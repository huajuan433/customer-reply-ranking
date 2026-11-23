@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title 设置客户识别规则
echo ============================================
echo   客户识别规则设置
echo ============================================
echo.
echo 工具需要知道：微信里哪些联系人算"你的客户"。
echo.
echo   1. 备注以数字开头  （如 2505山东省立张医生，编号客户）
echo   2. 有备注就算客户  （备注非空即纳入）
echo   3. 备注包含关键词  （如"客户""患者"）
echo.
echo 不确定就选 1；如果榜单是空的，改选 2。
echo.
set /p choice=请输入 1 / 2 / 3 然后回车：
echo.

if "%choice%"=="1" goto rule1
if "%choice%"=="2" goto rule2
if "%choice%"=="3" goto rule3
echo 输入无效，按 1 处理。
goto rule1

:rule1
python reply_ranking.py --rule number_prefix --rescan --scan-only
goto done

:rule2
python reply_ranking.py --rule has_remark --rescan --scan-only
goto done

:rule3
set /p kw=请输入关键词（如：客户）：
python reply_ranking.py --rule keyword --keyword "%kw%" --rescan --scan-only
goto done

:done
echo.
echo 设置完成。双击「客户回复排行榜.bat」查看新榜单。
pause
