@echo off
REM Stops the Car Trends bot and the Cloudflare quick tunnel started by start_bot.cmd.
REM Note: the next start_bot.cmd gets a NEW tunnel address; the bot re-registers
REM the Meta webhook automatically when APP_SECRET holds the Meta app secret.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
    echo Stopping bot process %%p
    taskkill /PID %%p /F >nul 2>&1
)
taskkill /IM cloudflared.exe.exe /F >nul 2>&1 && echo Tunnel stopped.
echo Done.
