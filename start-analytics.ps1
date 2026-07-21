$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Url = "http://127.0.0.1:8765"

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 2
    if ($health.StatusCode -eq 200) {
        Start-Process $Url
        exit 0
    }
} catch {
}

$Python = (Get-Command python -ErrorAction Stop).Source
$Pythonw = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (Test-Path -LiteralPath $Pythonw) {
    Start-Process -FilePath $Pythonw `
        -ArgumentList @((Join-Path $ScriptDir "analytics_server.py"), "--open") `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden
} else {
    Start-Process -FilePath $Python `
        -ArgumentList @((Join-Path $ScriptDir "analytics_server.py"), "--open") `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden
}
