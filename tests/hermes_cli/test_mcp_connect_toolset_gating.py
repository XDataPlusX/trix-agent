"""Спека 20, Ruling 1 — ``mcp_connect`` доступен только в мессенджере.

Тот же контур, что у ``secret_request`` в спеке 19, и по той же причине:
подключать MCP-сервер разговором нужно там, где у клиента нет ни шелла, ни
хостовой файловой системы из песочницы. У CLI, десктопа и TUI для этого
есть ``hermes mcp add`` и собственный терминал, поэтому платить схемой
инструмента они не должны.

Проверяется теми же цепочками разрешения, которыми пользуются сами
поверхности: ``_get_platform_tools`` для платформ шлюза и связка
``_HERMES_CORE_TOOLS`` / ``desktop_ui`` / ``project`` для десктопа.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.tools_config import _get_platform_tools, _toolset_allowed_for_platform
from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _load_trix_template() -> dict:
    with open(TRIX_TEMPLATE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tools_for(enabled_toolsets) -> set:
    names = set()
    for ts in enabled_toolsets:
        names.update(resolve_toolset(ts))
    return names


def test_absent_from_hermes_core_tools():
    """Не в общем наборе: за него платили бы все платформы на каждом вызове."""
    assert "mcp_connect" not in _HERMES_CORE_TOOLS


def test_absent_from_desktop_surface_toolsets():
    """Десктопный резолвер складывает desktop_ui и project поверх ядра —
    инструмента нет ни там, ни там."""
    assert "mcp_connect" not in resolve_toolset("desktop_ui", include_registry=False)
    assert "mcp_connect" not in resolve_toolset("project", include_registry=False)


def test_absent_for_bare_cli_platform():
    """У сессии CLI инструмента нет: там есть терминал и `hermes mcp add`."""
    enabled = _get_platform_tools({}, "cli", include_default_mcp_servers=False)
    assert "mcp" not in enabled
    assert "mcp_connect" not in _tools_for(enabled)


def test_present_for_the_real_telegram_client_template():
    """Конфиг, который реально уезжает клиенту, включает инструмент."""
    enabled = _get_platform_tools(
        _load_trix_template(), "telegram", include_default_mcp_servers=False
    )
    assert "mcp" in enabled
    assert "mcp_connect" in _tools_for(enabled)


def test_toolset_is_not_configurable_outside_messaging():
    """Таблица ограничений не пускает тулсет в чек-листы чужих платформ."""
    assert _toolset_allowed_for_platform("mcp", "telegram")
    assert not _toolset_allowed_for_platform("mcp", "cli")
    assert not _toolset_allowed_for_platform("mcp", "discord")


def _schema_names_for_telegram() -> set:
    """Имена инструментов, которые реально уходят модели в telegram-сессии.

    Идём той же цепочкой, что и сама сессия: список тулсетов платформы из
    настоящего клиентского шаблона, затем сборка схем со всеми ярусами
    отложенного показа.
    """
    from model_tools import get_tool_definitions

    enabled = _get_platform_tools(_load_trix_template(), "telegram")
    defs = get_tool_definitions(enabled_toolsets=list(enabled), quiet_mode=True)
    names = set()
    for d in defs:
        fn = d.get("function") if isinstance(d, dict) else None
        name = (fn or d).get("name") if isinstance(fn or d, dict) else None
        if name:
            names.add(name)
    return names


def test_the_model_actually_sees_the_tool():
    """Инструмент обязан быть в схеме напрямую, а не за мостом.

    Спека 20, Ruling 1 и приёмка §5.7. На свежей машине MCP-серверов нет,
    поэтому mcp_connect оказывается ЕДИНСТВЕННЫМ откладываемым
    инструментом — и мост включается от него одного, убирая его же из
    схемы. Тогда подключить сервер модель может, только сперва догадавшись
    сходить в поиск инструментов.
    """
    assert "mcp_connect" in _schema_names_for_telegram()


def test_the_tool_does_not_turn_on_the_deferral_bridge():
    """На машине без единого сервера набор инструментов прежний.

    Появление служебных tool_search / tool_describe / tool_call означает,
    что режим подачи инструментов сменился на всей установке ещё до того,
    как клиент что-либо подключил.
    """
    names = _schema_names_for_telegram()
    assert not ({"tool_search", "tool_describe", "tool_call"} & names)
