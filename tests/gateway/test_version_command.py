"""Tests for gateway /version command.

Раньше здесь сравнивался вывод `/version` с `format_banner_version_label()`
— то есть с баннером запуска CLI, который НАМЕРЕННО несёт хвост
`· upstream … (+N carried commits)`. Утверждение было противоположно
задуманному (см. `banner.format_version_reply_text` и
`slash_exec._exec_version`: хвост в клиентском ответе — жаргон
сопровождения, у свежей установки он всегда «0 ahead», а слова «upstream»
и «carried commits» ничего не говорят тому, кто этот репозиторий не
клонировал).

Тест проходил только там, где хвост оказывался пустым, и краснел в любом
окружении, где данные git разрешались — например в отдельном клоне, из
которого собирается релиз. То есть он не проверял поведение, а зависел от
устройства рабочей копии.
"""

import asyncio

from hermes_cli.banner import format_banner_version_label, format_version_reply_text


def test_gateway_version_command_matches_the_client_facing_helper():
    from gateway.run import GatewayRunner

    result = asyncio.run(GatewayRunner._handle_version_command(None, None))  # type: ignore[arg-type]
    assert result == format_version_reply_text()


def test_gateway_version_command_carries_no_maintenance_tail():
    """Настоящий контракт: клиент не должен читать про upstream и
    перенесённые коммиты. Проверяется наблюдаемым свойством, а не
    сравнением двух реализаций между собой — иначе тест снова окажется
    зелёным ровно там, где сравнивать нечего."""
    from gateway.run import GatewayRunner

    result = asyncio.run(GatewayRunner._handle_version_command(None, None))  # type: ignore[arg-type]
    assert "upstream" not in result
    assert "carried commits" not in result
    assert "·" not in result


def test_the_two_labels_are_allowed_to_differ():
    """Обратная сторона: баннер CLI и ответ клиенту — разные тексты по
    замыслу, и их расхождение НЕ является поломкой. Тест фиксирует, что
    длинная форма является надмножеством короткой, а не равна ей."""
    assert format_version_reply_text() in format_banner_version_label()
