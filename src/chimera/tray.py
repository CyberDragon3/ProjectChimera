"""pystray system-tray integration. Runs on the main thread; daemon on a background thread."""

from __future__ import annotations

import asyncio
import threading
import webbrowser

import structlog

from chimera.config import Settings
from chimera.daemon import Chimera

log = structlog.get_logger(__name__)


def _build_icon_image():
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    img = Image.new("RGB", (64, 64), (11, 13, 15))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), outline=(82, 211, 178), width=3)
    draw.ellipse((22, 22, 42, 42), fill=(82, 211, 178))
    return img


def run_tray(settings: Settings) -> None:
    import pystray  # type: ignore[import-not-found]

    chimera = Chimera(settings)
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(chimera.run(dry_run=False))
        except Exception:
            log.exception("tray.daemon_crashed")
        finally:
            try:
                loop.close()
            except Exception:
                log.exception("tray.loop_close_failed")

    t = threading.Thread(target=_run_loop, daemon=True, name="chimera-loop")
    t.start()

    def _open_dashboard(_icon, _item) -> None:
        url = f"http://{settings.dashboard.host}:{settings.dashboard.port}/"
        webbrowser.open(url)

    def _quit(icon, _item) -> None:
        # The daemon may be inside uvicorn shutdown / sensor cancellation;
        # set the stop event then let the icon close after submitting it.
        try:
            loop.call_soon_threadsafe(chimera._stop.set)
        except RuntimeError:
            pass  # loop already closed
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", _open_dashboard),
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("chimera", _build_icon_image(), "Chimera", menu)
    icon.run()
    # Allow uvicorn + sensors a generous shutdown budget. Daemon thread
    # gets kill-on-process-exit if we exceed.
    t.join(timeout=15.0)
