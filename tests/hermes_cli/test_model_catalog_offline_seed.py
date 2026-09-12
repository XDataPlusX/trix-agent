"""Offline model-catalog resolution on the actual client layout.

``website/`` is excluded from the client checkout (sparse-checkout in
``scripts/install.sh`` — see Task 7/9 of the installer/updater plan): the
docs-site source never reaches a real Trix Agent VPS. Before Task 11,
``seed_cache_from_checkout()`` read its local fallback from
``website/static/api/model-catalog.json`` — a path that simply does not
exist on a client machine, so the offline model-catalog resolution path was
silently broken from the moment ``website/`` was excluded.

The existing test suite never caught this because, at the time ``website/``
was excluded, the fixture/manifest file still lived in a directory that
*did* ship (the fixtures ran against a full dev checkout, not the pruned
client layout). This file exercises the CLIENT layout specifically: a
synthetic checkout containing ONLY ``assets/api/model-catalog.json`` with no
``website/`` directory anywhere in it, and no network access, and asserts
the catalog still resolves.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + reset any module-level catalog cache per test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    from hermes_cli import model_catalog

    importlib.reload(model_catalog)
    yield home
    model_catalog.reset_cache()


def _make_client_checkout(root: Path, manifest_text: str | None) -> None:
    """Build a synthetic checkout that matches what a real client sees.

    Deliberately does NOT create a ``website/`` directory at all -- not an
    empty one, not one missing just the JSON file. On a real client
    checkout the whole directory is absent, and a test that leaves it
    present (even empty) wouldn't reproduce the bug the sparse-checkout
    exclusion introduced.
    """
    (root / "assets" / "api").mkdir(parents=True)
    if manifest_text is not None:
        (root / "assets" / "api" / "model-catalog.json").write_text(
            manifest_text, encoding="utf-8"
        )
    assert not (root / "website").exists(), "test setup bug: website/ must be absent"


def test_offline_seed_file_lives_outside_website():
    """Guard against a regression that puts the fallback back under website/."""
    seed = REPO_ROOT / "assets" / "api" / "model-catalog.json"
    assert seed.exists(), "offline model-catalog fallback is missing from assets/api/"
    assert not (REPO_ROOT / "website" / "static" / "api" / "model-catalog.json").exists(), (
        "the old website/ copy should have been moved, not duplicated"
    )


def test_client_layout_without_website_resolves_catalog_offline(tmp_path, isolated_home, monkeypatch):
    """End-to-end: no network, no website/ directory anywhere -- the catalog
    still resolves from the on-disk seed file, exactly like a real client.

    Before Task 11 this failed: ``seed_cache_from_checkout`` looked for
    ``website/static/api/model-catalog.json``, which this checkout (by
    construction) does not have, so it returned False and get_catalog()
    fell through to an empty dict.
    """
    from hermes_cli import model_catalog

    manifest_text = (REPO_ROOT / "assets" / "api" / "model-catalog.json").read_text(
        encoding="utf-8"
    )
    checkout = tmp_path / "client-checkout"
    _make_client_checkout(checkout, manifest_text)

    # No network: any attempt to actually open a socket blows up loudly
    # instead of silently succeeding, so a regression that falls through to
    # a live fetch would fail this test rather than pass it by accident.
    def _no_network(*args, **kwargs):
        raise AssertionError("offline path must not touch the network")

    with patch("urllib.request.urlopen", side_effect=_no_network):
        seeded = model_catalog.seed_cache_from_checkout(checkout)
        assert seeded is True, "seeding from the client-layout checkout must succeed"

        resolved = model_catalog.get_catalog()

    expected = json.loads(manifest_text)
    assert resolved == expected
    assert resolved.get("providers", {}).get("nous", {}).get("models"), (
        "resolved catalog must carry real provider data, not an empty stub"
    )


def test_seed_from_checkout_is_non_fatal_when_the_file_is_missing(tmp_path, isolated_home):
    """Missing fallback file -> False, no crash."""
    from hermes_cli import model_catalog

    checkout = tmp_path / "client-checkout"
    checkout.mkdir()
    assert model_catalog.seed_cache_from_checkout(checkout) is False


def test_seed_from_checkout_is_non_fatal_when_the_file_is_empty(tmp_path, isolated_home):
    """Empty (0-byte) fallback file -> False, no crash."""
    from hermes_cli import model_catalog

    checkout = tmp_path / "client-checkout"
    _make_client_checkout(checkout, "")
    assert model_catalog.seed_cache_from_checkout(checkout) is False


def test_seed_from_checkout_is_non_fatal_when_the_file_is_malformed(tmp_path, isolated_home):
    """Malformed JSON -> False, no crash."""
    from hermes_cli import model_catalog

    checkout = tmp_path / "client-checkout"
    _make_client_checkout(checkout, "{not valid json at all")
    assert model_catalog.seed_cache_from_checkout(checkout) is False


def test_seed_from_checkout_is_non_fatal_when_the_file_is_schema_invalid(tmp_path, isolated_home):
    """Well-formed JSON that fails manifest schema validation -> False, no crash."""
    from hermes_cli import model_catalog

    checkout = tmp_path / "client-checkout"
    _make_client_checkout(checkout, json.dumps({"unrelated": "shape"}))
    assert model_catalog.seed_cache_from_checkout(checkout) is False


def test_fresh_install_with_no_network_resolves_the_default_model(isolated_home):
    """The acceptance criterion this file is named for: a FRESH install
    (empty HERMES_HOME, nothing ever cached, no network reachable at all)
    must still resolve a default model.

    ``seed_cache_from_checkout()`` was previously only wired into
    ``hermes update``'s two call sites (git-pull and zip-unpack paths) --
    never into a fresh install. install.sh's ``copy_config_templates()``
    now calls it once, right after the repo is cloned, against the real
    checkout (not a synthetic one). This test invokes that exact call --
    ``seed_cache_from_checkout(REPO_ROOT)`` -- against a brand-new empty
    HERMES_HOME to prove the wiring actually closes the gap, using
    ``get_default_model_from_cache()``: the hot-path accessor that agent
    build / gateway session setup call, which by contract never touches
    the network and returns None when nothing is cached.
    """
    from hermes_cli import model_catalog

    # Before any seeding: a genuinely fresh install has nothing cached yet.
    assert model_catalog.get_default_model_from_cache("openrouter") is None

    def _no_network(*args, **kwargs):
        raise AssertionError("fresh-install offline path must not touch the network")

    with patch("urllib.request.urlopen", side_effect=_no_network):
        seeded = model_catalog.seed_cache_from_checkout(REPO_ROOT)
        assert seeded is True, "fresh-install seed from the real checkout must succeed"

        resolved = model_catalog.get_default_model_from_cache("openrouter")

    assert resolved, (
        "a fresh install with no network must resolve a default model from "
        "the seeded disk cache, not fall through to None"
    )


def test_network_fetch_path_never_reaches_nousresearch(isolated_home):
    """Intercept the actual outbound request (not a source-text scan) and
    prove the URL it hits never mentions nousresearch, under any outcome.
    """
    from hermes_cli import model_catalog

    seen_urls: list[str] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"version": 1, "providers": {"openrouter": {"models": []}}}
            ).encode()

    def _fake_urlopen(request, timeout=None):
        seen_urls.append(request.full_url)
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        model_catalog.get_catalog(force_refresh=True)

    assert seen_urls, "expected at least one network attempt during force_refresh"
    assert all("nousresearch" not in u.lower() for u in seen_urls), seen_urls
