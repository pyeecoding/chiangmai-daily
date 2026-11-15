@echo off
rem Regenerates the Chiang Mai daily report in site\index.html (called by Task Scheduler).
cd /d "%~dp0"
python build_report.py >> "%~dp0run.log" 2>&1
