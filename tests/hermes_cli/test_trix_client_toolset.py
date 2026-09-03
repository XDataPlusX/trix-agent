"""Client-scenario coverage for the curated Trix Telegram toolset.

Design §11 (``docs/product/specs/2026-08-17-trix-agent-standard-build-design.md``)
is a table: "here is what a client does with the agent, here is the tool it
needs, here is whether curation still provides it." This file is that table
turned into an executable acceptance check for the whole spec's tool
curation (§7): courting a shorter tool schema must never cost the client a
scenario they actually use.

The toolset is built with the *exact* chain the gateway uses to build a
session's tools (``gateway/run.py:25743``):

    _get_platform_tools(config, "telegram")   # -> enabled toolset NAMES
        -> resolve_toolset() per name          # -> tool names
        -  resolve_toolset() per name in agent.disabled_toolsets

Design §7.2 documents why the subtraction step operates on ``resolve_toolset()``
per name rather than on ``toolsets.py``'s bundle-aware helpers: the platform
composite here is a flat curated list, not a ``hermes-*`` bundle, so there is
no core-tools-wipe risk to guard against (contrast model_tools.py's
``bundle_non_core_tools`` special case for platform bundles / posture
toolsets). This also mirrors the trap design §7.2 found: subtraction is by
tool NAME, so a disabled toolset can silently strip a tool that another
*enabled* toolset also provides.
"""

import copy
from pathlib import Path

import pytest
import yaml

from hermes_cli.tools_config import _get_platform_tools
from tools.registry import discover_builtin_tools, registry
from toolsets import resolve_toolset

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _load_template():
    with open(TRIX_TEMPLATE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resolve_client_tool_names(cfg: dict, platform: str) -> set:
    """Reproduce the gateway's per-session tool-name resolution for a platform.

    Same chain as ``gateway/run.py:25743``: enabled toolset NAMES from
    ``_get_platform_tools()``, each expanded via ``resolve_toolset()``, minus
    ``resolve_toolset()`` of each name in ``agent.disabled_toolsets``.
    """
    enabled_toolset_names = _get_platform_tools(cfg, platform)
    tool_names: set = set()
    for toolset_name in enabled_toolset_names:
        tool_names.update(resolve_toolset(toolset_name))

    agent_cfg = cfg.get("agent") or {}
    disabled_toolset_names = agent_cfg.get("disabled_toolsets") or []
    for toolset_name in disabled_toolset_names:
        tool_names.difference_update(resolve_toolset(toolset_name))

    return tool_names


def _resolve_client_schema_tool_names(cfg: dict, platform: str) -> set:
    """The tool names that actually reach the model, ``check_fn`` included.

    ``trix-config.yaml`` carries no ``agent.disabled_toolsets`` (see the
    comment in the template): ``bfl`` and ``kanban`` are not excluded by
    name -- ``_resolve_client_tool_names()`` above will happily return
    ``kanban_*``/``bfl_flux3_*`` names, because ``_get_platform_tools()``
    restores both as non-configurable platform-native toolsets regardless of
    ``platform_toolsets.telegram``. What keeps them off the wire is each
    tool's own ``check_fn`` (``check_bfl_requirements`` /
    ``_check_kanban_mode``), applied by ``registry.get_definitions()`` --
    the exact call ``model_tools._compute_tool_definitions()`` makes before
    handing schemas to the model. This reproduces that call.

    ``discover_builtin_tools()`` runs first so the registry actually knows
    about these tools at all (each test file is its own subprocess, so
    nothing has imported ``tools/kanban_tools.py`` / ``tools/flux3_video_tool.py``
    yet). Skipping it would make an unregistered name silently vanish from
    ``get_definitions()``'s output for a reason that has nothing to do with
    ``check_fn`` -- the absence assertions below would pass for the wrong
    reason.
    """
    discover_builtin_tools()
    tool_names = _resolve_client_tool_names(cfg, platform)
    schema = registry.get_definitions(tool_names, quiet=True)
    return {entry["function"]["name"] for entry in schema}


@pytest.fixture(scope="module")
def template():
    return _load_template()


@pytest.fixture(scope="module")
def telegram_tools(template):
    return _resolve_client_tool_names(template, "telegram")


@pytest.fixture(scope="module")
def telegram_schema_tools(template):
    return _resolve_client_schema_tool_names(template, "telegram")


# ---------------------------------------------------------------------------
# §11 — every client scenario keeps its tool
# ---------------------------------------------------------------------------

# (tool name, which §11 row / agent-loop primitive it serves). Comments are
# load-bearing: this list is read again in six months and must explain
# *why* each name is here, not just that it happened to be there in 2026-08.
CLIENT_SCENARIO_TOOLS = [
    ("terminal", "Выполнить команду — команда в Docker-песочнице (§11)"),
    ("process", "Выполнить команду — фоновые/долгие процессы; резолвится "
                "вместе с terminal из тулсета 'terminal', то же §11-сценарий"),
    ("read_file", "Прочитать и править файл — чтение (§11)"),
    ("write_file", "Прочитать и править файл — запись (§11)"),
    ("patch", "Прочитать и править файл — точечная правка (§11)"),
    ("search_files", "Прочитать и править файл — поиск по файлам (§11)"),
    ("web_search", "Поискать в сети — бэкенд ddgs, без ключа (§11, §7.1)"),
    ("browser_navigate", "Прочитать страницу — открыть URL локальным Chromium (§11)"),
    ("browser_snapshot", "Прочитать страницу — прочитать содержимое страницы (§11)"),
    ("vision_analyze", "Прислать агенту фото и спросить, что на нём (§11)"),
    ("memory", "Вспомнить прошлое — долговременная память о клиенте (§11)"),
    ("session_search", "Вспомнить прошлое — поиск по прошлым разговорам (§11)"),
    ("cronjob", "Поставить задачу по расписанию, вместе с /sethome (§11)"),
    ("text_to_speech", "Услышать ответ голосом — голос ru-RU (§11)"),
    ("todo", "Агент удерживает план внутри длинной задачи (не §11-строка, но "
             "базовый примитив агентного цикла, включённый тулсетом 'todo')"),
    ("clarify", "Агент задаёт уточняющий вопрос вместо угадывания (не §11-строка, "
                "базовый примитив, включённый тулсетом 'clarify')"),
    ("skills_list", "Навыки агента и справка по продукту — список навыков "
                     "(комментарий шаблона: \"в том числе справка по продукту\")"),
    ("skill_view", "Навыки агента и справка по продукту — открыть конкретный навык"),
]


class TestClientScenarioToolsPresent:
    @pytest.mark.parametrize(
        "tool_name,scenario",
        CLIENT_SCENARIO_TOOLS,
        ids=[name for name, _ in CLIENT_SCENARIO_TOOLS],
    )
    def test_tool_present(self, telegram_tools, tool_name, scenario):
        assert tool_name in telegram_tools, (
            f"'{tool_name}' is missing from the curated Telegram toolset -- "
            f"this breaks the client scenario: {scenario}"
        )


# ---------------------------------------------------------------------------
# §7.1 — deliberately excluded tools stay excluded
# ---------------------------------------------------------------------------

class TestClientScenarioToolsAbsent:
    def test_web_extract_absent(self, telegram_tools):
        # No paid extract backend configured (§4.3) -- see the pair
        # invariant below for the relation this is one half of.
        assert "web_extract" not in telegram_tools

    def test_computer_use_absent(self, telegram_tools):
        # macOS screen-control tool; the product runs on Linux VPS (§7.1).
        assert "computer_use" not in telegram_tools

    def test_no_kanban_tools_reach_schema(self, telegram_schema_tools):
        # Multi-agent task board; the client runs a single agent, no board
        # to maintain (§7.1, §11). Not excluded by name -- the template
        # carries no agent.disabled_toolsets (kanban_* is still part of the
        # name-resolved toolset, see _resolve_client_tool_names). What keeps
        # it off the wire is _check_kanban_mode() returning False on a clean
        # machine with no `kanban` in the top-level `toolsets` config key.
        kanban_tools = {name for name in telegram_schema_tools if name.startswith("kanban_")}
        assert not kanban_tools, kanban_tools

    def test_no_bfl_tools_reach_schema(self, telegram_schema_tools):
        # Paid Flux video generation; requires a Nous sign-in the client
        # doesn't have (§7.1, §11). Not excluded by name for the same reason
        # as kanban above -- check_bfl_requirements() returning False on a
        # clean machine with no Nous credential is what excludes it.
        bfl_tools = {name for name in telegram_schema_tools if name.startswith("bfl_flux3_")}
        assert not bfl_tools, bfl_tools


# ---------------------------------------------------------------------------
# Pair invariant: web_extract present iff web.extract_backend is configured
# ---------------------------------------------------------------------------

class TestWebExtractPairedWithExtractBackend:
    """This is a relation, not a snapshot: it must hold for the template as
    shipped today (no backend -> no tool) AND for the template with a
    backend configured (backend -> tool). If only the first branch existed,
    a future change that adds ``web.extract_backend`` to the template
    without also restoring the ``web`` toolset in ``platform_toolsets``
    would go unnoticed -- exactly the coupling design §7.1's honest-caveat
    section warns is easy to drop.
    """

    def test_no_backend_configured_means_no_web_extract(self, template):
        assert "extract_backend" not in (template.get("web") or {}), (
            "trix-config.yaml now sets web.extract_backend -- the other "
            "half of this pair (restoring 'web' in platform_toolsets) must "
            "also be true; see test_backend_configured_means_web_extract_present"
        )
        tool_names = _resolve_client_tool_names(template, "telegram")
        assert "web_extract" not in tool_names

    def test_backend_configured_means_web_extract_present(self, template):
        mutated = copy.deepcopy(template)
        mutated.setdefault("web", {})["extract_backend"] = "firecrawl"
        # Mirrors the manual step trix-config.yaml's own comment documents
        # (lines around platform_toolsets.telegram's "search" entry): once a
        # paid extract backend exists, swap "search" for "web".
        telegram_toolsets = mutated["platform_toolsets"]["telegram"]
        mutated["platform_toolsets"]["telegram"] = [
            "web" if name == "search" else name for name in telegram_toolsets
        ]

        tool_names = _resolve_client_tool_names(mutated, "telegram")

        assert "web_extract" in tool_names, (
            "configuring web.extract_backend and swapping search->web in "
            "platform_toolsets.telegram must restore web_extract -- the "
            "pairing between the config knob and the resolved toolset is "
            "broken"
        )
        # web_search must survive the swap too (it's still provided by
        # the 'web' toolset itself, and independently by 'browser').
        assert "web_search" in tool_names


# ---------------------------------------------------------------------------
# Subtraction invariant: disabled_toolsets must not strip a shared tool
# ---------------------------------------------------------------------------

class TestDisabledToolsetsDoNotStripSharedTools:
    """``agent.disabled_toolsets`` subtracts by tool NAME
    (model_tools.py::_compute_tool_definitions), not by toolset identity. A
    disabled toolset that happens to share a tool name with a currently
    *enabled* toolset removes that tool everywhere, not just from the
    disabled toolset -- this is exactly the defect design §7.2 found with
    ``web``: putting ``web`` in ``agent.disabled_toolsets`` would strip
    ``web_search``, which ``search`` and ``browser`` also provide.

    This is expressed as a relation between whatever the template's
    ``agent.disabled_toolsets`` and ``platform_toolsets.telegram`` say today,
    not as a hardcoded pair of names -- it keeps catching the defect class
    even if the specific disabled/enabled toolsets change later.
    """

    def test_disabled_toolsets_do_not_remove_tools_other_enabled_toolsets_provide(
        self, template
    ):
        enabled_toolset_names = set(_get_platform_tools(template, "telegram"))
        agent_cfg = template.get("agent") or {}
        disabled_toolset_names = agent_cfg.get("disabled_toolsets") or []

        for disabled_name in disabled_toolset_names:
            removed = set(resolve_toolset(disabled_name))
            provided_by_other_enabled: set = set()
            for enabled_name in enabled_toolset_names:
                if enabled_name == disabled_name:
                    continue
                provided_by_other_enabled.update(resolve_toolset(enabled_name))

            overlap = removed & provided_by_other_enabled
            assert not overlap, (
                f"disabled toolset '{disabled_name}' removes tool(s) "
                f"{sorted(overlap)} that another enabled toolset also "
                "provides -- disabled_toolsets subtracts by tool NAME, so "
                "this silently strips the tool from every provider, not "
                f"just '{disabled_name}' (design §7.2)"
            )
