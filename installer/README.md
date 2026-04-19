# Chimera installer

## Build a portable zip (no extra tooling)

```powershell
.\installer\build.ps1
```

Output: `dist\Chimera-portable.zip`.

## Build a real installer (Inno Setup)

1. Install [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. Run `.\installer\build.ps1` — it detects Inno Setup automatically.
3. Output: `dist\installer\ChimeraSetup-0.1.0.exe`.

## What the installer does

- Drops Chimera into `%LOCALAPPDATA%\Programs\Chimera` (per-user, no admin).
- Registers a **Task Scheduler** entry (`Chimera`) that runs `chimera.exe --tray` at user logon.
- Adds a Start Menu entry (and optional desktop shortcut).
- On uninstall, removes the Task Scheduler entry and all files.

LibreHardwareMonitor is a separate one-time install (admin required) — see `scripts\install_lhm.ps1`. Without it, the Zebrafish thermal tier is inert but the rest of Chimera still runs.
