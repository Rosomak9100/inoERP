# Jednorázová instalace na Windows.
# Spuštění (v PowerShellu ve složce burza-agent):
#   powershell -ExecutionPolicy Bypass -File windows\instalace.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Python nenalezen. Nainstaluj Python 3.12 z https://www.python.org/downloads/ (zaškrtni 'Add python.exe to PATH') a spusť skript znovu." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".venv")) { py -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\pip.exe install -r requirements.txt

if (-not (Test-Path "config_a.toml")) { Copy-Item config_a.example.toml config_a.toml }
if (-not (Test-Path "config_b.toml")) { Copy-Item config_b.example.toml config_b.toml }
if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Otevírám .env – vyplň klíče, ulož (Ctrl+S) a zavři Poznámkový blok." -ForegroundColor Yellow
    Start-Process notepad.exe ".env" -Wait
}

& .\.venv\Scripts\python.exe -m burza --config config_a.toml check
& .\.venv\Scripts\python.exe -m burza init-start --configs config_a.toml config_b.toml
Write-Host "Hotovo. Spuštění agentů: powershell -ExecutionPolicy Bypass -File windows\spust.ps1" -ForegroundColor Green
