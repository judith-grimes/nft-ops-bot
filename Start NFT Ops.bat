@echo off
cd /d "%~dp0"
echo Starting NFT Ops... (close this window or press Ctrl+C to stop)
uv run --frozen nft-ops
pause
