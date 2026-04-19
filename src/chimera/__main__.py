"""CLI entry point — `python -m chimera`."""

from __future__ import annotations

import argparse
import asyncio

from chimera import logging as log_cfg
from chimera.config import load_default
from chimera.daemon import Chimera


def main() -> None:
    parser = argparse.ArgumentParser(prog="chimera")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Start the daemon but skip binding sensors/reflexes — heartbeat only.",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Run with a system-tray icon. Foreground loop; quit via tray menu.",
    )
    args = parser.parse_args()

    settings = load_default()
    log_cfg.configure(settings.logging.level, settings.logging.format)

    if args.tray:
        from chimera.tray import run_tray

        run_tray(settings)
        return

    chimera = Chimera(settings)
    asyncio.run(chimera.run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
