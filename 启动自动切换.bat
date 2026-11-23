@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   微信"发完自动切下一个+记日志"工具
echo.
echo   热键说明：
echo     Ctrl+Alt+N  = 记下当前客户 + 切到下一个
echo     Ctrl+Alt+P  = 跳回上一个客户
echo     Ctrl+Alt+L  = 显示发送日志
echo     Ctrl+Alt+S  = 退出程序
echo.
echo   注意：发送动作仍需你手动按，本工具只负责切会话和记日志，不会替你发消息。
echo.
python auto_next.py
