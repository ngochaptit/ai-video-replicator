@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  start "" mshta "javascript:alert('AI Video Replicator chua duoc cai dat day du. Hay lien he nguoi cai dat.');close();"
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -m moon.operator_launcher
exit /b 0
