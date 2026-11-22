@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动聊天档案工作台...
echo 浏览器将打开 http://127.0.0.1:8765
start "" "http://127.0.0.1:8765"
python chatvault.py
pause
