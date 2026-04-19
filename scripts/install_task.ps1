#Requires -Version 5.1
<#
Registers a Windows Task Scheduler entry that launches Chimera at user logon.
Runs pythonw.exe -m chimera as the current user with no console window.
#>

param(
    [string]$TaskName = "Chimera",
    [string]$PythonW = ""
)

if (-not $PythonW) {
    # -ErrorAction belongs on Get-Command, not on Select-Object; otherwise a
    # missing pythonw.exe raises a terminating error before the if-check runs.
    $resolved = Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
    if ($resolved) { $PythonW = $resolved }
}

if (-not $PythonW) {
    Write-Error "pythonw.exe not found in PATH. Install Python 3.11+ or pass -PythonW."
    exit 1
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$action = New-ScheduledTaskAction -Execute $PythonW -Argument "-m chimera" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

Write-Host "Registered Task Scheduler entry '$TaskName' for user $env:USERNAME." -ForegroundColor Green
Write-Host "Chimera will start on next logon, or run:  Start-ScheduledTask '$TaskName'" -ForegroundColor Green
