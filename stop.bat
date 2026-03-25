@echo off
echo Stopping Cricket Score Scraper...

REM Kill all python processes running scraper.py
wmic process where "name='python3.13.exe' and commandline like '%%scraper.py%%'" delete >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%scraper.py%%'" delete >nul 2>&1

echo Scraper stopped.
pause
