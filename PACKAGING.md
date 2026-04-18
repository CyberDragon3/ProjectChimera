# Packaging Project Chimera for Windows

This produces a double-clickable installer (`ChimeraSetup.exe`) plus a
PyInstaller onedir bundle as the payload. If Inno Setup is not installed,
the build falls back to a portable `.zip` so you still get a distributable
artifact.

## Prerequisites

- **Python 3.11+** on PATH
- **PyInstaller** in the same Python env: `pip install -r requirements.txt pyinstaller`
- **Inno Setup 6** (optional, but required for the `.exe` installer):
  https://jrsoftware.org/isdl.php
- **Ollama** (runtime prerequisite for users, not for building):
  https://ollama.com/download

We intentionally do **not** bundle Ollama. It is a large native binary with
its own updater and model store, and shipping it ourselves would (a) blow
up installer size from ~100 MB to multiple GB, (b) fork model-management
responsibility away from the upstream vendor, and (c) complicate licensing.
The installer detects whether Ollama is present and shows a one-time
friendly reminder if not — install is non-blocking.

## Build

```
.\build.bat
```

Under the hood this calls `build.ps1` which:

1. Wipes `build/`, `dist/`, and `installer-output/`.
2. Runs `python -m PyInstaller Chimera.spec --noconfirm`.
3. Looks for `iscc.exe` on PATH and in the two standard install dirs.
4. If found, compiles `installer-output\ChimeraSetup.exe`.
   If not, creates `installer-output\Chimera-portable.zip`.

## Startup UX

The launcher now prefers an embedded desktop window via `pywebview`, with the
dashboard loaded from Chimera's local FastAPI server once it is ready. This
keeps first-run onboarding inside the app instead of jumping straight to the
system browser.

If `pywebview` cannot be imported or fails to initialize on a machine, the
launcher falls back to the previous browser flow automatically. Frozen builds
still work because the PyInstaller spec collects `webview` package data and
its optional Windows interop DLLs when that dependency is installed.

## Artifacts

| Artifact                                     | When produced                |
|----------------------------------------------|------------------------------|
| `installer-output\ChimeraSetup.exe`          | Inno Setup installed         |
| `installer-output\Chimera-portable.zip`      | Inno Setup missing (fallback)|
| `dist\Chimera\Chimera.exe`                   | Always (the raw onedir)      |

## Test locally

After a successful build:

```
dist\Chimera\Chimera.exe
```

The launcher should open the UI for you. If the embedded shell falls back to
the browser, it will use `http://127.0.0.1:8000/`. Logs are written to
`%APPDATA%\Chimera\chimera.log` with rotation at 1 MB.

On a normal install, the UI should appear inside the Chimera desktop window.
If the embedded shell is unavailable, the launcher will open the same local
URL in the default browser instead.

To test the installer itself, run `installer-output\ChimeraSetup.exe`.
Default install is per-user into `%LOCALAPPDATA%\Programs\Chimera`.

## Clean

```
Remove-Item -Recurse -Force build,dist,installer-output
```

## Updating the Ollama prerequisite check

The check lives in `installer.iss` under `[Code] > OllamaInstalled()`. It
currently looks for `ollama.exe` in:

- `{localappdata}\Programs\Ollama\ollama.exe`
- `{pf}\Ollama\ollama.exe`
- `{pf32}\Ollama\ollama.exe`

If Ollama ships a new default install location, add it to that function
and rerun `.\build.bat`.

## Editing the runtime config

`app\config.yaml` is bundled inside the frozen app, but the launcher also
looks for `config.yaml` **next to `Chimera.exe`** first. Drop a customised
copy there to override defaults without rebuilding.
