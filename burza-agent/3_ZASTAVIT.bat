@echo off
cd /d "%~dp0"
echo Zastavuji agenty (zavrou sve pozice a skonci)...
".venv\Scripts\python.exe" -m burza --config config_a.toml kill
echo.
echo Agenti se zastavi behem nekolika desitek sekund. Znovu spustis pres 2_SPUSTIT.bat
pause
