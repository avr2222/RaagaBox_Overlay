@echo off
echo =======================================
echo   CRICHEROES TOURNAMENT AUTO-TRACKER
echo =======================================

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.x and add it to PATH.
    pause
    exit /b 1
)

echo Checking dependencies...
python -c "import playwright, requests" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    python -m playwright install chromium
)

echo Starting local HTTP server on port 8080...
start "Overlay HTTP Server" /min python -m http.server 8080

echo Opening overlay in browser...
timeout /t 1 /nobreak >nul
start "" "http://localhost:8080/overlay.html"

echo Starting scraper...
python scraper.py
pause
