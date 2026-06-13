@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Git。请先安装 Git for Windows 后再运行本脚本。
    echo 下载: https://git-scm.com/download/win
    exit /b 1
)

if exist "cookies.json" (
    echo [提示] cookies.json 已在 .gitignore 中，不会纳入版本库
)
if exist "accounts\" (
    echo [提示] accounts/ 已在 .gitignore 中，不会纳入版本库
)
if exist "..\soop_tools\" (
    echo [提示] 上级 soop_tools/ 为本地开发辅助脚本，请勿加入本仓库
)

if not exist ".git" (
    echo [1/4] 初始化本地仓库...
    git init -b main
) else (
    echo [1/4] 使用已有本地仓库
)

echo [2/4] 暂存文件...
git add -A
git diff --cached --name-only | findstr /i /r "cookies accounts soop_tools \.har _probe soop_capture" >nul 2>&1
if not errorlevel 1 (
    echo [错误] 暂存区含 cookies、accounts、soop_tools 或抓包文件，已中止提交
    echo 请检查 .gitignore 后执行: git reset HEAD
    git diff --cached --name-only
    exit /b 1
)
git status --short

echo.
echo [3/4] 创建初始提交...
git diff --cached --quiet
if not errorlevel 1 (
    echo 没有新的变更，跳过提交
) else (
    git commit -m "Initial release: SOOP Drops Miner v1.0.0" -m "SOOP Live 掉宝挂机工具初始版本，包含 GUI、Bridge 进房、任务进度、奖励背包与 PyInstaller 打包配置。"
)

echo [4/4] 打标签 v1.0.0...
git rev-parse v1.0.0 >nul 2>&1
if not errorlevel 1 (
    echo 标签 v1.0.0 已存在，跳过
) else (
    git tag -a v1.0.0 -m "SOOP Drops Miner v1.0.0"
)

echo.
echo ===== 完成 =====
echo 仓库目录: %CD%
echo 当前分支:
git branch --show-current 2>nul || git rev-parse --abbrev-ref HEAD
echo.
echo 最近提交:
git log -1 --oneline
echo.
echo 标签:
git tag -l
echo.
echo 未配置远端，不会 push。如需以后添加远端:
echo   git remote add origin ^<你的仓库 URL^>
echo   git push -u origin main --tags
endlocal
