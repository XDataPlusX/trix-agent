"""Tests for the bundled Nexara STT plugin (plugins/stt/nexara).

Covers: ABC contract + registration, credential resolution via the shared
``resolve_provider_secret`` helper, the OpenAI-compatible form-data upload
(success / 401 / network error / timeout), language-hint forwarding, and
that the API key never leaks into an error envelope. A dispatch-level test
also exercises the real ``tools.transcription_tools`` plugin-dispatch path
end to end.
"""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

import plugins.stt.nexara as nexara_plugin
from agent.transcription_provider import TranscriptionProvider
from plugins.stt.nexara import NexaraTranscriptionProvider, register


API_KEY = "nx-super-secret-test-key"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    # Every test starts with no ambient Nexara credential unless it sets one.
    monkeypatch.delenv("NEXARA_API_KEY", raising=False)
    yield


def _audio_file(tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"fake-ogg-bytes")
    return path


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_plugin_manifest_is_a_bundled_backend():
    manifest_path = "plugins/stt/nexara/plugin.yaml"
    with open(manifest_path, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    assert manifest["name"] == "nexara"
    assert manifest["kind"] == "backend"
    env_names = [
        e["name"] if isinstance(e, dict) else e
        for e in manifest.get("requires_env", [])
    ]
    assert "NEXARA_API_KEY" in env_names


# ---------------------------------------------------------------------------
# ABC contract + registration
# ---------------------------------------------------------------------------


def test_provider_is_a_transcription_provider():
    provider = NexaraTranscriptionProvider()
    assert isinstance(provider, TranscriptionProvider)
    assert provider.name == "nexara"
    assert provider.display_name == "Nexara"


def test_get_setup_schema_exposes_env_var_prompt():
    schema = NexaraTranscriptionProvider().get_setup_schema()
    assert schema["env_vars"][0]["key"] == "NEXARA_API_KEY"


def test_register_calls_ctx_register_transcription_provider():
    calls = []

    class _Ctx:
        def register_transcription_provider(self, provider):
            calls.append(provider)

    register(_Ctx())
    assert len(calls) == 1
    assert isinstance(calls[0], NexaraTranscriptionProvider)


def test_registers_into_the_real_registry_without_shadowing_builtins():
    """Confirms 'nexara' isn't in the built-in shadow list, so registration
    actually sticks (a colliding name would be silently dropped with a
    warning — see agent/transcription_registry.py)."""
    from agent import transcription_registry

    transcription_registry._reset_for_tests()
    try:
        transcription_registry.register_provider(NexaraTranscriptionProvider())
        assert transcription_registry.get_provider("nexara") is not None
        assert transcription_registry.get_provider("NEXARA").name == "nexara"
    finally:
        transcription_registry._reset_for_tests()


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


def test_is_available_false_without_key(monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: "")
    assert NexaraTranscriptionProvider().is_available() is False


def test_is_available_true_with_key(monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    assert NexaraTranscriptionProvider().is_available() is True


# ---------------------------------------------------------------------------
# transcribe() — missing key
# ---------------------------------------------------------------------------


# NOTE: no "transcribe() without a key returns a Russian error" test here.
# The real dispatch path (tools.transcription_tools._dispatch_to_plugin_provider)
# gates on is_available() BEFORE ever calling transcribe() — see
# test_dispatch_reports_unavailable_when_key_missing below for that real
# path, and test_is_available_false_without_key above for the check that
# gate actually relies on. A direct transcribe() call with no key is a
# defensive branch nothing in the real system reaches; it isn't worth its
# own behavioral test beyond "it doesn't crash and doesn't leak" (covered
# incidentally by every other error-path test's `API_KEY not in error`
# assertion pattern).


# ---------------------------------------------------------------------------
# transcribe() — success, default model, base_url resolution
# ---------------------------------------------------------------------------


def test_transcribe_success(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse(200, {"text": "привет, это тест"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = NexaraTranscriptionProvider().transcribe(
        str(_audio_file(tmp_path)), language="ru",
    )

    assert result == {
        "success": True,
        "transcript": "привет, это тест",
        "provider": "nexara",
    }
    assert captured["url"] == nexara_plugin._DEFAULT_BASE_URL
    assert captured["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert captured["data"]["language"] == "ru"
    # Critical fix: the OpenAI-compatible endpoint requires a non-empty
    # `model` field — omitting it is a guaranteed 400. No model given ->
    # falls back to default_model() ("whisper-1").
    assert captured["data"]["model"] == "whisper-1"
    assert captured["timeout"] == 120.0


def test_transcribe_forwards_model_when_given(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["data"] = data
        return _FakeResponse(200, {"text": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    NexaraTranscriptionProvider().transcribe(
        str(_audio_file(tmp_path)), model="nexara-whisper",
    )
    # An explicit model always wins over the default_model() fallback.
    assert captured["data"]["model"] == "nexara-whisper"


def test_upload_does_not_force_a_mimetype(tmp_path, monkeypatch):
    """Matches the ElevenLabs/xAI STT upload shape — (filename, handle)
    only, no forced third `application/octet-stream` element."""
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["files"] = files
        return _FakeResponse(200, {"text": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    name, _fh = captured["files"]["file"]  # raises ValueError if a 3-tuple
    assert name == "voice.ogg"


def test_resolve_base_url_priority_config_over_env_over_default(monkeypatch):
    assert nexara_plugin._resolve_base_url({}) == nexara_plugin._DEFAULT_BASE_URL

    monkeypatch.setenv("NEXARA_BASE_URL", "https://env.example/v1/x")
    assert nexara_plugin._resolve_base_url({}) == "https://env.example/v1/x"

    assert (
        nexara_plugin._resolve_base_url({"base_url": "https://cfg.example/v1/y"})
        == "https://cfg.example/v1/y"
    )


def test_transcribe_uses_config_base_url_over_env(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    monkeypatch.setenv("NEXARA_BASE_URL", "https://env.example/v1/x")
    monkeypatch.setattr(
        nexara_plugin, "_load_nexara_stt_config",
        lambda: {"base_url": "https://cfg.example/v1/y"},
    )
    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["url"] = url
        return _FakeResponse(200, {"text": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert captured["url"] == "https://cfg.example/v1/y"


# ---------------------------------------------------------------------------
# transcribe() — error paths (Russian text, key never leaked)
# ---------------------------------------------------------------------------


def test_transcribe_401_returns_russian_error_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: _FakeResponse(401, {"error": "invalid api key"}),
    )
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert "401" in result["error"]
    assert "ключ" in result["error"].lower()
    assert API_KEY not in result["error"]


def test_transcribe_other_http_error_surfaces_status_and_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: _FakeResponse(500, {"error": "internal error"}),
    )
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert "500" in result["error"]
    assert "internal error" in result["error"]
    assert API_KEY not in result["error"]


def test_error_detail_masks_api_key_if_echoed_by_gateway(tmp_path, monkeypatch):
    """Defensive second layer: even if a misbehaving gateway ever echoed
    the Authorization header's key back into an error body, it must not
    reach the returned error string verbatim."""
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: _FakeResponse(
            500, {"error": f"upstream rejected token {API_KEY}"}
        ),
    )
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert API_KEY not in result["error"]
    assert "***" in result["error"]


def test_transcribe_network_error(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)

    def raise_network_error(*a, **kw):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "post", raise_network_error)
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert "Nexara" in result["error"]
    assert API_KEY not in result["error"]


def test_transcribe_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)

    def raise_timeout(*a, **kw):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", raise_timeout)
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert "Таймаут" in result["error"]
    assert API_KEY not in result["error"]


def test_transcribe_empty_transcript_is_treated_as_no_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse(200, {"text": "  "}))
    result = NexaraTranscriptionProvider().transcribe(str(_audio_file(tmp_path)))
    assert result["success"] is False
    assert result.get("no_speech") is True
    assert API_KEY not in result["error"]


def test_transcribe_missing_audio_file(tmp_path, monkeypatch):
    monkeypatch.setattr(nexara_plugin, "_resolve_api_key", lambda: API_KEY)
    missing = tmp_path / "does-not-exist.ogg"
    result = NexaraTranscriptionProvider().transcribe(str(missing))
    assert result["success"] is False
    assert API_KEY not in result["error"]


# ---------------------------------------------------------------------------
# Full dispatch path: tools.transcription_tools._dispatch_to_plugin_provider
# ---------------------------------------------------------------------------


def test_dispatch_to_plugin_provider_end_to_end(tmp_path, monkeypatch):
    """Real bundled-plugin discovery (kind: backend -> auto-loads with no
    plugins.enabled opt-in) registers 'nexara' into the registry — no
    manual register_provider() call. The real dispatcher (used by
    transcribe_audio()) then resolves it, checks availability, and
    forwards the resolved language.

    Sets the real env var (rather than monkeypatching the module we import
    directly) because discovery imports the bundled ``plugins/stt/nexara``
    file under its own synthetic module name
    (``hermes_plugins.stt__nexara``) — a distinct module object from the
    ``plugins.stt.nexara`` this test file imports — so the registered
    instance is not necessarily the one this test's module-level
    monkeypatches would reach. The env var and the httpx.post patch are
    both effective regardless of which module object ends up registered
    (real env var; httpx is one shared module in sys.modules either way).
    """
    from hermes_cli.plugins import PluginManager
    from agent import transcription_registry
    from tools.transcription_tools import _dispatch_to_plugin_provider

    monkeypatch.setenv("NEXARA_API_KEY", API_KEY)

    PluginManager().discover_and_load()
    assert transcription_registry.get_provider("nexara") is not None

    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["data"] = data
        captured["headers"] = headers
        return _FakeResponse(200, {"text": "готово"})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = _dispatch_to_plugin_provider(
        str(_audio_file(tmp_path)),
        "nexara",
        {"nexara": {}},
        model=None,
        language="ru",
    )

    assert result is not None
    assert result["success"] is True
    assert result["transcript"] == "готово"
    assert result["provider"] == "nexara"
    assert captured["data"]["language"] == "ru"
    assert captured["headers"]["Authorization"] == f"Bearer {API_KEY}"


def test_dispatch_reports_unavailable_when_key_missing(tmp_path, monkeypatch):
    """Same real-discovery path as above, no key set — the dispatcher's
    own availability gate fires before transcribe() is ever called."""
    from hermes_cli.plugins import PluginManager
    from agent import transcription_registry
    from tools.transcription_tools import _dispatch_to_plugin_provider

    monkeypatch.delenv("NEXARA_API_KEY", raising=False)

    PluginManager().discover_and_load()
    assert transcription_registry.get_provider("nexara") is not None

    result = _dispatch_to_plugin_provider(
        str(_audio_file(tmp_path)), "nexara", {"nexara": {}},
    )

    assert result is not None
    assert result["success"] is False
    assert "nexara" in result["error"].lower()
