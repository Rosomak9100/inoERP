@echo off
cd /d "%~dp0"
echo Porovnani strategii na historickych datech (stahne 120 dni z eToro, trva nekolik minut)...
echo.
".venv\Scripts\python.exe" -m burza --config config_a.toml compare --days 120
echo.
echo Vysledky jsou ulozene v data_a\backtests.jsonl. Posli Claudovi snimek teto tabulky.
pause
