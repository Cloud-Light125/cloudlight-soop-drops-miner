@echo off
setlocal
cd /d "%~dp0.."

echo [1/3] 安装打包依赖...
python -m pip install pyinstaller aiohttp websockets yarl -q

echo [2/3] 打包 exe（不包含 cookies.json）...
if exist "soop_miner\cookies.json" (
    echo   注意: 检测到本地 cookies.json，不会打入 exe
)
pyinstaller soop_miner\build.spec --noconfirm --clean
if errorlevel 1 (
    echo 打包失败
    exit /b 1
)

echo [3/3] 检查输出...
if not exist "dist\SOOP_Drops_Miner.exe" (
    echo 未找到 dist\SOOP_Drops_Miner.exe
    exit /b 1
)

findstr /i /c:"www5329" /c:"AuthTicket" /c:"BbsTicket" "dist\SOOP_Drops_Miner.exe" >nul 2>&1
if not errorlevel 1 (
    echo 警告: exe 中可能含有账号相关字符串，请检查后再分发
) else (
    echo 安全检查通过: exe 未检测到本地账号 cookie
)

echo.
echo 完成: dist\SOOP_Drops_Miner.exe
echo 可将该 exe 单独发给他人使用；对方登录后会在 exe 同目录生成 cookies.json
endlocal
