# End-to-end installer build for Project Chimera.
# 1. Clean previous artefacts.
# 2. PyInstaller onedir.
# 3. If Inno Setup 6 is available -> compile ChimeraSetup.exe.
#    Otherwise -> fall back to a portable .zip so the user still has a
#    distributable artefact.
#
# Exit codes:
#   0 success (installer or portable zip produced)
#   1 PyInstaller failed
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Write-Step($msg) { Write-Host "[build] $msg" -ForegroundColor Cyan }
function Write-Ok   ($msg) { Write-Host "[build] $msg" -ForegroundColor Green }
function Write-Warn ($msg) { Write-Host "[build] $msg" -ForegroundColor Yellow }
function Write-Err  ($msg) { Write-Host "[build] $msg" -ForegroundColor Red }

# --- 1. Clean -------------------------------------------------------------
Write-Step 'Cleaning build/ dist/ installer-output/'
foreach ($d in @('build','dist','installer-output')) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Force -Path 'installer-output' | Out-Null

# --- 2. PyInstaller -------------------------------------------------------
Write-Step 'Running PyInstaller (this may take a minute)...'
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue) }
if (-not $py) {
    Write-Err 'python not found on PATH'
    exit 1
}

& $py.Source -m PyInstaller Chimera.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Err "PyInstaller failed (exit $LASTEXITCODE)"
    exit 1
}

$distDir = Join-Path $ScriptDir 'dist\Chimera'
if (-not (Test-Path $distDir)) {
    Write-Err "Expected $distDir to exist after PyInstaller but it does not."
    exit 1
}

$bytes = (Get-ChildItem -Recurse -File $distDir | Measure-Object -Property Length -Sum).Sum
$mb = [math]::Round($bytes / 1MB, 1)
Write-Ok "PyInstaller OK -- dist\Chimera = $mb MB"

# --- 3. Find Inno Setup compiler ------------------------------------------
$iscc = $null
$pathIscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($pathIscc) { $iscc = $pathIscc.Source }
if (-not $iscc) {
    foreach ($cand in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )) {
        if (Test-Path $cand) { $iscc = $cand; break }
    }
}

$produced = @()

if ($iscc) {
    Write-Step "Compiling installer with $iscc"
    & $iscc 'installer.iss'
    if ($LASTEXITCODE -ne 0) {
        Write-Err "iscc failed (exit $LASTEXITCODE) -- falling back to portable zip"
    } else {
        $setup = Join-Path $ScriptDir 'installer-output\ChimeraSetup.exe'
        if (Test-Path $setup) {
            $produced += $setup
            Write-Ok "Installer: $setup"
        }
    }
} else {
    Write-Warn 'Inno Setup 6 not found.'
    Write-Warn 'Install from: https://jrsoftware.org/isdl.php  (then re-run build.bat)'
}

# --- Portable zip fallback ------------------------------------------------
if ($produced.Count -eq 0) {
    $zip = Join-Path $ScriptDir 'installer-output\Chimera-portable.zip'
    Write-Step "Creating portable zip: $zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Compress-Archive -Path (Join-Path $distDir '*') -DestinationPath $zip -CompressionLevel Optimal
    if (Test-Path $zip) {
        $produced += $zip
        Write-Ok "Portable zip: $zip"
    } else {
        Write-Err 'Compress-Archive did not produce the zip.'
        exit 1
    }
}

Write-Host ''
Write-Ok 'Build complete. Artifacts:'
foreach ($p in $produced) { Write-Host "  $p" }
exit 0
