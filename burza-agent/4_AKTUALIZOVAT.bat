@echo off
cd /d "%~dp0"
echo Stahuji nejnovejsi verzi...
git pull
if errorlevel 1 (
  echo.
  echo Aktualizace se nepovedla. Pokud jsi kod stahoval jako ZIP, stahni novy ZIP z GitHubu.
  pause
  exit /b 1
)
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m burza sync-config --configs config_a.toml config_b.toml
echo.
echo Hotovo. Pokud agenti bezi, zastav je (3_ZASTAVIT.bat) a spust znovu (2_SPUSTIT.bat).
pause
