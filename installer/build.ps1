#Requires -Version 5.1
<#
    Chimera installer build orchestrator.

    1. Builds the PyInstaller onedir bundle into dist\chimera\
    2. If Inno Setup 6 is installed, compiles ChimeraSetup-<version>.exe
    3. Otherwise zips the onedir bundle into dist\Chimera-portable.zip
#>

param(
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

Write-Host "== Chimera installer build ==" -ForegroundColor Cyan

if (-not $SkipPyInstaller) {
    Write-Host "Running PyInstaller..." -ForegroundColor Yellow
    python -m pip install --quiet pyinstaller
    python -m PyInstaller "installer\chimera.spec" --noconfirm --clean
}

$distBundle = Join-Path $repo "dist\chimera"
if (-not (Test-Path $distBundle)) {
    throw "PyInstaller output not found at $distBundle"
}

$iscc = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
    Write-Host "Compiling installer with Inno Setup..." -ForegroundColor Yellow
    & $iscc "installer\chimera.iss"
    Write-Host "Installer built at dist\installer\" -ForegroundColor Green
}
else {
    Write-Host "Inno Setup not found. Producing portable zip instead." -ForegroundColor Yellow
    $zip = Join-Path $repo "dist\Chimera-portable.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $distBundle "*") -DestinationPath $zip
    Write-Host "Portable bundle: $zip" -ForegroundColor Green
    Write-Host "Install Inno Setup 6 (https://jrsoftware.org/isdl.php) and rerun to build a real installer."
}
