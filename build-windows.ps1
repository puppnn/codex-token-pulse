param(
    [string]$Version = "1.0.2",
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

$PythonPrefix = (& $Python -c "import sys; print(sys.prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PythonPrefix) {
    throw "Unable to resolve the selected Python environment."
}
$PythonPathEntries = @(
    $PythonPrefix,
    (Join-Path $PythonPrefix "Library\mingw-w64\bin"),
    (Join-Path $PythonPrefix "Library\usr\bin"),
    (Join-Path $PythonPrefix "Library\bin"),
    (Join-Path $PythonPrefix "Scripts"),
    (Join-Path $PythonPrefix "bin"),
    (Join-Path $PythonPrefix "DLLs")
) | Where-Object { Test-Path -LiteralPath $_ }
$env:Path = (($PythonPathEntries + @($env:Path)) -join [IO.Path]::PathSeparator)

$TclLibraryCandidates = @(
    (Join-Path $PythonPrefix "tcl\tcl8.6"),
    (Join-Path $PythonPrefix "Library\lib\tcl8.6")
)
$TkLibraryCandidates = @(
    (Join-Path $PythonPrefix "tcl\tk8.6"),
    (Join-Path $PythonPrefix "Library\lib\tk8.6")
)
$TclLibrary = $TclLibraryCandidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ "init.tcl") } | Select-Object -First 1
$TkLibrary = $TkLibraryCandidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ "tk.tcl") } | Select-Object -First 1
if (-not $TclLibrary -or -not $TkLibrary) {
    throw "The selected Python environment does not contain a complete Tcl/Tk library."
}
$env:TCL_LIBRARY = $TclLibrary
$env:TK_LIBRARY = $TkLibrary

$TclRuntimeVersion = (& $Python -c "import tkinter; t=tkinter.Tcl(); print(t.eval('info patchlevel'))").Trim()
$TclRequirementLine = Get-Content -LiteralPath (Join-Path $TclLibrary "init.tcl") |
    Where-Object { $_ -match '^package require -exact Tcl\s+([0-9.]+)' } |
    Select-Object -First 1
$TclVersionMatch = [regex]::Match(
    [string]$TclRequirementLine,
    '([0-9]+\.[0-9]+\.[0-9]+)'
)
if (-not $TclVersionMatch.Success) {
    throw "Unable to determine the Tcl script version."
}
$TclScriptVersion = $TclVersionMatch.Groups[1].Value
if ($TclRuntimeVersion -ne $TclScriptVersion) {
    throw "Tcl/Tk mismatch before build: runtime $TclRuntimeVersion, scripts $TclScriptVersion."
}

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

$SmokeDataDir = Join-Path $BuildDir "smoke-data"
New-Item -ItemType Directory -Force -Path $SmokeDataDir | Out-Null
$PreviousDataDir = $env:TOKEN_PULSE_DATA_DIR
$env:TOKEN_PULSE_DATA_DIR = $SmokeDataDir
try {
    $Smoke = Start-Process `
        -FilePath (Join-Path $DistDir "TokenPulse.exe") `
        -ArgumentList "--smoke-test" `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($Smoke.ExitCode -ne 0) {
        throw "Packaged Tk smoke test failed with exit code $($Smoke.ExitCode)."
    }
} finally {
    $env:TOKEN_PULSE_DATA_DIR = $PreviousDataDir
}

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
