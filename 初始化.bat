@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title 初始化 - 客户回复排行榜
echo ============================================
echo   客户回复排行榜 - 首次使用请先运行本文件
echo ============================================
echo.
echo 需要：Windows + Python 3.10+ + 微信 4.x 已登录并保持运行。
echo 首次运行需要网络安装 Python 依赖；聊天数据只在本机处理。
echo 本工具只读你自己账号的聊天记录，不会上传任何数据。
echo.
python init_setup.py
echo.
pause


