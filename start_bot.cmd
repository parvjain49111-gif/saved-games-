@echo off
REM ---------------------------------------------------------------------------
REM Car Trends Instagram bot - one-click start (Windows)
REM   1. starts the Cloudflare quick tunnel (public HTTPS address -> port 8000)
REM   2. starts the bot on 127.0.0.1:8000
REM The bot follows the tunnel's address by itself and re-registers the Meta
REM webhook when the address changed (needs APP_SECRET = Meta app secret in .env).
REM Keep this window open; closing it stops the bot. Run stop_bot.cmd to stop.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set CLOUDFLARED=C:\cloudflared\cloudflared.exe.exe
if not exist "%CLOUDFLARED%" set CLOUDFLARED=cloudflared

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if %errorlevel%==0 (
    echo Port 8000 is already in use - the bot seems to be running. Run stop_bot.cmd first.
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq cloudflared.exe.exe" 2>nul | find /I "cloudflared" >nul
if not %errorlevel%==0 (
    echo Starting the Cloudflare tunnel...
    start "Car Trends tunnel" /MIN "%CLOUDFLARED%" tunnel --url http://127.0.0.1:8000
    timeout /t 8 /nobreak >nul
) else (
    echo Cloudflare tunnel already running.
)

echo Starting the bot on http://127.0.0.1:8000 ...
set PYTHONIOENCODING=utf-8
python -m uvicorn bot:app --host 127.0.0.1 --port 8000 --no-access-log
endlocal
