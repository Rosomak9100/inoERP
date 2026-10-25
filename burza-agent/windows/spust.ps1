# Spustí Studii A, Studii B a dashboard (každý ve vlastním okně) a otevře prohlížeč.
# Agent se po pádu sám znovu spustí. Zastavení: python -m burza kill  nebo zavřít okna.
#   powershell -ExecutionPolicy Bypass -File windows\spust.ps1
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

function Start-Agent($config) {
    $cmd = "Set-Location '$root'; `$host.UI.RawUI.WindowTitle = 'Burza $config'; " +
           "while (`$true) { & '$py' -m burza --config $config run; Write-Host 'Agent skončil, restart za 60 s (Ctrl+C = konec)'; Start-Sleep 60 }"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
}

# Počítač nesmí během obchodních hodin (15:30–22:00 SEČ) usnout.
powercfg /change standby-timeout-ac 0 2>$null

Start-Agent "config_a.toml"
if (Test-Path (Join-Path $root "config_b.toml")) { Start-Agent "config_b.toml" }
$dash = "Set-Location '$root'; `$host.UI.RawUI.WindowTitle = 'Burza dashboard'; & '$py' -m burza dashboard --studies config_a.toml config_b.toml"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $dash
Start-Sleep 3
Start-Process "http://127.0.0.1:8080"
