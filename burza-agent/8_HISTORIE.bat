@echo off
cd /d "%~dp0"
echo Pocitam, jak by si strategie vedla v minulosti proti S^&P 500 a Nasdaq 100 a vyhled na 3 roky...
echo.
".venv\Scripts\python.exe" -m burza --config config_a.toml history-report
echo.
echo Report se otevrel v prohlizeci (soubor data_a\historie.html).
pause
