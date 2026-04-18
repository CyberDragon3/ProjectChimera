"""Windows-friendly launcher for Project Chimera (frozen/windowed).

Responsibilities:
    1. Resolve the config path (next to the executable when frozen, otherwise
       fall back to the package's bundled config).
    2. Start `app.main.run_app()` in a background thread via asyncio.
    3. Prefer the user's default browser for the localhost UI because browser
       speech APIs are more reliable there than inside the embedded shell.
    4. Allow the embedded shell as an opt-in fallback when
       `CHIMERA_EMBEDDED=1` is set.
    5. Log everything to %APPDATA%/Chimera/chimera.log (rotating at 1 MB).
    6. Keep the process alive until the server exits, the window closes, or
       SIGINT is received.

Because PyInstaller freezes this as a `--windowed` app on Windows, this
module never prints to stdout. All output goes through the rotating file
logger.
"""
from __future__ import annotations

import asyncio
import html
import importlib
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

WINDOW_TITLE = "Jarvis | Project Chimera"
BROWSER_STARTUP_TIMEOUT_S = 10.0
EMBEDDED_STARTUP_TIMEOUT_S = 60.0
EMBEDDED_WINDOW_SIZE = (1280, 900)
EMBEDDED_WINDOW_MIN_SIZE = (960, 640)


def _log_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        base = Path(appdata) / "Chimera"
    else:  # non-Windows fallback for tests / local dev
        base = Path.home() / ".chimera"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _configure_logging() -> logging.Logger:
    log_path = _log_dir() / "chimera.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    return logging.getLogger("chimera.launcher")


log = logging.getLogger("chimera.launcher")


def _resolve_config_path() -> Path:
    """Resolve the bundled-defaults config path.

    This always points at the read-only defaults. ``app.main.load_config``
    is responsible for layering the writable per-user config
    (``%APPDATA%/Chimera/config.yaml``) on top via a deep merge, so partial
    user files never wipe out other sections.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        external = exe_dir / "config.yaml"
        if external.exists():
            return external
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        bundled = meipass / "app" / "config.yaml"
        if bundled.exists():
            return bundled
    return Path(__file__).parent / "config.yaml"


def _read_user_config() -> dict:
    """Best-effort read of the writable per-user config, for URL resolution.
    Returns ``{}`` when the file is missing or malformed.
    """
    try:
        from app.setup_check import user_config_path  # type: ignore
        path = user_config_path()
        if not path.exists():
            return {}
        import yaml  # type: ignore
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}


def _patch_main_config_path() -> None:
    """Point `app.main.CONFIG_PATH` at the resolved runtime config."""
    try:
        from app import main as app_main  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to import app.main: %s", exc)
        raise

    cfg = _resolve_config_path()
    app_main.CONFIG_PATH = cfg
    log.info("using config: %s", cfg)


def _normalize_server_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _build_server_url(host: str, port: int) -> str:
    return f"http://{_normalize_server_host(host)}:{int(port)}/"


def _read_server_url() -> str:
    try:
        import yaml  # type: ignore

        with _resolve_config_path().open("r", encoding="utf-8") as handle:
            base = yaml.safe_load(handle) or {}
        patch = _read_user_config()
        server_cfg = {**(base.get("server") or {}), **(patch.get("server") or {})}
        host = server_cfg.get("host", "127.0.0.1")
        port = int(server_cfg.get("port", 8000))
    except Exception:
        host, port = "127.0.0.1", 8000
    return _build_server_url(host, port)


def _wait_for_server(url: str, timeout_s: float = BROWSER_STARTUP_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            ConnectionError,
            OSError,
        ):
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("probe error: %s", exc)
        time.sleep(0.25)
    return False


def _embedded_loading_html(url: str) -> str:
    safe_url = html.escape(url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{WINDOW_TITLE}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e2e8f0;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at top, rgba(34, 197, 94, 0.18), transparent 45%),
        linear-gradient(180deg, #111827 0%, #020617 100%);
    }}
    main {{
      width: min(560px, calc(100vw - 48px));
      padding: 32px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 20px;
      background: rgba(15, 23, 42, 0.88);
      box-shadow: 0 28px 80px rgba(2, 6, 23, 0.5);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 1.85rem;
    }}
    p {{
      margin: 0 0 16px;
      line-height: 1.5;
      color: #cbd5e1;
    }}
    code {{
      font-family: Consolas, monospace;
      color: #bfdbfe;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Starting Jarvis</h1>
    <p>The local Jarvis surface is booting inside this window.</p>
    <p>If startup takes longer than expected, the app will keep waiting for <code>{safe_url}</code>.</p>
  </main>
</body>
</html>
"""


def _embedded_error_html(url: str, message: str) -> str:
    safe_url = html.escape(url, quote=True)
    safe_message = html.escape(message)
    safe_log_path = html.escape(str(_log_dir() / "chimera.log"), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{WINDOW_TITLE}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Segoe UI", sans-serif;
      background: #fff7ed;
      color: #431407;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: linear-gradient(180deg, #fff7ed 0%, #ffedd5 100%);
    }}
    main {{
      width: min(600px, calc(100vw - 48px));
      padding: 32px;
      border: 1px solid rgba(194, 65, 12, 0.18);
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 24px 60px rgba(154, 52, 18, 0.16);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 1.8rem;
    }}
    p {{
      margin: 0 0 14px;
      line-height: 1.5;
    }}
    code {{
      font-family: Consolas, monospace;
      color: #9a3412;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Jarvis did not finish starting</h1>
    <p>{safe_message}</p>
    <p>Expected UI endpoint: <code>{safe_url}</code></p>
    <p>Startup logs: <code>{safe_log_path}</code></p>
  </main>
</body>
</html>
"""


def _import_webview() -> Any | None:
    try:
        return importlib.import_module("webview")
    except Exception as exc:  # noqa: BLE001
        log.info("embedded shell unavailable; falling back to browser: %s", exc)
        return None


def _prefer_embedded_shell() -> bool:
    value = (os.environ.get("CHIMERA_EMBEDDED") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class _ServerThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="chimera-server", daemon=True)
        self.exc: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:
        try:
            from app.main import run_app  # type: ignore

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_app())
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        except BaseException as exc:  # noqa: BLE001
            self.exc = exc
            log.exception("server thread crashed: %s", exc)

    def request_stop(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(
                lambda: [task.cancel() for task in asyncio.all_tasks(loop)]
            )
        except Exception:
            pass


def _embedded_startup(window: Any, url: str, server: _ServerThread) -> None:
    ready = _wait_for_server(url, timeout_s=EMBEDDED_STARTUP_TIMEOUT_S)
    if ready:
        log.info("server is up; loading embedded shell")
        try:
            window.load_url(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("embedded shell failed to load %s: %s", url, exc)
        return

    if server.exc is not None:
        message = "The Chimera server exited during startup."
    else:
        message = "The Chimera server did not become ready before the embedded startup timeout."

    log.warning(message)
    try:
        window.load_html(_embedded_error_html(url, message))
    except Exception as exc:  # noqa: BLE001
        log.warning("embedded shell failed to show startup error page: %s", exc)


def _open_embedded_shell(url: str, server: _ServerThread) -> bool:
    webview = _import_webview()
    if webview is None:
        return False

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            html=_embedded_loading_html(url),
            width=EMBEDDED_WINDOW_SIZE[0],
            height=EMBEDDED_WINDOW_SIZE[1],
            min_size=EMBEDDED_WINDOW_MIN_SIZE,
        )
        webview.start(
            func=_embedded_startup,
            args=(window, url, server),
            debug=False,
            private_mode=False,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("embedded shell failed; falling back to browser: %s", exc, exc_info=True)
        return False


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("webbrowser.open failed: %s", exc)


def main() -> int:
    global log
    log = _configure_logging()
    log.info("Chimera launcher starting (frozen=%s)", getattr(sys, "frozen", False))

    try:
        _patch_main_config_path()
    except Exception:
        return 1

    server = _ServerThread()
    server.start()

    url = _read_server_url()
    stopping = threading.Event()

    def _request_shutdown(*_args: object) -> None:
        log.info("shutdown requested")
        stopping.set()
        server.request_stop()

    try:
        signal.signal(signal.SIGINT, _request_shutdown)
    except Exception:
        pass

    embedded_ran = False
    if _prefer_embedded_shell():
        log.info("CHIMERA_EMBEDDED is set; attempting embedded shell")
        embedded_ran = _open_embedded_shell(url, server)
    else:
        log.info("defaulting to the system browser for full voice support")

    if embedded_ran:
        log.info("embedded shell closed; stopping server")
        _request_shutdown()
        server.join(timeout=5.0)
    else:
        log.info("waiting for server at %s", url)
        ready = _wait_for_server(url, timeout_s=BROWSER_STARTUP_TIMEOUT_S)
        if ready:
            log.info("server is up; opening browser")
            _open_browser(url)
        else:
            log.warning("server did not become ready within %.1fs; continuing anyway", BROWSER_STARTUP_TIMEOUT_S)

        try:
            while server.is_alive() and not stopping.is_set():
                server.join(timeout=0.5)
        except KeyboardInterrupt:
            _request_shutdown()
            server.join(timeout=5.0)

    if server.is_alive():
        log.warning("server thread is still alive while launcher exits")

    if server.exc is not None:
        log.error("server exited with exception: %s", server.exc)
        return 2

    log.info("Chimera launcher exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
