#!/usr/bin/env python3
"""Chiang Mai daily report: weather, flood risk, air quality/haze, US-relevant news -> static HTML site.

Stdlib only. Run:  python build_report.py
Output:
  site/index.html                 today's report (open this in a browser / bookmark it)
  site/archive/YYYY-MM-DD.html    one page per day
  site/archive/index.html         list of all days
  data/YYYY-MM-DD.json            raw collected data for each day
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import pathlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent
SITE = ROOT / "site"
ARCHIVE = SITE / "archive"
DATA = ROOT / "data"
BKK = dt.timezone(dt.timedelta(hours=7))

LAT, LON = 18.7883, 98.9853            # Chiang Mai city centre
RIVER_LAT, RIVER_LON = 18.79, 98.99    # nearest river cell in the flood model (small stream; see note in report)
UA = "chiangmai-daily/1.0 (personal daily report)"

GDACS_FLOOD_RSS = "https://www.gdacs.org/xml/rss_fl.xml"
STATE_DEPT_RSS = "https://travel.state.gov/_res/rss/TAsTWs.xml"

NEWS_QUERIES = {
    "Visa & immigration": '("Thailand visa" OR "Thailand immigration" OR "90-day report" OR "visa exemption" OR "Thailand work permit")',
    "Costs & economy": '("Thailand baht" OR "Thailand inflation" OR "Thailand electricity" OR "Thailand rent" OR "Chiang Mai cost of living")',
    "Risks & safety": '("Chiang Mai" OR Thailand) (flood OR scam OR earthquake OR "tourist arrested" OR "travel warning")',
    "Haze & smoke": '("Chiang Mai" OR "northern Thailand") (haze OR smoke OR PM2.5 OR "burning season" OR "crop burning" OR "air pollution")',
    "Chiang Mai local": '"Chiang Mai"',
    "Interesting in Chiang Mai": '"Chiang Mai" (festival OR event OR opens OR launches OR award OR concert OR market)',
}

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


# ---------- fetching ----------

def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def get_json(url: str) -> dict:
    return json.loads(get(url))


def fetch_weather() -> dict:
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Bangkok",
        "past_days": 3, "forecast_days": 7,
        "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                  "precipitation_probability_max,wind_speed_10m_max"),
    })
    return get_json("https://api.open-meteo.com/v1/forecast?" + q)


def fetch_river() -> dict:
    q = urllib.parse.urlencode({
        "latitude": RIVER_LAT, "longitude": RIVER_LON, "timezone": "Asia/Bangkok",
        "past_days": 3, "forecast_days": 7, "daily": "river_discharge",
    })
    return get_json("https://flood-api.open-meteo.com/v1/flood?" + q)


def fetch_air() -> dict:
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Bangkok",
        "current": "us_aqi,pm2_5", "hourly": "us_aqi,pm2_5",
        "past_days": 2, "forecast_days": 5,
    })
    return get_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + q)


def fetch_gdacs_thailand() -> list[dict]:
    # The GDACS feed is not always well-formed XML, so extract items with regex.
    text = get(GDACS_FLOOD_RSS).decode("utf-8", "replace")

    def tag(block: str, name: str) -> str:
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
        return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""

    out = []
    for block in re.findall(r"<item>(.*?)</item>", text, re.S):
        title, desc = tag(block, "title"), tag(block, "description")
        if "thailand" in (title + " " + desc).lower():
            out.append({"title": title, "link": tag(block, "link"), "published": tag(block, "pubDate")})
    return out


def fetch_state_dept_thailand() -> dict | None:
    root = ET.fromstring(get(STATE_DEPT_RSS))
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        if "thailand" in title.lower():
            desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
            return {"title": title, "link": it.findtext("link") or "",
                    "published": it.findtext("pubDate") or "",
                    "summary": " ".join(html.unescape(desc).split())[:600]}
    return None


def fetch_news(query: str, limit: int = 8) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query + " when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en",
    })
    root = ET.fromstring(get(url))
    items = []
    for it in root.iter("item"):
        items.append({
            "title": it.findtext("title") or "",
            "link": it.findtext("link") or "",
            "published": it.findtext("pubDate") or "",
            "source": it.findtext("source") or "",
        })
    return items[:limit * 2]


# ---------- processing ----------

def parse_date(s: str) -> dt.datetime:
    try:
        return email.utils.parsedate_to_datetime(s).astimezone(BKK)
    except (TypeError, ValueError):
        return dt.datetime(1970, 1, 1, tzinfo=BKK)


def fmt_date(s: str) -> str:
    d = parse_date(s)
    return d.strftime("%d %b %H:%M") if d.year > 1970 else s


def dedupe_sort(items: list[dict], limit: int) -> list[dict]:
    seen, out = set(), []
    for it in sorted(items, key=lambda i: parse_date(i["published"]), reverse=True):
        key = re.sub(r"\W+", " ", it["title"].lower()).strip()[:80]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out[:limit]


def flood_assessment(weather: dict, river: dict | None) -> dict:
    """Heuristic from rainfall (and river trend). NOT an official warning; see TMD."""
    precip = weather["daily"]["precipitation_sum"]
    past3 = sum(p or 0 for p in precip[0:3])
    next3 = sum(p or 0 for p in precip[3:6])
    peak = max((p or 0 for p in precip[3:10]), default=0)
    reasons = [
        f"Rain last 3 days: {past3:.0f} mm",
        f"Forecast rain next 3 days: {next3:.0f} mm",
        f"Heaviest forecast day (7 days): {peak:.0f} mm",
    ]
    if past3 >= 100 or next3 >= 100 or peak >= 80:
        level = "HIGH"
    elif past3 >= 50 or next3 >= 50 or peak >= 50:
        level = "ELEVATED"
    else:
        level = "LOW"

    trend = None
    if river:
        flows = [v for v in river["daily"]["river_discharge"][3:] if v is not None]
        if len(flows) >= 2 and flows[0] > 0:
            trend = flows[-1] / flows[0]
            if trend >= 1.5:
                reasons.append(f"Modelled river discharge rising ({trend:.1f}x over 7 days)")
                if level == "LOW":
                    level = "ELEVATED"
            elif trend <= 0.7:
                reasons.append(f"Modelled river discharge falling ({trend:.1f}x over 7 days)")
            else:
                reasons.append("Modelled river discharge roughly stable")
    return {"level": level, "reasons": reasons, "trend": trend}


def aqi_label(v: float | None) -> str:
    if v is None:
        return "n/a"
    for limit, label in [(50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"),
                         (200, "Unhealthy"), (300, "Very unhealthy")]:
        if v <= limit:
            return label
    return "Hazardous"


def aqi_daily_max(air: dict) -> list[tuple[str, float | None, float | None]]:
    """(date, max US AQI, max PM2.5) per day from the hourly series."""
    h = air["hourly"]
    by_day: dict[str, list[tuple[float | None, float | None]]] = {}
    for t, aqi, pm in zip(h["time"], h["us_aqi"], h["pm2_5"]):
        by_day.setdefault(t[:10], []).append((aqi, pm))
    out = []
    for day, vals in by_day.items():
        aqis = [a for a, _ in vals if a is not None]
        pms = [p for _, p in vals if p is not None]
        out.append((day, max(aqis) if aqis else None, max(pms) if pms else None))
    return out


# ---------- rendering ----------

E = html.escape

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;
line-height:1.5;color:#1d1d1f;background:#f7f7f5}
h1{margin-bottom:.2rem}h2{margin-top:2rem;border-bottom:1px solid #ccc;padding-bottom:.2rem}
a{color:#0b5cad}nav a{margin-right:1.2rem;font-weight:600}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:1rem 1.25rem;margin:1rem 0}
.badge{display:inline-block;padding:.15rem .7rem;border-radius:999px;font-weight:700;color:#fff}
.LOW{background:#2e7d32}.ELEVATED{background:#ef6c00}.HIGH{background:#c62828}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:.4rem .5rem;text-align:left}
.muted{color:#666;font-size:.9em}.err{color:#b00020}.news li{margin:.35rem 0}
@media (prefers-color-scheme:dark){
body{background:#121212;color:#e8e8e8}.card{background:#1d1d1d;border-color:#333}
th,td{border-color:#333}h2{border-color:#444}a{color:#8ab4ff}.muted{color:#999}}
"""


def page(title: str, body: str, today_href: str, archive_href: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)}</title><style>{CSS}</style></head>
<body>
<nav><a href="{today_href}">Today</a><a href="{archive_href}">Archive</a></nav>
{body}
</body></html>"""


def render_news(title: str, items: list[dict]) -> str:
    if not items:
        return f"<h3>{E(title)}</h3><p class='muted'>Nothing in the last 2 days.</p>"
    lis = "".join(
        f"<li><a href='{E(i['link'])}' target='_blank' rel='noopener'>{E(i['title'])}</a>"
        f" <span class='muted'>{E(i.get('source', ''))} · {E(fmt_date(i['published']))}</span></li>"
        for i in items
    )
    return f"<h3>{E(title)}</h3><ul class='news'>{lis}</ul>"


def render_report(r: dict, nav: tuple[str, str]) -> str:
    now: dt.datetime = dt.datetime.fromisoformat(r["generated"])
    errors = r["errors"]
    parts = [f"<h1>Chiang Mai daily report</h1>"
             f"<p class='muted'>{now:%A %d %B %Y}, generated {now:%H:%M} (Bangkok time)</p>"]

    # Flood risk
    fl = r.get("flood")
    if fl:
        parts.append("<h2>Flood risk</h2><div class='card'>")
        parts.append(f"<p><span class='badge {fl['level']}'>{fl['level']}</span></p><ul>")
        parts += [f"<li>{E(x)}</li>" for x in fl["reasons"]]
        parts.append("</ul><p class='muted'>Heuristic from rainfall and a modelled river forecast, not an official "
                     "warning. The modelled Ping River point is a small-stream grid cell, so trend matters more "
                     "than absolute flow. Official warnings: <a href='https://www.tmd.go.th/' target='_blank' "
                     "rel='noopener'>Thai Meteorological Dept</a> and local Chiang Mai authorities.</p></div>")

    # GDACS
    gd = r.get("gdacs")
    if gd is not None:
        if gd:
            lis = "".join(f"<li><a href='{E(g['link'])}' target='_blank' rel='noopener'>{E(g['title'])}</a> "
                          f"<span class='muted'>{E(fmt_date(g['published']))}</span></li>" for g in gd)
            parts.append(f"<h3>Active flood alerts for Thailand (GDACS)</h3><ul>{lis}</ul>")
        else:
            parts.append("<p class='muted'>No Thailand flood alerts in the GDACS global feed.</p>")

    # Weather now
    w = r.get("weather")
    if w:
        cur = w["current"]
        parts.append("<h2>Weather</h2><div class='card'>")
        parts.append(f"<p><strong>Now:</strong> {cur['temperature_2m']}°C, "
                     f"{E(WMO.get(cur['weather_code'], 'Unknown'))}, humidity {cur['relative_humidity_2m']}%, "
                     f"precipitation {cur['precipitation']} mm</p>")
        parts.append("<table><tr><th>Date</th><th>Conditions</th><th>Min / Max °C</th>"
                     "<th>Rain mm</th><th>Rain chance</th><th>Max wind km/h</th></tr>")
        d = w["daily"]
        for i in range(3, len(d["time"])):
            parts.append(
                f"<tr><td>{E(dt.date.fromisoformat(d['time'][i]).strftime('%a %d %b'))}</td>"
                f"<td>{E(WMO.get(d['weather_code'][i], 'Unknown'))}</td>"
                f"<td>{d['temperature_2m_min'][i]} / {d['temperature_2m_max'][i]}</td>"
                f"<td>{d['precipitation_sum'][i]}</td>"
                f"<td>{d['precipitation_probability_max'][i]}%</td>"
                f"<td>{d['wind_speed_10m_max'][i]}</td></tr>")
        parts.append("</table></div>")

    # Air quality and haze
    aq = r.get("air")
    if aq:
        c = aq["current"]
        parts.append("<h2>Air quality and haze</h2><div class='card'>"
                     f"<p><strong>Now: US AQI {c['us_aqi']}</strong> ({E(aqi_label(c['us_aqi']))}), "
                     f"PM2.5 {c['pm2_5']} µg/m³</p>")
        rows = aqi_daily_max(aq)
        today = dt.datetime.fromisoformat(r["generated"]).strftime("%Y-%m-%d")
        rows = [x for x in rows if x[0] >= today]
        if rows:
            parts.append("<table><tr><th>Day</th><th>Peak US AQI</th><th>Peak PM2.5 µg/m³</th>"
                         "<th>Category</th></tr>")
            for day, aqi, pm in rows:
                parts.append(f"<tr><td>{E(dt.date.fromisoformat(day).strftime('%a %d %b'))}</td>"
                             f"<td>{aqi}</td><td>{pm}</td><td>{E(aqi_label(aqi))}</td></tr>")
            parts.append("</table>")
        parts.append(
            "<p class='muted'><strong>Haze season:</strong> Chiang Mai's smoke problem is usually worst from roughly "
            "late January to April, peaking around March, driven by agricultural and forest burning in the region "
            "and atmospheric inversions that trap PM2.5 in the valley. Burning can start in late autumn, so a rise "
            "in the table before the main season is possible. Practical: check the table each morning, keep N95-type "
            "masks and HEPA filtration at home, and watch the Haze &amp; smoke news below. Station-level data: "
            "<a href='https://aqicn.org/city/thailand/chiang-mai/' target='_blank' rel='noopener'>AQICN</a>.</p>"
            "</div>")

    # US State Department
    sd = r.get("state_dept")
    if sd:
        parts.append(f"<h2>US State Department advisory</h2><div class='card'>"
                     f"<p><a href='{E(sd['link'])}' target='_blank' rel='noopener'><strong>{E(sd['title'])}</strong></a> "
                     f"<span class='muted'>{E(fmt_date(sd['published']))}</span></p>"
                     f"<p>{E(sd['summary'])}</p></div>")

    # News
    news = r.get("news", {})
    parts.append("<h2>News relevant to you</h2>")
    for cat in NEWS_QUERIES:
        parts.append(render_news(cat, news.get(cat, [])))

    # Errors
    if errors:
        parts.append("<h2>Sources that failed today</h2><ul>")
        parts += [f"<li class='err'>{E(k)}: {E(v)}</li>" for k, v in errors.items()]
        parts.append("</ul>")

    return page(f"Chiang Mai report {now:%Y-%m-%d}", "\n".join(parts), *nav)


def render_archive_index(days: list[str]) -> str:
    lis = "".join(f"<li><a href='{d}.html'>{d}</a></li>" for d in days) or "<li>No reports yet.</li>"
    body = f"<h1>Archive</h1><ul>{lis}</ul>"
    return page("Chiang Mai report archive", body, "../index.html", "index.html")


# ---------- main ----------

def collect() -> dict:
    now = dt.datetime.now(BKK)
    r: dict = {"generated": now.isoformat(timespec="minutes"), "errors": {}}

    def run(key: str, fn):
        try:
            return fn()
        except Exception as e:  # keep going; failures are shown in the report
            r["errors"][key] = f"{type(e).__name__}: {e}"
            return None

    r["weather"] = run("weather forecast (Open-Meteo)", fetch_weather)
    r["air"] = run("air quality (Open-Meteo)", fetch_air)
    river = run("river discharge (Open-Meteo flood API)", fetch_river)
    r["gdacs"] = run("GDACS flood alerts", fetch_gdacs_thailand)
    r["state_dept"] = run("US State Department advisory", fetch_state_dept_thailand)

    r["flood"] = flood_assessment(r["weather"], river) if r["weather"] else None

    r["news"] = {}
    for cat, q in NEWS_QUERIES.items():
        items = run(f"news: {cat}", lambda q=q: fetch_news(q))
        r["news"][cat] = dedupe_sort(items or [], 8)
    return r


def main() -> None:
    r = collect()
    day = dt.datetime.fromisoformat(r["generated"]).strftime("%Y-%m-%d")
    SITE.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)

    (DATA / f"{day}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    (ARCHIVE / f"{day}.html").write_text(render_report(r, ("../index.html", "index.html")), encoding="utf-8")
    (SITE / "index.html").write_text(render_report(r, ("index.html", "archive/index.html")), encoding="utf-8")

    days = sorted((p.stem for p in DATA.glob("*.json")), reverse=True)
    (ARCHIVE / "index.html").write_text(render_archive_index(days), encoding="utf-8")

    print(f"Wrote {SITE / 'index.html'}")
    if r["errors"]:
        print("Failed sources:")
        for k, v in r["errors"].items():
            print(f"  - {k}: {v}")


if __name__ == "__main__":
    main()
