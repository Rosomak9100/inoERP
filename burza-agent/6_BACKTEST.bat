@echo off
cd /d "%~dp0"
echo Porovnani strategii na historickych datech za posledni rok.
echo Stazeni dat z eToro a vypocet trvaji asi 5-15 minut, nech okno otevrene.
echo.
".venv\Scripts\python.exe" -m burza --config config_a.toml compare --days 365 --refresh
echo.
echo Vysledky jsou ulozene v data_a\backtests.jsonl. Posli Claudovi snimek teto tabulky.
pause
