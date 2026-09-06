"""Спека 18 acceptance criterion 5 — no tool schema leaks a ``.hermes`` path.

The model must stop reading ``.hermes`` in its own tool descriptions and
concluding it is Hermes instead of Trix (docs/product/specs/
2026-09-06-trix-agent-sandbox-identity-design.md). This asserts over the
*objects* ``get_tool_definitions()`` returns — never by reading source text,
which this project bans outright (see CLAUDE.md, "Never read source code in
tests").
"""
from __future__ import annotations

import pytest


def _iter_strings(value):
    """Yield every string found anywhere inside a nested dict/list structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


class TestToolSchemasCarryNoHermesPath:
    def test_no_tool_schema_string_mentions_a_hermes_path(self):
        """No tool schema (name/description/parameter text) may contain a
        ``.hermes`` path fragment — the exact evidence the model used to
        conclude it was Hermes rather than Trix."""
        from model_tools import get_tool_definitions

        tools = get_tool_definitions(quiet_mode=True)
        assert tools, "expected at least some tool definitions in the test environment"

        offenders = []
        for tool in tools:
            for s in _iter_strings(tool):
                if ".hermes" in s:
                    offenders.append((tool.get("function", {}).get("name"), s))

        assert not offenders, (
            "tool schema(s) still leak a .hermes path: "
            f"{offenders[:10]}{'...' if len(offenders) > 10 else ''}"
        )

    def test_no_tool_schema_string_mentions_root_hermes_or_tilde_hermes(self):
        """Belt-and-suspenders: neither the container form (``/root/.hermes``)
        nor the shell-expanded form (``~/.hermes``) may appear anywhere in a
        tool schema."""
        from model_tools import get_tool_definitions

        tools = get_tool_definitions(quiet_mode=True)
        joined = "\n".join(
            s for tool in tools for s in _iter_strings(tool)
        )
        assert "/root/.hermes" not in joined
        assert "~/.hermes" not in joined


def test_session_snapshot_filename_carries_no_upstream_name():
    """Снимок сессии лежит в /tmp ВНУТРИ песочницы — его имя видит модель.

    Найдено живым прогоном на VM 31.29.151.3 уже ПОСЛЕ переименования
    базы: `ls -a /root` показывал чистый `.trix`, а
    `find / -name '*hermes*'` возвращал `/tmp/hermes-snap-<id>.sh`.
    Спека 18 перечисляла четыре канала утечки и этот пропустила, ревью
    тоже; поймала только проверка на живой машине.

    Проверяется поведение (какой путь объект реально построил), а не
    текст исходника.
    """
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment()
    assert "hermes" not in env._snapshot_path.lower(), env._snapshot_path
    assert env._session_id in env._snapshot_path
