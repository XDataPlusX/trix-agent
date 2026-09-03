"""Nexara speech-to-text backend.

Nexara (nexara.ru) is a Russian-hosted speech-to-text service exposed
through an OpenAI-SDK-compatible endpoint
(``POST https://api.nexara.ru/v1/audio/transcriptions``), authenticated
with ``Authorization: Bearer nx-<key>``. It transcribes multiple
languages, including strong Russian support, and is reachable from
Russian datacenters without a proxy.

Registers a :class:`~agent.transcription_provider.TranscriptionProvider`
named ``nexara`` (see ``plugins/stt/nexara/plugin.yaml`` — bundled,
``kind: backend``, so it auto-loads and is selectable via
``stt.provider: nexara`` without any ``plugins.enabled`` opt-in).

Credential resolution goes through
``tools.tool_backend_helpers.resolve_provider_secret`` — the single
owner of STT/TTS key lookup (config > env/.env > credential pool) — so
``NEXARA_API_KEY`` set via ``hermes auth add nexara`` or ``.env`` works
identically to the built-in providers. (The API key itself is never read
from ``config.yaml`` — only ``stt.nexara.base_url``/``model`` are, see
below — so error messages must not claim otherwise.)

Model and language selection: the dispatcher
(``tools/transcription_tools.py::_dispatch_to_plugin_provider``)
already resolves ``stt.nexara.model`` and the language hint (order:
``stt.nexara.language`` -> ``stt.language`` -> the legacy
``HERMES_LOCAL_STT_LANGUAGE`` env var -> auto-detect) before calling
:meth:`NexaraTranscriptionProvider.transcribe`, so this module does not
duplicate that resolution — it only forwards whatever it is given. When
no model is resolved that way (e.g. a direct caller that bypasses the
dispatcher), :meth:`transcribe` falls back to :meth:`default_model`
(``whisper-1``, matching the OpenAI-compatible convention this endpoint
follows) — the OpenAI-compatible ``/v1/audio/transcriptions`` contract
requires a ``model`` field, so omitting it entirely is a guaranteed 400.

Known gap: unlike the built-in cloud providers, plugin-dispatched
providers are NOT run through the cloud silence-trim preprocessing in
``tools/transcription_tools.py`` (see its own comment: "Command-type and
plugin providers are deliberately NOT trimmed"). This means silence in
the original recording is uploaded to, and billed by, Nexara as-is — a
real cost, not a feature. Candidate for an upstream fix that extends the
trim step to plugin providers too; out of scope for this plugin, which
cannot preprocess audio before the dispatcher hands it over.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

# Hardcoded fallback only — see _resolve_base_url() for the real
# (per-call) resolution order: stt.nexara.base_url (config.yaml) ->
# NEXARA_BASE_URL (env) -> this default. Nexara does not currently
# document an alternate base URL; this is an escape hatch, same pattern
# as ELEVENLABS_STT_BASE_URL / XAI_STT_BASE_URL in transcription_tools.py.
_DEFAULT_BASE_URL = "https://api.nexara.ru/v1/audio/transcriptions"

# xAI/ElevenLabs STT both use 120s for the same reason: a multi-minute
# voice note takes real upload + processing time, and 60s was too tight.
_REQUEST_TIMEOUT_SECONDS = 120.0


def _resolve_api_key() -> str:
    """Resolve the Nexara API key via the shared voice-key resolver.

    Delegates to ``tools.tool_backend_helpers.resolve_provider_secret``
    so env/.env and the ``hermes auth add nexara`` credential pool
    resolve identically to the built-in STT providers. Never raises.
    """
    try:
        from tools.tool_backend_helpers import resolve_provider_secret
    except Exception:  # pragma: no cover — tools is in-repo
        return str(os.getenv("NEXARA_API_KEY") or "").strip()
    try:
        return resolve_provider_secret("NEXARA_API_KEY", "nexara")
    except Exception:  # noqa: BLE001 — resolution must never raise
        logger.debug("Nexara API key resolution failed", exc_info=True)
        return ""


def _load_nexara_stt_config() -> Dict[str, Any]:
    """Read ``stt.nexara`` from config.yaml (base_url override only —
    the API key is never read from here, see the module docstring)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("stt") if isinstance(cfg, dict) else None
        nexara_section = section.get("nexara") if isinstance(section, dict) else None
        return nexara_section if isinstance(nexara_section, dict) else {}
    except Exception:
        logger.debug("Could not load stt.nexara config", exc_info=True)
        return {}


def _resolve_base_url(nexara_cfg: Dict[str, Any]) -> str:
    """(config > env > hardcoded default) — mirrors the ElevenLabs/xAI
    STT base_url resolution pattern in transcription_tools.py."""
    return str(
        nexara_cfg.get("base_url")
        or os.getenv("NEXARA_BASE_URL")
        or _DEFAULT_BASE_URL
    ).strip().rstrip("/")


def _extract_error_detail(response: Any, api_key: str = "") -> str:
    """Best-effort human-readable detail from a non-200 Nexara response.

    Never includes request headers (the API key lives only in the
    ``Authorization`` header we sent, never in the response body we
    read here) — this function only ever looks at response content.
    ``api_key``, when given, is scrubbed from the detail as a defensive
    second layer in case a misbehaving gateway ever echoed the
    ``Authorization`` header back into an error body.
    """
    try:
        body = response.json()
    except Exception:
        detail = (getattr(response, "text", "") or "")[:300]
    else:
        detail = ""
        if isinstance(body, dict):
            error_value = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(error_value, dict):
                detail = str(error_value.get("message") or error_value)
            elif error_value:
                detail = str(error_value)
        if not detail:
            detail = (getattr(response, "text", "") or "")[:300]
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail


class NexaraTranscriptionProvider(TranscriptionProvider):
    """Nexara (nexara.ru) STT backend — OpenAI-compatible transcriptions API."""

    @property
    def name(self) -> str:
        return "nexara"

    @property
    def display_name(self) -> str:
        return "Nexara"

    def is_available(self) -> bool:
        return bool(_resolve_api_key())

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Nexara",
            "badge": "платно · серверы в РФ",
            "tag": "Российское распознавание речи, 0,36₽/мин, 200 минут бесплатно",
            "env_vars": [
                {
                    "key": "NEXARA_API_KEY",
                    "prompt": "Nexara API key",
                    "url": "https://nexara.ru",
                },
            ],
        }

    def list_models(self) -> List[Dict[str, Any]]:
        # Nexara does not publish a selectable model catalog today, but
        # the OpenAI-compatible /v1/audio/transcriptions contract this
        # endpoint follows requires a non-empty `model` field — omitting
        # it entirely is a guaranteed 400 on every request. "whisper-1"
        # is the OpenAI-compatible default id every other OpenAI-shaped
        # STT backend in this codebase falls back to (see DEFAULT_STT_MODEL
        # in tools/transcription_tools.py). default_model() (ABC default)
        # returns this row's "id" whenever transcribe() isn't given an
        # explicit model.
        return [{"id": "whisper-1", "display": "Whisper-1 (по умолчанию)"}]

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        api_key = _resolve_api_key()
        if not api_key:
            # Realistically unreachable via the normal dispatch path: the
            # plugin dispatcher (tools/transcription_tools.py::
            # _dispatch_to_plugin_provider) already gates on is_available()
            # BEFORE ever calling transcribe(), and returns its own
            # "STT plugin 'nexara' is not available" envelope instead. This
            # branch only fires for a caller that instantiates the provider
            # directly and skips that gate — kept short, no instructions no
            # one following the normal path will ever see.
            return {
                "success": False,
                "transcript": "",
                "error": "NEXARA_API_KEY не задан.",
                "provider": self.name,
            }

        try:
            import httpx
        except ImportError:
            return {
                "success": False,
                "transcript": "",
                "error": "Пакет httpx не установлен (pip install httpx).",
                "provider": self.name,
            }

        nexara_cfg = _load_nexara_stt_config()
        base_url = _resolve_base_url(nexara_cfg)
        resolved_model = model or self.default_model()

        path = Path(file_path)
        data: Dict[str, str] = {}
        if resolved_model:
            data["model"] = resolved_model
        if language:
            data["language"] = language

        try:
            with path.open("rb") as audio_file:
                response = httpx.post(
                    base_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (path.name, audio_file)},
                    data=data,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
        except OSError as exc:
            return {
                "success": False,
                "transcript": "",
                "error": f"Не удалось прочитать аудиофайл: {exc}",
                "provider": self.name,
            }
        except httpx.TimeoutException:
            return {
                "success": False,
                "transcript": "",
                "error": (
                    f"Таймаут запроса к Nexara STT ({_REQUEST_TIMEOUT_SECONDS:.0f}с)."
                ),
                "provider": self.name,
            }
        except httpx.RequestError as exc:
            # str(exc) on httpx request errors never includes headers —
            # only connection-level detail (host, reason) — safe to surface.
            return {
                "success": False,
                "transcript": "",
                "error": f"Сетевая ошибка Nexara STT: {exc}",
                "provider": self.name,
            }
        except Exception as exc:  # noqa: BLE001 — never raise per the ABC contract
            logger.warning("Nexara STT transcription failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "transcript": "",
                "error": f"Ошибка Nexara STT: {exc}",
                "provider": self.name,
            }

        if response.status_code == 401:
            return {
                "success": False,
                "transcript": "",
                "error": "Nexara STT: неверный или отозванный API-ключ (HTTP 401).",
                "provider": self.name,
            }
        if response.status_code != 200:
            detail = _extract_error_detail(response, api_key)
            return {
                "success": False,
                "transcript": "",
                "error": f"Ошибка Nexara STT (HTTP {response.status_code}): {detail}",
                "provider": self.name,
            }

        try:
            payload = response.json()
        except Exception:
            return {
                "success": False,
                "transcript": "",
                "error": "Nexara STT вернул некорректный JSON-ответ.",
                "provider": self.name,
            }

        transcript_text = ""
        if isinstance(payload, dict):
            transcript_text = str(payload.get("text") or "").strip()
        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "Nexara STT вернул пустой транскрипт.",
                "provider": self.name,
                "no_speech": True,
            }

        logger.info(
            "Transcribed %s via Nexara STT (%d chars)",
            path.name, len(transcript_text),
        )
        return {"success": True, "transcript": transcript_text, "provider": self.name}


def register(ctx) -> None:
    """Plugin entry point — wire ``NexaraTranscriptionProvider`` into the registry."""
    ctx.register_transcription_provider(NexaraTranscriptionProvider())
