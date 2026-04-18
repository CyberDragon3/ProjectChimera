# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Project Chimera (onedir, windowed)."""

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve()
APP_DIR = ROOT / "app"
STATIC_DIR = APP_DIR / "dashboard" / "static"
CONFIG_FILE = APP_DIR / "config.yaml"
ICON_FILE = ROOT / "assets" / "chimera.ico"

# --- Data files ------------------------------------------------------------
datas = []
binaries = []

# Whole static tree -> app/dashboard/static/*
if STATIC_DIR.exists():
    for p in STATIC_DIR.rglob("*"):
        if p.is_file():
            rel_parent = p.parent.relative_to(ROOT)
            datas.append((str(p), str(rel_parent).replace("\\", "/")))

# Runtime config
if CONFIG_FILE.exists():
    datas.append((str(CONFIG_FILE), "app"))

# --- Hidden imports --------------------------------------------------------
hiddenimports = [
    # uvicorn internal loaders that PyInstaller misses
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # third-party dynamics
    "mss",
    "mss.windows",
    "pynput",
    "pynput.mouse",
    "pynput.mouse._win32",
    "pynput.keyboard",
    "pynput.keyboard._win32",
    "httpx",
    "yaml",
    "webview",
    "numpy.core._methods",
    "numpy.core._dtype_ctypes",
    # our own submodules (belt + suspenders vs dynamic imports inside app.main)
    "app",
    "app.main",
    "app.actions",
    "app.contracts",
    "app.event_bus",
    "app.tier1_executive",
    "app.tools",
    "app.setup_check",
    "app.tier2_translation",
    "app.tier3_reflex",
    "app.tier3_reflex.base",
    "app.tier3_reflex.fly",
    "app.tier3_reflex.worm",
    "app.tier3_reflex.mouse",
    "app.tier3_reflex.neural",
    "app.dashboard",
    "app.dashboard.server",
]

# Sweep FastAPI / Starlette / uvicorn submodules aggressively — their
# module graph loves to play hide-and-seek with PyInstaller.
for pkg in ("uvicorn", "starlette", "fastapi", "anyio", "h11", "httptools", "webview"):
    try:
        hiddenimports.extend(collect_submodules(pkg))
    except Exception:
        pass

# Collect their data files too (e.g. starlette templates)
for pkg in ("starlette", "fastapi", "webview"):
    try:
        datas.extend(collect_data_files(pkg))
    except Exception:
        pass

# pywebview ships Windows interop DLLs under webview/lib. Keep them optional
# so browser fallback still builds even if the dependency is absent locally.
try:
    binaries.extend(collect_dynamic_libs("webview"))
except Exception:
    pass

# --- Excludes: heavy deps we never use -------------------------------------
# Chimera only needs: fastapi, uvicorn, httpx, numpy, mss, pynput, pyyaml,
# psutil. Anything else in site-packages is bloat — reject aggressively.
excludes = [
    # GUI / plotting
    "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pygame", "kivy",
    # Data science heavies
    "pandas", "scipy", "sklearn", "statsmodels", "sympy", "numba",
    "numba.cuda", "numba.core", "xgboost", "lightgbm", "catboost",
    # Deep learning
    "torch", "torchvision", "torchaudio", "tensorflow", "jax", "flax",
    "keras", "bitsandbytes", "triton", "accelerate", "deepspeed",
    "xformers", "diffusers", "sentence_transformers", "transformers",
    "safetensors", "peft", "trl", "unsloth", "vllm",
    # HF + data loaders
    "datasets", "huggingface_hub", "evaluate", "tokenizers", "timm",
    "kernels",
    # Notebook / dev tools
    "IPython", "notebook", "jupyter", "jupyterlab", "ipywidgets",
    "qtconsole", "nbconvert", "nbformat", "black", "isort",
    "pytest", "pytest_asyncio",
    # NLP stacks we never import
    "nltk", "spacy", "gensim",
    # Crypto / heavy parsers
    "cryptography", "pycryptodome", "Crypto", "cryptography.hazmat",
    "lxml", "lxml.etree", "lxml.isoschematron", "lxml.objectify",
    # Imaging
    "PIL", "pillow", "cv2", "skimage", "imageio", "OpenEXR",
    # Cloud SDKs
    "boto3", "botocore", "google", "googleapis_common_protos",
    "azure", "grpc", "grpcio",
    # Misc that shows up transitively
    "pyarrow", "duckdb", "polars", "mlflow", "wandb", "clickhouse_driver",
    "pymongo", "redis", "aiohttp_retry",
]

block_cipher = None

a = Analysis(
    ["app/launcher.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = str(ICON_FILE) if ICON_FILE.exists() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Chimera",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Chimera",
)
