# Spustí Studii A, Studii B a dashboard (každý ve vlastním okně) a otevře prohlížeč.
# Spouští se dvojklikem na 2_SPUSTIT.bat. Zastavení: 3_ZASTAVIT.bat
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py) -or -not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "Nejdřív je potřeba instalace: dvojklik na 1_INSTALACE.bat" -ForegroundColor Red
    Read-Host "Stiskni Enter"
    exit 1
}
if (Test-Path (Join-Path $root "STOP")) { Remove-Item (Join-Path $root "STOP") }  # zrušit dřívější zastavení

function Start-Agent($config, $title) {
    $cmd = "Set-Location '$root'; `$host.UI.RawUI.WindowTitle = '$title – NEZAVÍRAT'; " +
           "while (-not (Test-Path 'STOP')) { & '$py' -m burza --config $config run; " +
           "if (Test-Path 'STOP') { break }; Write-Host 'Agent se zastavil, nový pokus za 60 s'; Start-Sleep 60 }; " +
           "Write-Host 'Agent zastaven.'"
    Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command", $cmd
}

# Počítač nesmí během obchodních hodin (15:30–22:00 SEČ) usnout (platí pro napájení ze sítě).
powercfg /change standby-timeout-ac 0 2>$null

Start-Agent "config_a.toml" "Burza Studie A"
& $py -m burza --config config_b.toml status *> $null
if ($LASTEXITCODE -eq 0) {
    Start-Agent "config_b.toml" "Burza Studie B"
} else {
    Write-Host "Studie B se nespouští – chybí klíč Finnhub nebo FRED (zdarma; doplň přes 1_INSTALACE.bat)." -ForegroundColor Yellow
}
$dash = "Set-Location '$root'; `$host.UI.RawUI.WindowTitle = 'Burza dashboard – NEZAVÍRAT'; & '$py' -m burza dashboard --studies config_a.toml config_b.toml"
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command", $dash
Start-Sleep 4
Start-Process "http://127.0.0.1:8080"
Write-Host "Spuštěno. Okna agentů nech otevřená (můžeš je minimalizovat). Toto okno můžeš zavřít."
Start-Sleep 8
