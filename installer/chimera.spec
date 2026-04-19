# PyInstaller spec for Chimera — builds a windowed tray app.
# Usage:  pyinstaller installer/chimera.spec --noconfirm

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repo = Path(SPECPATH).resolve().parent

block_cipher = None

# uvicorn[standard] resolves loop/protocol/lifespan implementations at runtime
# via importlib; PyInstaller can't see those edges, so we collect everything.
_uvicorn_subs = collect_submodules("uvicorn")
_anthropic_subs = collect_submodules("anthropic")

a = Analysis(
    [str(repo / "src" / "chimera" / "__main__.py")],
    pathex=[str(repo / "src")],
    binaries=[],
    datas=[
        (str(repo / "src" / "chimera" / "dashboard" / "static"), "chimera/dashboard/static"),
        (str(repo / "config" / "chimera.toml"), "config"),
    ],
    hiddenimports=[
        "chimera.tray",
        "chimera.dashboard.app",
        "chimera.sensors.cpu",
        "chimera.sensors.idle",
        "chimera.sensors.thermal",
        "chimera.sensors.window",
        "chimera.reflexes.worm",
        "chimera.reflexes.fly",
        "chimera.reflexes.zebrafish",
        "chimera.reflexes.mouse",
        "chimera.llm.gate",
        "chimera.llm.ollama_client",
        "chimera.llm.claude_client",
        # Win32 / WMI runtime (used by sensors via lazy import)
        "wmi",
        "win32com",
        "win32com.client",
        "pythoncom",
        "pywintypes",
        "win32gui",
        "win32process",
        "win32api",
        # HTTP / async stack pulled in by anthropic + ollama
        "httpx",
        "h11",
        "anyio",
        "sniffio",
        "certifi",
        # uvicorn loop / protocol / lifespan implementations
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # Tomli fallback when running on a 3.10 build env
        "tomli",
        *_uvicorn_subs,
        *_anthropic_subs,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chimera",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # tray app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="chimera",
)
