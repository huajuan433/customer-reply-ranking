@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title 生成分发包
echo ============================================
echo   生成可以给同事用的分发包
echo ============================================
echo.
echo 会自动剔除：你的聊天记录、解密数据库、密钥、账号配置。
echo.
python make_package.py
echo.
pause
