param(
    [int]$Port = 0,
    [string]$NodeId = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscale) {
    $defaultTailscale = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $defaultTailscale) {
        $tailscalePath = $defaultTailscale
    } else {
        throw "Tailscale CLI was not found. Install Tailscale and sign in first."
    }
} else {
    $tailscalePath = $tailscale.Source
}

$tailscaleIp = (& $tailscalePath ip -4 2>$null | Select-Object -First 1).ToString().Trim()
if (-not $tailscaleIp) {
    throw "Tailscale returned no IPv4 address. Confirm that it is running and signed in."
}

$arguments = @(".\remote_usage.py", "--host", $tailscaleIp)
if ($Port -gt 0) {
    $arguments += @("--port", $Port.ToString())
}
if ($NodeId) {
    $arguments += @("--node-id", $NodeId)
}

Write-Host "Starting the read-only Token Pulse usage endpoint on Tailscale $tailscaleIp ..."
python @arguments
