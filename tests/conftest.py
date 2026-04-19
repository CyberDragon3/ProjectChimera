"""Shared test fixtures."""

from __future__ import annotations

import pytest

from chimera import logging as log_cfg


@pytest.fixture(autouse=True, scope="session")
def _configure_logging() -> None:
    log_cfg.configure(level="WARNING", fmt="console")
