# Instalace Burza agenta na Windows. Spouští se dvojklikem na 1_INSTALACE.bat.
# Lze spouštět opakovaně – co už je hotové, přeskočí.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"

function Krok($text) { Write-Host ""; Write-Host "=== $text ===" -ForegroundColor Cyan }
function Konec($text, $ok) {
    Write-Host ""
    if ($ok) { Write-Host $text -ForegroundColor Green } else { Write-Host $text -ForegroundColor Red }
    Read-Host "Stiskni Enter pro zavření okna"
    if ($ok) { exit 0 } else { exit 1 }
}

Krok "Krok 1 z 5: Kontrola Pythonu"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Konec "Python není nainstalovaný. Stáhni ho z https://www.python.org/downloads/ , při instalaci zaškrtni 'Add python.exe to PATH' a pak znovu dvojklik na 1_INSTALACE.bat." $false
}
$ver = (& py -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ([version]$ver -lt [version]"3.11") {
    Konec "Máš Python $ver, potřeba je 3.11 nebo novější. Nainstaluj nový z https://www.python.org/downloads/" $false
}
Write-Host "Python $ver – OK"

Krok "Krok 2 z 5: Instalace knihoven (může trvat pár minut)"
if (-not (Test-Path $py)) {
    & py -m venv .venv
    if ($LASTEXITCODE -ne 0) { Konec "Nepodařilo se vytvořit prostředí Pythonu." $false }
}
& $py -m pip install --disable-pip-version-check --quiet --upgrade pip
& $py -m pip install --disable-pip-version-check --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Konec "Instalace knihoven selhala. Zkontroluj internet a spusť 1_INSTALACE.bat znovu." $false }
Write-Host "Knihovny – OK"

Krok "Krok 3 z 5: Nastavení studií"
foreach ($s in "a", "b") {
    if (-not (Test-Path "config_$s.toml")) { Copy-Item "config_$s.example.toml" "config_$s.toml" }
}
Write-Host "Studie A a B připravené (200 000 Kč virtuálně, simulace)"

Krok "Krok 4 z 5: Klíče"
$values = [ordered]@{}
if (Test-Path $envFile) {
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        $t = $line.Trim().TrimStart([char]0xFEFF)
        if ($t -and -not $t.StartsWith("#") -and $t.Contains("=")) {
            $k, $v = $t.Split("=", 2)
            $values[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }
}
$keys = @(
    @("ETORO_API_KEY",  "eToro – API Key (api-portal.etoro.com → API Key Management)", $true),
    @("ETORO_USER_KEY", "eToro – User Key (zobrazí se jen jednou při vytvoření klíče)", $true),
    @("FINNHUB_API_KEY", "Finnhub – zprávy pro Studii B (zdarma na finnhub.io), Enter = přeskočit", $false),
    @("FRED_API_KEY",    "FRED – makrodata pro Studii B (zdarma na fred.stlouisfed.org), Enter = přeskočit", $false),
    @("ANTHROPIC_API_KEY", "Claude – AI pro Studii B (console.anthropic.com), Enter = přeskočit", $false)
)
Write-Host "Klíč vlož pravým tlačítkem myši (nebo Ctrl+V) a potvrď Enterem."
Write-Host "Klíče se uloží jen do souboru .env v tomto počítači. Nikomu je neposílej."
foreach ($k in $keys) {
    $name, $popis, $povinny = $k
    if ($values[$name]) { Write-Host "$name – už vyplněno"; continue }
    do {
        $val = (Read-Host "$popis`n$name").Trim().Trim('"').Trim("'")
        if ($povinny -and -not $val) { Write-Host "Tento klíč je povinný." -ForegroundColor Yellow }
    } while ($povinny -and -not $val)
    $values[$name] = $val
}
$lines = @("# Klíče Burza agenta – nikomu neposílat")
foreach ($name in $values.Keys) { $lines += "$name=$($values[$name])" }
[IO.File]::WriteAllLines($envFile, $lines)   # UTF-8 bez BOM
Write-Host "Klíče uložené."

Krok "Krok 5 z 5: Zkouška spojení s eToro"
& $py -m burza --config config_a.toml check
if ($LASTEXITCODE -ne 0) {
    Konec "Kontrola neprošla. Oprav klíče (smaž soubor .env a spusť 1_INSTALACE.bat znovu), nebo pošli Claudovi text výše – BEZ klíčů." $false
}
& $py -m burza init-start --configs config_a.toml config_b.toml
if (-not ($values["FINNHUB_API_KEY"] -or $values["FRED_API_KEY"]) -or -not $values["ANTHROPIC_API_KEY"]) {
    Write-Host "Studie B se nespustí, dokud nedoplníš klíče Finnhub/FRED a Claude (smaž .env a spusť instalaci znovu)." -ForegroundColor Yellow
}
Konec "HOTOVO. Agenta spustíš dvojklikem na 2_SPUSTIT.bat" $true
