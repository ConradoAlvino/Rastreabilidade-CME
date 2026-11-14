@echo off
cd /d "%~dp0"
echo Iniciando TrayTrack no navegador...
start "" "http://127.0.0.1:8000"
py -3 web_app.py
if errorlevel 1 (
    echo.
    echo O comando py falhou. Tentando python...
    python web_app.py
)
pause
