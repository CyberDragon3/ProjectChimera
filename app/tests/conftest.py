"""Shared pytest fixtures."""
from __future__ import annotations

import asyncio
import os
import pytest


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _isolate_brain_dir(tmp_path, monkeypatch):
    """Route SpikingBrain persistence into a per-test tmp dir so tests
    never read or write the user's real learned weights."""
    monkeypatch.setenv("CHIMERA_BRAIN_DIR", str(tmp_path / "brains"))
