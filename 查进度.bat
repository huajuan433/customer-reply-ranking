@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   查"我发到哪个客户了"——最近2小时
echo.
python send_tracker.py --hours 2
echo.
pause
