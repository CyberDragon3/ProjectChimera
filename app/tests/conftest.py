"""Shared pytest fixtures."""
from __future__ import annotations

import asyncio
import pytest


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
