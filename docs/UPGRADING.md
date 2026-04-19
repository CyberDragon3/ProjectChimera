# Upgrading Chimera

## 2026-04-19 — Daemon now requires elevation

Starting with the v2 menagerie (biological veto hierarchy), Chimera
runs at the highest available privilege so the Lysosome can call
`SetSystemFileCacheSize` and Worm's `psutil.nice` reliably reaches
foreign processes.

### What you need to do

1. Close any running Chimera instance (tray → Quit, or `taskkill /IM chimera.exe`).
2. Re-run the installer, **or** re-run `scripts\install_task.ps1` from
   an elevated PowerShell. This rewrites the scheduled task with
   `-RunLevel Highest`.
3. Sign out and back in (the task fires at logon).
4. First launch will raise a UAC prompt. Accept.

Subsequent logons run silently — the stored task-scheduler credentials
carry the elevation token.

### Verifying

```powershell
Get-ScheduledTask -TaskName "ChimeraHomeostasis" | Select-Object -ExpandProperty Principal
```

`RunLevel` must be `Highest`.
