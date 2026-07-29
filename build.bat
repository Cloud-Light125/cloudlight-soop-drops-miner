@echo off
setlocal
cd /d "%~dp0"

echo [1/3] 安装受约束的运行及打包依赖...
python -m pip install -r "%~dp0requirements.txt" "pyinstaller>=6.0,<7.0" -q
if errorlevel 1 exit /b 1

echo [2/3] 打包 CloudLight SOOP Drops Miner...
python -m PyInstaller "%~dp0build.spec" --noconfirm --clean
if errorlevel 1 (
    echo 打包失败
    exit /b 1
)

echo [3/3] 检查输出...
set "OUTPUT=%~dp0dist\CloudLight_SOOP_Drops_Miner.exe"
if not exist "%OUTPUT%" (
    echo 未找到 "%OUTPUT%"
    exit /b 1
)

echo 完成: "%OUTPUT%"
endlocal
