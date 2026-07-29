@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "HTTP_PROXY=http://127.0.0.1:7890"
set "HTTPS_PROXY=http://127.0.0.1:7890"

set "OWNER=yundan125"
set "REPO=cloudlight-soop-drops-miner"
set "TAG=v1.0"
set "EXE_PATH=%~dp0dist\CloudLight_SOOP_Drops_Miner.exe"
set "ASSET_NAME=CloudLight_SOOP_Drops_Miner.exe"

if not exist "%EXE_PATH%" (
    echo [ERROR] Exe not found:
    echo   %EXE_PATH%
    echo Build first, or edit EXE_PATH in this script.
    exit /b 1
)

echo ===== Upload %TAG% release asset =====
echo File: %EXE_PATH%
for %%A in ("%EXE_PATH%") do echo Size: %%~zA bytes
echo Repo: https://github.com/%OWNER%/%REPO%
echo.

set "GITHUB_TOKEN="
set /p GITHUB_TOKEN=GitHub Token with repo scope: 
if "%GITHUB_TOKEN%"=="" (
    echo [ERROR] Token is required.
    echo Create at: https://github.com/settings/tokens
    exit /b 1
)

set "RELEASE_JSON=%TEMP%\soop_release_%TAG%.json"
curl -sS -H "Authorization: token %GITHUB_TOKEN%" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/%OWNER%/%REPO%/releases/tags/%TAG%" > "%RELEASE_JSON%"
findstr /c:"upload_url" "%RELEASE_JSON%" >nul 2>&1
if errorlevel 1 (
    echo Creating release for tag %TAG%...
    curl -sS -X POST -H "Authorization: token %GITHUB_TOKEN%" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/%OWNER%/%REPO%/releases" -d "{\"tag_name\":\"%TAG%\",\"name\":\"CloudLight SOOP Drops Miner %TAG%\",\"body\":\"Windows build. Multi-account, systray, channel picker, GUI overhaul.\",\"draft\":false,\"prerelease\":false}" > "%RELEASE_JSON%"
    findstr /c:"upload_url" "%RELEASE_JSON%" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to create release:
        type "%RELEASE_JSON%"
        exit /b 1
    )
    echo [OK] Release created.
) else (
    echo [OK] Release already exists for %TAG%.
)

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-Content -Raw '%RELEASE_JSON%' | ConvertFrom-Json).id"`) do set "RELEASE_ID=%%I"
if "%RELEASE_ID%"=="" (
    echo [ERROR] Cannot read release id from API response.
    type "%RELEASE_JSON%"
    exit /b 1
)

echo Uploading %ASSET_NAME% ...
curl -sS -X POST -H "Authorization: token %GITHUB_TOKEN%" -H "Content-Type: application/octet-stream" --data-binary "@%EXE_PATH%" "https://uploads.github.com/repos/%OWNER%/%REPO%/releases/%RELEASE_ID%/assets?name=%ASSET_NAME%" > "%TEMP%\soop_upload_result.json"
findstr /c:"browser_download_url" "%TEMP%\soop_upload_result.json" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Upload failed:
    type "%TEMP%\soop_upload_result.json%"
    exit /b 1
)

echo.
echo ===== Done =====
echo Release page:
echo   https://github.com/%OWNER%/%REPO%/releases/tag/%TAG%
echo.
type "%TEMP%\soop_upload_result.json" | findstr /i "browser_download_url name size"
endlocal
