@echo off
cd /d "%~dp0"
echo Porovnani strategii na historickych datech za posledni rok.
echo Data se stahuji jen jednou denne. Vypocet trva asi 5-20 minut, nech okno otevrene.
echo.
".venv\Scripts\python.exe" -m burza --config config_a.toml compare --days 365
echo.
echo Vysledky jsou ulozene v data_a\backtests.jsonl. Posli Claudovi snimek teto tabulky.
pause
