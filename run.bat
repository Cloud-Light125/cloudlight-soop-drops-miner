@echo off
cd /d "%~dp0.."
python -c "import websockets" 2>nul || pip install websockets -q
python -m soop_miner --gui %*
