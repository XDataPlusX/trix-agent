"""Machine-readable form of the three installer preflight results.

``scripts/install.sh`` still parses each check's two-line "OK"/"WARN" +
message text via a short Python subprocess -- unchanged, see
tests/test_install_sh_docker_preflight.py,
tests/test_install_sh_chromium_preflight.py,
tests/test_install_sh_ddgs_preflight.py. ``to_dict()`` is the additional,
JSON-serializable path a programmatic caller (the future spec 12 support
page, docs/product/PROMPT-spec15-support-page.md) can use instead of
re-deriving structure from that text.

The contract under test: all three of DockerPreflightResult,
BrowserPreflightResult, and DdgsPreflightResult expose the SAME shape --
``check`` (a stable per-module identifier), ``ok``, ``message``, ``details``
-- so a caller can iterate the three results uniformly rather than
special-casing each module's field names.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.docker_preflight import DockerPreflightResult
from hermes_cli.browser_preflight import BrowserPreflightResult
from hermes_cli.search_preflight import DdgsPreflightResult

_COMMON_KEYS = {"check", "ok", "message", "details"}


class TestUniformShapeAcrossModules:
    def test_all_three_result_types_produce_the_same_key_set(self):
        docker = DockerPreflightResult(
            binary_found=True, daemon_responds=True, usable_without_sudo=True, message="ok"
        ).to_dict()
        browser = BrowserPreflightResult(ok=True, message="ok").to_dict()
        ddgs = DdgsPreflightResult(ok=True, message="ok").to_dict()

        # Required-keys check, not a shape freeze: a future field (a repair
        # hint, a check-ordering index, ...) added to one module's to_dict()
        # must not fail this test just for existing -- only a MISSING
        # required key should. issubset (not ==) is the behavioral contract:
        # every caller iterating these three uniformly needs check/ok/
        # message/details to be there, not that nothing else ever is.
        assert _COMMON_KEYS.issubset(docker.keys())
        assert _COMMON_KEYS.issubset(browser.keys())
        assert _COMMON_KEYS.issubset(ddgs.keys())

    def test_check_identifier_is_distinct_per_module(self):
        """A caller aggregating all three checks needs a stable way to tell
        them apart (e.g. to route "which one needs a fix" logic) -- the
        `check` field is that identifier, so it must never collide."""
        docker = DockerPreflightResult(
            binary_found=True, daemon_responds=True, usable_without_sudo=True, message="ok"
        ).to_dict()["check"]
        browser = BrowserPreflightResult(ok=True, message="ok").to_dict()["check"]
        ddgs = DdgsPreflightResult(ok=True, message="ok").to_dict()["check"]

        assert docker == "docker"
        assert browser == "chromium"
        assert ddgs == "ddgs"
        assert len({docker, browser, ddgs}) == 3

    def test_every_result_to_dict_is_json_serializable(self):
        results = [
            DockerPreflightResult(
                binary_found=False, daemon_responds=False, usable_without_sudo=False, message="нет"
            ),
            BrowserPreflightResult(ok=False, message="нет"),
            DdgsPreflightResult(ok=False, message="нет"),
        ]
        for result in results:
            payload = result.to_dict()
            roundtripped = json.loads(json.dumps(payload))
            assert roundtripped == payload


class TestDockerPreflightToDict:
    def test_ok_and_message_track_the_dataclass(self):
        result = DockerPreflightResult(
            binary_found=True, daemon_responds=True, usable_without_sudo=False, message="частично"
        )
        payload = result.to_dict()

        assert payload["ok"] == result.ok
        assert payload["ok"] is False  # usable_without_sudo=False -> overall not ok
        assert payload["message"] == result.message

    def test_details_carries_the_three_underlying_flags(self):
        result = DockerPreflightResult(
            binary_found=True, daemon_responds=False, usable_without_sudo=False, message="x"
        )
        details = result.to_dict()["details"]

        assert details == {
            "binary_found": result.binary_found,
            "daemon_responds": result.daemon_responds,
            "usable_without_sudo": result.usable_without_sudo,
        }


class TestBrowserPreflightToDict:
    # Parametrized on BOTH sides of `ok` in one assertion: Docker's fixtures
    # already exercise both booleans across its tests (ok=True via
    # TestUniformShapeAcrossModules, ok=False here), but the browser and
    # ddgs fixtures before this fix only ever built ok=True / ok=False
    # respectively -- so a `to_dict()` that hardcodes `"ok": True` (browser)
    # or `"ok": False` (ddgs) survived every existing test untouched. See
    # AGENTS.md "Тесты попарной различимости надо заводить на ВСЕ ветки
    # разом".
    @pytest.mark.parametrize("ok", [True, False])
    def test_ok_and_message_track_the_dataclass(self, ok):
        message = "готов" if ok else "не найден"
        result = BrowserPreflightResult(ok=ok, message=message)
        payload = result.to_dict()

        assert payload["ok"] is ok
        assert payload["message"] == result.message
        assert payload["details"] == {}


class TestDdgsPreflightToDict:
    @pytest.mark.parametrize("ok", [True, False])
    def test_ok_and_message_track_the_dataclass(self, ok):
        message = "установлен" if ok else "не установлен"
        result = DdgsPreflightResult(ok=ok, message=message)
        payload = result.to_dict()

        assert payload["ok"] is ok
        assert payload["message"] == result.message
        assert payload["details"] == {}
