@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo 打包聊天档案工作台为独立窗口 exe
echo ============================================
echo.
echo 依赖检查：pywebview / pyinstaller
python -c "import webview, PyInstaller" 2>nul
if errorlevel 1 (
  echo 缺少依赖，正在安装...
  python -m pip install pywebview pyinstaller -i https://mirrors.cloud.tencent.com/pypi/simple
)
echo.
echo 开始打包（约需 1-3 分钟）...
python -m PyInstaller ChatVault.spec --noconfirm --distpath "..\ChatVault-App" --workpath ".cache\pyinstaller"
if errorlevel 1 (
  echo 打包失败。
  pause
  exit /b 1
)
echo.
echo ============================================
echo 打包完成：..\ChatVault-App\聊天档案工作台.exe
echo 首次运行会在 exe 目录生成 .local\config.json，
echo 请把自己的归档路径与 GLM 密钥填入其中。
echo ============================================
pause
