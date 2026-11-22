@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "APP=..\ChatVault-App\聊天档案工作台.exe"
if not exist "%APP%" (
  echo 未找到独立窗口程序，请先运行 打包exe.bat 生成。
  pause
  exit /b 1
)
echo 正在启动聊天档案工作台（独立窗口）...
start "" "%APP%"
