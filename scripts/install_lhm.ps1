#Requires -Version 5.1
<#
Installs LibreHardwareMonitor into C:\Tools\LibreHardwareMonitor and registers
its WMI provider so the Zebrafish thermal sensor can read CPU/GPU temps.
Requires admin because LHM reads MSR registers.

Run in an elevated PowerShell:
    Set-ExecutionPolicy -Scope Process Bypass
    .\install_lhm.ps1
#>

$url = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor-net472.zip"
$dest = "C:\Tools\LibreHardwareMonitor"

New-Item -ItemType Directory -Force -Path $dest | Out-Null
$zip = Join-Path $env:TEMP "lhm.zip"
Write-Host "Downloading LibreHardwareMonitor..."
Invoke-WebRequest -Uri $url -OutFile $zip
Expand-Archive -Path $zip -DestinationPath $dest -Force
Remove-Item $zip

Write-Host "Installed to $dest. Launch LibreHardwareMonitor.exe once as admin,"
Write-Host "enable Options -> Run on Windows Startup, Options -> WMI Provider."
