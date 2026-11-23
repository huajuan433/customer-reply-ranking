@echo off
chcp 65001 >nul
cd /d "%~dp0tools\Moect"
echo ============================================
echo   微信聊天记录 - 重新导出并清洗
echo ============================================
echo.
echo [1/3] 提取密钥并解密（需微信保持登录）...
python -m wx4 key >nul 2>&1
python -c "import os; from wx4 import decrypt as d, key as k, paths; a=paths.find_account(); p=k.load_keys('wx4_keys.json'); keys={x:y for x,y in p.items() if not x.startswith('_')}; d.decrypt_all(keys, a['db_storage'], 'app/DataBase/decrypted')" >nul 2>&1
echo       完成
echo.
echo [2/3] 导出全部会话为CSV...
python -m wx4 export --decrypted-dir app/DataBase/decrypted -f csv --export-dir "%~dp0output\raw_csv" >nul 2>&1
echo       完成
echo.
echo [3/3] 清洗并生成AI分析包...
cd /d "%~dp0"
python clean_chats.py --top 20 --ai-pack 15
echo.
echo ============================================
echo   全部完成！
echo   高价值客户排行: output\cleaned\高价值客户排行.md
echo   AI分析包:       output\cleaned\AI分析包.txt
echo   单个客户记录:   output\cleaned\ (按客户名命名)
echo ============================================
pause
