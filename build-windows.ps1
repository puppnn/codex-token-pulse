param(
    [string]$Version = "1.0.1",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist"
$ReleaseDir = Join-Path $Root "release"
$VersionFile = Join-Path $BuildDir "token-pulse-version.txt"
$Icon = Join-Path $Root "assets\token-pulse.ico"

New-Item -ItemType Directory -Force -Path $BuildDir, $DistDir, $ReleaseDir | Out-Null

& $Python (Join-Path $Root "packaging\generate_icon.py")
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

& $Python (Join-Path $Root "packaging\generate_version_info.py") `
    --version $Version `
    --output $VersionFile
if ($LASTEXITCODE -ne 0) { throw "Version resource generation failed." }

$CommonArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--noupx",
    "--distpath", $DistDir,
    "--workpath", (Join-Path $BuildDir "pyinstaller"),
    "--specpath", (Join-Path $BuildDir "spec"),
    "--icon", $Icon,
    "--version-file", $VersionFile
)

& $Python -m PyInstaller @CommonArgs `
    --windowed `
    --name TokenPulse `
    (Join-Path $Root "monitor.py")
if ($LASTEXITCODE -ne 0) { throw "TokenPulse.exe build failed." }

& $Python -m PyInstaller @CommonArgs `
    --console `
    --name TokenPulseExporter `
    (Join-Path $Root "client_usage_export.py")
if ($LASTEXITCODE -ne 0) { throw "TokenPulseExporter.exe build failed." }

$IsccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Iscc) {
    $Command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($Command) { $Iscc = $Command.Source }
}
if (-not $Iscc) {
    throw "Inno Setup 6 was not found. Install JRSoftware.InnoSetup with winget first."
}

& $Iscc "/DAppVersion=$Version" (Join-Path $Root "packaging\token-pulse.iss")
if ($LASTEXITCODE -ne 0) { throw "Installer build failed." }

$Installer = Join-Path $ReleaseDir "CodexTokenPulse-Setup-v$Version.exe"
$Checksum = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
"$Checksum  $(Split-Path -Leaf $Installer)" | Set-Content `
    -LiteralPath "$Installer.sha256" `
    -Encoding ascii

Write-Host "Built: $Installer"
Write-Host "SHA256: $Checksum"
