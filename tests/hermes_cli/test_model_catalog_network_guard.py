"""Сторож обязан уметь краснеть и уметь открываться маркером.

Первый тест бьёт по настоящему публичному пути (``get_catalog``), а не по
внутренней ``_fetch_manifest`` -- тесты, которые перезагружают
``hermes_cli.model_catalog`` (``importlib.reload``), тихо возвращают
настоящую ``_fetch_manifest`` на место, и атрибутный сторож перестаёт
существовать. Проверяем результатом (ни один реальный сокет не тронут), а
не типом исключения -- сторож вправе поменять механизм, инвариант должен
пережить рефакторинг.

Второй тест доказывает, что ``@pytest.mark.allow_catalog_fetch``
действительно снимает сторож, а не просто зарегистрирован и забыт.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest

from hermes_cli import model_catalog
from tests.conftest import _ALLOW_CATALOG_FETCH_MARK, CatalogNetworkGuardViolation

# Captured at module import time, before any test's autouse fixtures have
# run in this subprocess -- the one point where ``urllib.request.urlopen``
# is guaranteed to still be the real, unpatched function. Used below to
# prove the marker keeps the guard from touching this attribute at all.
_REAL_URLOPEN = urllib.request.urlopen


def test_get_catalog_blocks_the_real_network_and_names_the_escape_hatch(monkeypatch):
    """``get_catalog(force_refresh=True)`` must never reach a real socket.

    ``CatalogNetworkGuardViolation`` is deliberately поглощаемое (a
    ``RuntimeError`` subclass, see the class docstring in ``conftest.py``)
    so product code's honest ``except Exception`` offline fallback keeps
    working -- ``get_catalog()`` itself documents that it never raises and
    falls back to ``{}``/disk cache on any fetch failure. So this test
    can no longer assert "the exception escapes uncaught" (it doesn't, on
    purpose); the invariant that matters -- and the one that actually
    proves the guard did its job -- is that the real network was never
    touched. Visibility into the swallowed violation is asserted via the
    ``warnings.warn`` the guard fires before raising.
    """
    real_create_connection = socket.create_connection
    attempts: list = []

    def _counting_create_connection(*args, **kwargs):
        attempts.append(args)
        return real_create_connection(*args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", _counting_create_connection)

    with pytest.warns(UserWarning) as records:
        result = model_catalog.get_catalog(force_refresh=True)

    assert attempts == [], (
        "каталог моделей долетел до настоящего сокета -- сторож не "
        f"перехватил запрос до сетевого уровня: {attempts!r}"
    )
    # get_catalog() never raises by contract; a blocked fetch degrades to
    # the same "no catalog data" result as an honest 404.
    assert result == {}

    messages = [str(r.message) for r in records]
    assert any("model_catalog" in m for m in messages)
    assert any("мок" in m or "mock" in m for m in messages)
    assert any(_ALLOW_CATALOG_FETCH_MARK in m for m in messages)


@pytest.mark.allow_catalog_fetch
def test_allow_catalog_fetch_marker_reopens_the_real_path(monkeypatch):
    """With the marker, the guard must not touch ``urlopen`` at all -- the
    fixture's own opt-out check must fire before it ever patches anything.
    Verified two ways: identity (the guard didn't wrap the function), and
    an end-to-end fetch through a faked server (never real network)."""
    assert urllib.request.urlopen is _REAL_URLOPEN, (
        f"@pytest.mark.{_ALLOW_CATALOG_FETCH_MARK} must stop the guard "
        "from installing its urlopen wrapper in the first place"
    )

    import json

    manifest = {
        "version": 1,
        "providers": {
            "openrouter": {"models": [{"id": "vendor/model", "description": "d"}]}
        },
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(manifest).encode()

    calls: list = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    result = model_catalog.get_catalog(force_refresh=True)

    assert calls, "the fake server was never reached through the real code path"
    assert result["providers"]["openrouter"]["models"][0]["id"] == "vendor/model"
