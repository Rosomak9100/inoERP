@echo off
cd /d "%~dp0"
echo Prepinam obe studie na swing strategii RSI(2) a zakladam novy start experimentu.
echo (Agenti musi byt zastaveni - ikona Burza - ZASTAVIT.)
echo.
".venv\Scripts\python.exe" -m burza set-strategy --configs config_a.toml config_b.toml --horizon swing --strategy rsi2_pullback
if errorlevel 1 goto chyba
".venv\Scripts\python.exe" -m burza --config config_a.toml new-experiment --configs config_a.toml config_b.toml
if errorlevel 1 goto chyba
echo.
echo Hotovo. Spust agenty ikonou Burza - SPUSTIT.
pause
exit /b 0
:chyba
echo.
echo Neco se nepovedlo - posli Claudovi snimek tohoto okna.
pause
exit /b 1
