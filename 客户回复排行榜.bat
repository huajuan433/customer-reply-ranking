@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 客户回复排行榜（请勿关闭本窗口）
echo ============================================
echo   客户回复排行榜 + 点击直达聊天
echo ============================================
echo.
echo 正在刷新微信数据库并扫描（微信需保持登录）...
echo 扫描完成后会自动打开浏览器，在网页里点「打开聊天」即可。
echo.
echo 【重要】这个黑窗口要一直开着，关掉网页就点不动了。
echo.
python reply_ranking.py --update --port 8765
echo.
echo 服务已退出。
pause
