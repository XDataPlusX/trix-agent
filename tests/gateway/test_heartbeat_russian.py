"""Строка ожидания собирается по-русски и называет дело, а не инструмент."""

import re

from hermes_cli.trix_status import build_heartbeat_text

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def test_names_the_action_in_russian():
    text = build_heartbeat_text(minutes=3, tool_name="read_file", lang="ru")
    assert text == "⏳ 3 мин — читаю файл"


def test_unknown_tool_falls_back_to_the_plain_line():
    text = build_heartbeat_text(minutes=6, tool_name="some_future_tool", lang="ru")
    assert text == "⏳ Работаю — 6 мин"
    assert "some_future_tool" not in text


def test_no_tool_falls_back_to_the_plain_line():
    assert build_heartbeat_text(minutes=9, tool_name=None, lang="ru") == "⏳ Работаю — 9 мин"


def test_english_catalog_still_renders():
    text = build_heartbeat_text(minutes=3, tool_name="read_file", lang="en")
    assert "3 min" in text


def test_english_catalog_never_mixes_in_a_russian_action():
    """``_ACTIONS`` only holds Russian phrases -- splicing one into an
    English-language line would render "Working -- 3 min -- читаю файл",
    a mixed-language string that breaks the English catalog's own
    contract. lang="en" must produce a plain line with no tail at all,
    not the localized "with_action" template."""
    text = build_heartbeat_text(minutes=3, tool_name="read_file", lang="en")
    assert not _CYRILLIC_RE.search(text), f"English heartbeat contains Cyrillic: {text!r}"
    assert text == "⏳ Working — 3 min"


def test_batch_tool_call_still_names_the_first_known_action():
    """agent._current_tool becomes a ", "-joined string when the model
    calls several tools concurrently (agent/tool_executor.py:876,964),
    e.g. "read_file, web_search". A direct dict lookup on that whole
    string never matches, so without splitting it the heartbeat silently
    drops the deed for every batched turn -- a common case, since models
    batch tool calls often."""
    text = build_heartbeat_text(minutes=3, tool_name="read_file, web_search", lang="ru")
    assert text == "⏳ 3 мин — читаю файл"
