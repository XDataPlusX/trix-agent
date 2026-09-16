"""Default STT language contract.

Teknium (July 2026): the global ``stt.language`` defaults to a language hint
(not auto-detect) because Whisper auto-detection frequently misidentifies
short/accented clips ("STT transcribed the wrong language" class). Users opt
back into auto-detect with ``stt.language: ""``.

Ruling 3 (Aug 2026): the hint must be the product's own UI language, not a
hardcoded "en" -- a global default is only correct if it matches who the
product actually ships to (see tests/agent/test_i18n.py::
test_stt_language_default_matches_the_ui_language for the same invariant).
"""

from hermes_cli.config import DEFAULT_CONFIG
from tools.transcription_tools import _resolve_stt_language


class TestDefaultSttLanguage:
    def test_default_config_matches_the_ui_language(self):
        assert DEFAULT_CONFIG["stt"]["language"] == DEFAULT_CONFIG["display"]["language"]


    def test_per_provider_still_wins_over_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_LOCAL_STT_LANGUAGE", raising=False)
        stt = dict(DEFAULT_CONFIG["stt"])
        stt["groq"] = {"language": "he"}
        assert _resolve_stt_language("groq", stt) == "he"
