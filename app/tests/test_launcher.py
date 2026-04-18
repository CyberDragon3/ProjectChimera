from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import launcher


class _FakeWindow:
    def __init__(self) -> None:
        self.loaded_url: str | None = None
        self.loaded_html: str | None = None

    def load_url(self, url: str) -> None:
        self.loaded_url = url

    def load_html(self, html: str) -> None:
        self.loaded_html = html


def test_build_server_url_normalizes_wildcard_hosts() -> None:
    assert launcher._build_server_url("0.0.0.0", 8000) == "http://127.0.0.1:8000/"
    assert launcher._build_server_url("::", 9000) == "http://127.0.0.1:9000/"
    assert launcher._build_server_url("192.168.1.15", 7000) == "http://192.168.1.15:7000/"


def test_read_server_url_uses_config_values(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 8123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_resolve_config_path", lambda: config_path)

    assert launcher._read_server_url() == "http://127.0.0.1:8123/"


def test_import_webview_returns_none_when_missing(monkeypatch) -> None:
    def boom(_name: str):
        raise ModuleNotFoundError("webview")

    monkeypatch.setattr(launcher.importlib, "import_module", boom)
    assert launcher._import_webview() is None


def test_embedded_startup_loads_url_when_server_is_ready(monkeypatch) -> None:
    window = _FakeWindow()
    server = SimpleNamespace(exc=None)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda *_args, **_kwargs: True)

    launcher._embedded_startup(window, "http://127.0.0.1:8000/", server)

    assert window.loaded_url == "http://127.0.0.1:8000/"
    assert window.loaded_html is None


def test_embedded_startup_loads_error_html_when_server_crashes(monkeypatch) -> None:
    window = _FakeWindow()
    server = SimpleNamespace(exc=RuntimeError("boom"))
    monkeypatch.setattr(launcher, "_wait_for_server", lambda *_args, **_kwargs: False)

    launcher._embedded_startup(window, "http://127.0.0.1:8000/", server)

    assert window.loaded_url is None
    assert window.loaded_html is not None
    assert "Chimera did not finish starting" in window.loaded_html
    assert "server exited during startup" in window.loaded_html.lower()
