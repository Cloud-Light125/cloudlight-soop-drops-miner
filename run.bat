@echo off
setlocal
cd /d "%~dp0"
python "%~dp0entry.py" --gui %*
endlocal
