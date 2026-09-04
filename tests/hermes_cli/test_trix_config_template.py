"""Tests for the Trix curated config.yaml template and its resolver.

Trix ships a short, curated ~90-line config.yaml template
(``assets/config/trix-config.yaml``) instead of upstream's 1700+ line
``cli-config.yaml.example``. ``resolve_config_template()`` is the single,
testable place that decides which file a fresh install copies — see
``hermes_cli/config_template.py``.
"""

import copy
import re
from pathlib import Path

import pytest
import yaml

from gateway.display_config import resolve_display_setting
from hermes_cli.config import _KNOWN_ROOT_KEYS
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.config_template import resolve_config_template
from hermes_cli.tools_config import _get_platform_tools
from hermes_cli.toolset_validation import validate_platform_toolsets
from toolsets import validate_toolset

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _url_port(url: str):
    """Порт из адреса, или None если он не указан.

    ``_KNOWN_LOCAL_URLS`` — апстримный словарь мастера настройки: он растёт
    не нами, и запись без явного порта там законна
    (``http://localhost/health``). Прежний разбор
    ``int(url.rsplit(":", 1)[1].split("/")[0])`` на такой записи падал бы
    ``ValueError`` — первый же апстримный адрес без порта ронял бы наш тест
    исключением вместо осмысленного вердикта.
    """
    match = re.search(r":(\d+)(?:/|$)", url)
    return int(match.group(1)) if match else None


def test_url_port_survives_an_upstream_entry_without_a_port():
    assert _url_port("http://localhost/health") is None
    assert _url_port("http://127.0.0.1:11434") == 11434
    assert _url_port("http://localhost:18000/api/tags") == 18000

# Root keys that are version markers, not settings, and are therefore
# exempt from the "template must be a pure delta from DEFAULT_CONFIG" rule.
# ``_config_version`` MUST equal DEFAULT_CONFIG's current version (see
# test_config_version_matches_default below) -- if it didn't, the config
# loader would treat a freshly-installed template as stale (v0) and run it
# through migrate_config()'s yaml.dump on the very first `doctor --fix` /
# `hermes update` / `hermes profile create`, silently stripping every
# Russian comment in the file the moment the customer's install "updates".
_VERSION_MARKER_KEYS = frozenset({"_config_version"})

# Ключи, которые лежат в шаблоне ПУСТЫМИ намеренно: это не переопределение
# умолчания, а место для ответа клиента вместе с объяснением, зачем он
# нужен. Правило «чистой дельты» их не касается — оно про переопределённые
# значения, а здесь переопределять нечего.
#
# `timezone` (спека 11): ответ знает только сам клиент, он даёт его в
# мастере настройки. Любое непустое значение в шаблоне было бы ответом за
# него — и, что хуже, досев вписал бы это значение на уже установленные
# машины при первом же `hermes update`, то есть сменил бы пояс задним
# числом там, где это признано небезопасным (задачи сохраняются в базу уже
# с поясом). Строка нужна в файле, чтобы до клиента доехал комментарий и
# чтобы мастеру было куда писать.
#
# Само по себе освобождение ничего не прячет: пара
# `template["timezone"] == DEFAULT_CONFIG["timezone"] == ""` проверяется
# отдельно (TestTimezoneSection), так что подменить пустое место настоящим
# переопределением молча нельзя.
_DOCUMENTED_PLACEHOLDER_KEYS = frozenset({"timezone"})


# Ключи, чьи дефолты живут в gateway/display_config.py (_GLOBAL_DEFAULTS),
# а не в DEFAULT_CONFIG. Это настоящие настройки шлюза; отсутствие в
# DEFAULT_CONFIG — свойство апстрима, а не опечатка у нас. Проверяем
# отношением: ключ обязан существовать ТАМ.
def _gateway_display_defaults():
    from gateway.display_config import _GLOBAL_DEFAULTS

    return _GLOBAL_DEFAULTS


# Ключи, намеренно продублированные значением, равным дефолту. Отображение
# {путь: причина} — не множество: причины у этих ключей РАЗНЫЕ, и общий
# комментарий на всё множество позволил бы бесшумно докладывать сюда что
# угодно. Каждый новый ключ обязан принести собственное обоснование, а не
# сослаться на соседнее.
_UPSTREAM_DRIFT_PINS = {
    # Другой мотив: здесь действующий дефолт — ровно тот DEFAULT_CONFIG,
    # с которым сравнивает инвариант, так что строка ПОВЕДЕНИЯ НЕ МЕНЯЕТ
    # (доказано исполнением в ревью Задачи 3, круг 1: убрать её —
    # load_config_readonly()["approvals"]["timeout"] и
    # tools.approval._get_approval_config() всё равно дают 300). Строка
    # остаётся как документация для клиента: сколько секунд у него есть на
    # ответ, прежде чем агент сам откажется от удаления — без явной
    # строки в файле молчаливый отказ через 5 минут выглядел бы поломкой
    # продукта. Отслеживается отношением
    # (test_timeout_matches_default_config), а не снимком: если апстрим
    # сменит дефолт, тест покраснеет и заставит поправить документацию.
    "approvals.timeout": (
        "поведения не меняет — значение равно DEFAULT_CONFIG; строка "
        "остаётся как документация клиенту, отслеживается отношением, "
        "не снимком"
    ),
    # Шесть чисел связи с Телеграмом. Общее у них только то, что все они
    # равны дефолту и поведения не меняют; в файле каждое стоит ради своего
    # собственного вопроса, на который клиенту иначе нечем ответить.
    # Отслеживаются отношением (test_telegram_network_matches_default_config).
    "telegram.network.pool_size": (
        "единственное место, где клиент видит потолок одновременных "
        "соединений — без строки вопрос «почему в людном чате отправки "
        "встают в очередь» не на что перевести в действие"
    ),
    "telegram.network.pool_timeout": (
        "документирует, что мы сознательно ждём дольше библиотечной "
        "секунды: увидев строку, клиент правит её, а не считает зависание "
        "поломкой бота"
    ),
    "telegram.network.connect_timeout": (
        "это число называет сообщение об ошибке при недоступном Телеграме; "
        "названное в ошибке должно находиться в файле, иначе совет из "
        "текста ошибки невыполним"
    ),
    "telegram.network.read_timeout": (
        "порог, после которого молчание Телеграма считается отказом — "
        "клиент с медленным каналом поднимает именно его"
    ),
    "telegram.network.write_timeout": (
        "парная к read_timeout половина для исходящих запросов; без строки "
        "правка read_timeout выглядит достаточной, а она половинчатая"
    ),
    "telegram.network.media_write_timeout": (
        "отдельный путь для файлов: клиент, у которого «фото не уходят, а "
        "текст уходит», без этой строки не найдёт, что чинить — "
        "write_timeout к загрузкам не применяется вовсе"
    ),
}


# Корни, чьё содержимое — свободное отображение, задаваемое пользователем
# (DEFAULT_CONFIG хранит для них пустой словарь). Их листья не могут
# существовать в DEFAULT_CONFIG по построению, поэтому проверка
# существования к ним неприменима. Проверяем отношением: значение в
# DEFAULT_CONFIG обязано быть ПУСТЫМ словарём — если апстрим однажды
# наполнит его, исключение перестанет действовать и тест это покажет.
_FREE_FORM_MAPPING_ROOTS = ("platform_hints", "personalities", "hooks", "quick_commands")


def _is_free_form_mapping_leaf(path):
    root = path[0]
    if root not in _FREE_FORM_MAPPING_ROOTS:
        return False
    return DEFAULT_CONFIG.get(root) == {}


def _collect_leaves(node, prefix=()):
    """Yield (path_tuple, value) for every non-dict leaf under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _collect_leaves(value, prefix + (key,))
    else:
        yield prefix, node


def _lookup(default_config, path):
    """Walk ``default_config`` along ``path``. Returns (exists, value)."""
    cur = default_config
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


@pytest.fixture(scope="module")
def template():
    with open(TRIX_TEMPLATE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def template_text():
    """Сырой текст шаблона. Комментарии объявлены частью продукта — клиент
    читает этот файл как документацию, — поэтому у них есть свои инварианты,
    а разобранный ``dict`` их не видит вовсе."""
    return TRIX_TEMPLATE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# resolve_config_template()
# ---------------------------------------------------------------------------


class TestResolveConfigTemplate:
    def test_prefers_trix_template_when_present(self, tmp_path):
        # Both templates on disk — Trix's curated one wins.
        assets_dir = tmp_path / "assets" / "config"
        assets_dir.mkdir(parents=True)
        trix_file = assets_dir / "trix-config.yaml"
        trix_file.write_text("terminal:\n  backend: docker\n", encoding="utf-8")
        upstream_file = tmp_path / "cli-config.yaml.example"
        upstream_file.write_text("model: {}\n", encoding="utf-8")

        result = resolve_config_template(tmp_path)

        assert result == trix_file

    def test_falls_back_to_upstream_example(self, tmp_path):
        # No Trix template — falls back to the upstream example.
        upstream_file = tmp_path / "cli-config.yaml.example"
        upstream_file.write_text("model: {}\n", encoding="utf-8")

        result = resolve_config_template(tmp_path)

        assert result == upstream_file

    def test_returns_none_when_neither_exists(self, tmp_path):
        result = resolve_config_template(tmp_path)

        assert result is None


class TestResolveTrixConfigTemplateOnly:
    """``sync_missing_client_sections`` must splice ONLY the curated template.

    ``resolve_config_template()``'s upstream fallback is right for copying
    into an empty place (fresh install), but wrong for grafting into a
    live client file: upstream's 1700-line example carries English prose
    and sandbox-defeating keys (``docker_mount_cwd_to_workspace``,
    ``home_mode``, ...) that would land in the client's config.yaml the
    moment our own curated template happened to be missing.
    """

    def test_returns_the_curated_template_when_present(self, tmp_path):
        from hermes_cli.config_template import resolve_trix_config_template_only

        assets_dir = tmp_path / "assets" / "config"
        assets_dir.mkdir(parents=True)
        trix_file = assets_dir / "trix-config.yaml"
        trix_file.write_text("terminal:\n  backend: docker\n", encoding="utf-8")

        assert resolve_trix_config_template_only(tmp_path) == trix_file

    def test_no_upstream_fallback_when_curated_template_is_absent(self, tmp_path):
        from hermes_cli.config_template import resolve_trix_config_template_only

        upstream_file = tmp_path / "cli-config.yaml.example"
        upstream_file.write_text("model: {}\n", encoding="utf-8")

        assert resolve_trix_config_template_only(tmp_path) is None


# ---------------------------------------------------------------------------
# assets/config/trix-config.yaml content
# ---------------------------------------------------------------------------


class TestTrixConfigTemplateContent:
    def test_template_file_exists(self):
        assert TRIX_TEMPLATE_PATH.is_file(), (
            f"expected curated template at {TRIX_TEMPLATE_PATH}"
        )

    def test_parses_as_yaml_mapping(self, template):
        assert isinstance(template, dict)
        assert template, "template must not be empty"

    def test_every_root_key_is_known(self, template):
        unknown = [key for key in template if key not in _KNOWN_ROOT_KEYS]
        assert not unknown, (
            f"unknown root key(s) in trix-config.yaml: {unknown} — "
            "a typo here means the setting silently never applies"
        )

    def test_every_leaf_with_a_default_config_root_exists_in_default_config(
        self, template
    ):
        missing = []
        for root_key, root_value in template.items():
            if root_key not in DEFAULT_CONFIG:
                continue
            for path, _value in _collect_leaves(root_value, (root_key,)):
                exists, _default_value = _lookup(DEFAULT_CONFIG, path)
                if exists:
                    continue
                if (
                    len(path) == 2
                    and path[0] == "display"
                    and path[1] in _gateway_display_defaults()
                ):
                    continue
                if _is_free_form_mapping_leaf(path):
                    continue
                missing.append(".".join(path))
        assert not missing, (
            f"leaf path(s) not found in DEFAULT_CONFIG: {missing} — "
            "upstream may have renamed the key"
        )

    def test_no_leaf_value_matches_the_default(self, template):
        """The template must be a delta — every overridden leaf must differ
        from DEFAULT_CONFIG's value, or it doesn't belong in the file.

        ``_VERSION_MARKER_KEYS`` (``_config_version``) is the one deliberate
        exception: it is a schema-version marker the config loader/migrator
        reads, not a behavioral setting, and it is REQUIRED to equal
        DEFAULT_CONFIG's value (see test_config_version_matches_default) —
        the opposite of every other key in this file.
        """
        matches_default = []
        for root_key, root_value in template.items():
            if root_key in _VERSION_MARKER_KEYS:
                continue
            if root_key in _DOCUMENTED_PLACEHOLDER_KEYS:
                continue
            if root_key not in DEFAULT_CONFIG:
                continue
            for path, value in _collect_leaves(root_value, (root_key,)):
                exists, default_value = _lookup(DEFAULT_CONFIG, path)
                dotted = ".".join(path)
                if dotted in _UPSTREAM_DRIFT_PINS:
                    continue
                if _is_free_form_mapping_leaf(path):
                    continue
                if exists and value == default_value:
                    matches_default.append((dotted, value))
        assert not matches_default, (
            f"leaf(s) equal to DEFAULT_CONFIG, template is not a pure delta: "
            f"{matches_default}"
        )

    def test_config_version_matches_default(self, template):
        """_config_version must track DEFAULT_CONFIG's current version --
        a relation, not a snapshot, so a future upstream version bump makes
        this test demand the template be updated rather than silently going
        stale.

        Without this, a freshly-copied template with no (or an outdated)
        _config_version reads as v0 to the config loader, and the very
        first `doctor --fix` / `hermes update` / `hermes profile create`
        runs it through migrate_config()'s yaml.dump — which strips every
        Russian comment in the file. The whole point of shipping a curated,
        commented template is undone on the customer's first touch.
        """
        assert "_config_version" in template, (
            "trix-config.yaml is missing _config_version -- it will be "
            "treated as version 0 and rewritten (comments stripped) on the "
            "customer's first doctor --fix / hermes update / profile create"
        )
        assert template["_config_version"] == DEFAULT_CONFIG["_config_version"], (
            f"template _config_version={template['_config_version']!r} does not "
            f"match DEFAULT_CONFIG's {DEFAULT_CONFIG['_config_version']!r} -- "
            "bump the template to match, or it will be treated as stale and "
            "rewritten (comments stripped) on first migration"
        )

    def test_platform_toolsets_resolve_without_warnings(self, template):
        platform_toolsets = template.get("platform_toolsets", {})
        warnings = validate_platform_toolsets(platform_toolsets, validate_toolset)
        assert warnings == [], warnings

    def test_display_section_pins_client_chat_behaviour(self, template):
        """Все четыре ключа поведения чата закреплены явно.

        Сегодня три из них действуют через цепочку умолчаний
        (gateway/display_config.py), которую апстрим может поменять, а
        четвёртый (cleanup_progress) выключен и его надо включить.

        Литералы ниже документируют, что именно записано в файле. Контракт
        проверяет resolve_display_setting() — тот же резолвер, которым
        реально пользуется шлюз для платформы telegram, — так что тест
        ловит не только опечатку в значении, но и опечатку в структуре
        (например, не в той секции), из-за которой резолвер молча
        откатился бы на встроенный дефолт.
        """
        display = template["display"]
        assert display["platforms"]["telegram"]["streaming"] is False
        assert display["tool_progress"] == "off"
        assert display["interim_assistant_messages"] is False
        assert display["cleanup_progress"] is True

        assert resolve_display_setting(template, "telegram", "streaming") is False
        assert resolve_display_setting(template, "telegram", "tool_progress") == "off"
        assert (
            resolve_display_setting(template, "telegram", "interim_assistant_messages")
            is False
        )
        assert resolve_display_setting(template, "telegram", "cleanup_progress") is True

    def test_tts_voice_language_matches_display_language(self, template):
        """tts.edge.voice and DEFAULT_CONFIG's display.language must name the
        same language — an English voice reading Russian text is the bug
        this template exists to fix."""
        voice = template["tts"]["edge"]["voice"]
        display_language = DEFAULT_CONFIG["display"]["language"]

        voice_lang_prefix = voice.split("-")[0].lower()
        assert voice_lang_prefix == display_language.lower(), (
            f"tts.edge.voice={voice!r} does not match "
            f"display.language={display_language!r}"
        )


class TestUpstreamDriftPinsListItself:
    """_UPSTREAM_DRIFT_PINS is exactly the kind of exception list that
    quietly turns into decoration -- this template's test suite has
    already caught two decorative versions of it in one review round: the
    port literal in the Telegram hint test (fixed by deriving it from the
    template instead) and the reason string in this very dict, which the
    skip check never reads (only ``dotted in _UPSTREAM_DRIFT_PINS``
    membership matters to it) -- a reviewer proved by execution that
    ``{"approvals.cron_mode": ""}`` slips a brand-new pin past every test
    with zero justification. Both assertions below are about the
    *exception list itself*, not about the template, and both are
    required to keep the list honest over time:

    1. Every reason must be a real, non-trivial justification -- a
       one-word "документация" six months from now explains nothing to
       whoever has to decide if the pin still applies. Enforced with a
       length floor rather than parsing content, because content
       validation would be its own maze; length is a cheap, blunt proxy
       for "someone had to actually write a sentence here."
    2. Every pinned path must still be a *live* pin, right now: it must
       exist in the template, and its value there must still equal
       DEFAULT_CONFIG's value at that path. A pin protects one specific
       fact -- "this key is deliberately duplicated at the default
       value" -- and that fact can go stale two ways: the key gets
       removed from the template (nothing left to protect), or its value
       drifts away from the default (it's now a genuine delta, not a
       duplicate, and belongs in the template on its own merits, not
       hidden behind this exception). Either way, an unpruned pin is a
       hole punched in "template must be a pure delta" that nothing else
       in this file would ever re-discover.
    """

    _MIN_REASON_LENGTH = 40

    def test_every_pin_has_a_real_reason_and_is_still_needed(self, template):
        for dotted, reason in _UPSTREAM_DRIFT_PINS.items():
            assert (
                isinstance(reason, str) and len(reason.strip()) >= self._MIN_REASON_LENGTH
            ), (
                f"_UPSTREAM_DRIFT_PINS[{dotted!r}] reason is missing or too "
                f"short ({reason!r}, need >= {self._MIN_REASON_LENGTH} chars) "
                "-- a pin without a real written justification is just a "
                "silent hole in the 'template must be a pure delta' invariant"
            )

            path = tuple(dotted.split("."))
            exists_in_template, template_value = _lookup(template, path)
            assert exists_in_template, (
                f"_UPSTREAM_DRIFT_PINS pins {dotted!r}, but it is no longer "
                "present in trix-config.yaml -- remove the stale pin"
            )

            exists_in_default, default_value = _lookup(DEFAULT_CONFIG, path)
            assert exists_in_default and template_value == default_value, (
                f"_UPSTREAM_DRIFT_PINS pins {dotted!r} as 'deliberately "
                f"duplicated at the DEFAULT_CONFIG value', but "
                f"template={template_value!r} != "
                f"DEFAULT_CONFIG={default_value!r} -- the pin no longer "
                "applies (the key is now a real delta, or the default "
                "changed underneath it); remove the pin and let "
                "test_no_leaf_value_matches_the_default judge it on its own"
            )


class TestTerminalBackendIsDocker:
    """terminal.backend: docker is not cosmetic config -- it is the whole
    isolation guarantee for command execution (Ruling 1 of
    docs/product/specs/2026-08-17-trix-agent-standard-build-design.md;
    Вводные.md §5: "отдельная VM и отдельный Docker sandbox для действий
    агента").

    At ``terminal.backend: local`` the agent runs shell commands as the
    SAME OS user as the gateway process, with no boundary at all --
    ``terminal(command="cat $HERMES_HOME/.env")`` hands it back the
    client's own provider API keys. This is not a hypothetical: it is
    acknowledged directly in agent/file_safety.py.

    Before this test, a full run of tests/hermes_cli/ (4954 tests) stayed
    green with the ``backend: docker`` line deleted from the REAL template
    entirely -- every "docker-aware" test in this suite constructs its own
    synthetic ``"terminal:\\n  backend: docker\\n"`` fixture instead of
    reading assets/config/trix-config.yaml, so none of them could ever
    catch a regression in the shipped file. A replacement with ``local``
    happens to be caught by test_no_leaf_value_matches_the_default (it
    equals DEFAULT_CONFIG's own default), but a replacement with anything
    else -- including deletion -- is caught by nothing but this test.
    """

    def test_terminal_backend_is_docker(self, template):
        backend = (template.get("terminal") or {}).get("backend")
        assert backend == "docker", (
            f"trix-config.yaml's terminal.backend={backend!r}, expected "
            "'docker' -- Ruling 1 / Вводные.md §5. At 'local' the agent "
            "executes shell commands as the same OS user as the gateway "
            "process and can read the client's own .env (provider API "
            "keys) via terminal(command=\"cat $HERMES_HOME/.env\")."
        )


class TestPlatformToolsetsKeyResolution:
    """A typo in the platform key under ``platform_toolsets`` (e.g.
    ``telegramm`` instead of ``telegram``) is invisible to both
    ``validate_platform_toolsets`` (which only validates toolset *names*,
    not the platform key) and to the leaf/delta checks above (``platform_
    toolsets`` isn't rooted in DEFAULT_CONFIG, so those checks skip it
    entirely). Left uncaught, the agent silently falls back to the
    platform's native default composite toolset — undoing every bit of
    §7's curation (55 tools instead of 33) with zero warning.

    These are relations, not snapshots: they check that (1) every curated
    name actually got resolved, and (2) a typo'd key resolves to something
    different from the correctly-keyed template — not any specific
    hardcoded list of names.

    Note this is deliberately NOT a strict-equality check between the
    resolved set and ``platform_toolsets.telegram`` (that was tried and
    reverted): ``_get_platform_tools()`` also restores non-configurable
    platform-native toolsets (currently ``bfl``, ``kanban``) regardless of
    what ``platform_toolsets.telegram`` lists — trix-config.yaml carries no
    ``agent.disabled_toolsets`` to mask that (see the template's own
    comment: their own ``check_fn`` keeps them off the wire on a clean
    client machine either way). A superset is the honest contract here.
    """

    def test_every_curated_toolset_is_resolved(self, template):
        curated = set(template["platform_toolsets"]["telegram"])
        actual = _get_platform_tools(template, "telegram")

        assert curated <= actual, (
            f"_get_platform_tools() dropped {sorted(curated - actual)} from "
            f"platform_toolsets.telegram (resolved: {sorted(actual)}) — a "
            "platform-key typo (or similar mismatch) makes the resolver "
            "silently fall back to the platform's default composite "
            "toolset instead of the curated list"
        )

    def test_platform_key_typo_resolves_differently_from_the_real_key(self, template):
        correctly_keyed = _get_platform_tools(template, "telegram")

        mutated = copy.deepcopy(template)
        mutated["platform_toolsets"] = {
            "telegramm": mutated["platform_toolsets"].pop("telegram")
        }
        typo_fallback = _get_platform_tools(mutated, "telegram")

        assert correctly_keyed != typo_fallback, (
            "resolving 'telegram' against a template whose platform key is "
            "typo'd ('telegramm') produced the SAME toolset set as the "
            "correctly-keyed template — a real key typo would go "
            "undetected because the silent fallback to the platform's full "
            "default composite is indistinguishable from the curated list"
        )


class TestSandboxDemoPorts:
    """Песочница публикует десять высоких портов под демо.

    Диапазон высокий намеренно: на 3000/5432/6379/8080 инструменты садятся
    по умолчанию, и наружу однажды выехало бы то, что агент поднял для
    себя. Отдельно 8080 у продукта уже занят — там мастер настройки ищет
    самостоятельно поднятый SearXNG (_KNOWN_LOCAL_URLS).
    """

    def test_demo_port_range_is_published(self, template):
        extra = template["terminal"]["docker_extra_args"]
        assert extra == ["-p", "18000-18009:18000-18009"]

    def test_range_does_not_collide_with_known_local_services(self, template):
        from hermes_cli.setup_wizard.tools_view import _KNOWN_LOCAL_URLS

        spec = template["terminal"]["docker_extra_args"][1]
        host_range = spec.split(":", 1)[0]
        low, high = (int(part) for part in host_range.split("-"))

        for url in _KNOWN_LOCAL_URLS.values():
            # ``_KNOWN_LOCAL_URLS`` — апстримный словарь мастера настройки:
            # он растёт не нами, и запись без явного порта там законна
            # (``http://localhost/health``). Раньше здесь стоял
            # ``int(url.rsplit(":", 1)[1]...)``, который на такой записи падал
            # бы ValueError — то есть первый же апстримный адрес без порта
            # ронял бы наш тест исключением вместо осмысленного вердикта.
            # Порта нет — коллизии портов быть не может, запись пропускаем.
            port = _url_port(url)
            if port is None:
                continue
            assert not (low <= port <= high), (
                f"демо-диапазон перекрывает локальный сервис {url} — "
                "контейнер не стартует, если клиент его включит"
            )


class TestToolLoopGuardrails:
    """Заклинивший агент обязан останавливаться сам, а не по команде
    человека, которого в мессенджере нет.

    Апстримный дефолт ``hard_stop_enabled: false`` рассчитан на консоль:
    предупреждение уходит в контекст, модель его читает, а если не
    послушалась — человек видит повтор и жмёт стоп. У клиента в Telegram
    ``display.tool_progress`` выключен: он не видит ни вызовов, ни
    предупреждений, и «стоп» для него — это молчание в чате. Круг при
    этом идёт за его деньги.
    """

    def test_hard_stop_is_enabled(self, template):
        assert template["tool_loop_guardrails"]["hard_stop_enabled"] is True

    def test_hard_stop_is_a_real_override_of_the_upstream_default(self):
        """Смысл строки — именно в расхождении с апстримом.

        Если апстрим когда-нибудь включит жёсткую остановку сам, строка
        станет пустым дублем дефолта, и её надо будет убрать (её тут же
        поймает test_no_leaf_value_matches_the_default). Этот тест ловит
        обратное: что расхождение всё ещё существует и строка всё ещё
        что-то меняет.
        """
        assert (
            DEFAULT_CONFIG["tool_loop_guardrails"]["hard_stop_enabled"] is False
        ), (
            "апстрим включил hard_stop_enabled по умолчанию — строка в "
            "шаблоне больше ничего не переопределяет, убрать её"
        )

    def test_client_never_sees_the_warnings_the_upstream_default_relies_on(
        self, template
    ):
        """Связь двух настроек, ради которой всё это и сделано.

        Основание для жёсткой остановки — что в Telegram поток вызовов
        выключен. Если кто-то вернёт ``tool_progress`` обратно, основание
        исчезнет и решение надо пересматривать, а не тащить по инерции.
        """
        assert template["display"]["tool_progress"] == "off"


class TestSessionResetSection:
    """Заброшенная тема обязана однажды начаться заново — сама.

    Клиент ведёт темы в группе Telegram, и каждая тема это отдельный
    разговор. Апстримный дефолт ``mode: "none"`` значит «не начинать
    заново никогда»: тема, к которой не вернулись, навсегда остаётся
    живым разговором и никогда не финализируется. Ежедневного рубежа мы
    при этом не берём — он обрывал бы тему посреди работы.
    """

    def _policy(self, template):
        """Настоящий резолвер шлюза, а не чтение словаря.

        ``SessionResetPolicy.from_dict`` — то, через что значения из
        config.yaml реально доезжают до ``_should_reset``. Опечатка в
        имени ключа даст здесь молчаливый апстримный дефолт, и тест это
        покажет; сравнение с разобранным ``dict`` — нет.
        """
        from gateway.config import SessionResetPolicy

        return SessionResetPolicy.from_dict(template["session_reset"])

    def test_mode_is_not_the_upstream_never_reset_default(self, template):
        from gateway.config import SessionResetPolicy

        policy = self._policy(template)
        assert policy.mode != SessionResetPolicy().mode, (
            "режим сброса совпал с апстримным дефолтом — заброшенная тема "
            "снова остаётся живым разговором навсегда"
        )
        assert policy.mode == "idle"

    def test_the_daily_boundary_is_deliberately_not_used(self, template):
        """«idle», а не «daily»/«both»: рубеж суток обрывает тему посреди
        работы — вернулись к ней наутро, а разговор уже начат заново."""
        assert "daily" not in self._policy(template).mode
        assert "at_hour" not in template["session_reset"], (
            "at_hour в шаблоне ничего не делает при mode: idle и читается "
            "клиентом как обещание ежедневного рубежа, которого нет"
        )

    def test_the_span_outlasts_a_weekend(self, template):
        """Отношение, а не снимок значения: пятница вечер — понедельник
        утро это 64 часа. Любой срок короче означает, что рабочая тема
        начинается заново каждые выходные."""
        weekend_minutes = 64 * 60
        assert self._policy(template).idle_minutes > weekend_minutes

    def test_the_span_is_longer_than_a_day(self, template):
        """Отдельно от выходных: срок в сутки и меньше рвал бы тему,
        к которой вернулись на следующий день."""
        from gateway.config import SessionResetPolicy

        policy = self._policy(template)
        assert policy.idle_minutes > 24 * 60
        assert policy.idle_minutes > SessionResetPolicy().idle_minutes, (
            "срок не превышает апстримные сутки — строка в шаблоне ничего "
            "не даёт сверх дефолта"
        )

    def test_the_client_is_told_before_the_conversation_starts_over(self, template):
        """Уведомление — то, из-за чего вся секция безопасна для клиента.
        Оно приходит по апстримному дефолту ``notify``, поэтому в шаблоне
        его нет; проверяем, что дефолт всё ещё такой."""
        assert self._policy(template).notify is True, (
            "уведомление о новом разговоре выключено — тема начиналась бы "
            "заново молча"
        )

    def test_the_notice_the_client_will_read_names_this_very_span(self, template):
        """Связь шаблона с текстом: сколько стоит в конфиге, столько и
        произносится клиенту. Отдельный литерал «трое суток» в тесте свёл
        бы пару только на бумаге — нужен вызов продуктового кода."""
        from hermes_cli.trix_session_notices import duration_phrase

        rendered = duration_phrase(
            template["session_reset"]["idle_minutes"], lang="ru"
        )
        assert rendered == "трое суток", rendered


class TestApprovalsSection:
    def test_mode_is_manual(self, template):
        """Подтверждение спрашивает человека, а не вспомогательную модель.

        smart отдал бы вердикт основной модели клиента (auxiliary.approval
        .provider: auto) — за его деньги и с правом самой одобрить то самое
        удаление, ради которого правило заведено.
        """
        assert template["approvals"]["mode"] == "manual"

    def test_timeout_matches_default_config(self, template):
        """timeout: 300 is a documentation pin, not a behavioral override.

        It equals DEFAULT_CONFIG's own default today, so it changes nothing
        at runtime (verified by execution in Task 3 review round 1: with the
        line removed, both ``load_config_readonly()["approvals"]["timeout"]``
        and ``tools.approval._get_approval_config()`` still resolve to 300).
        It stays in the template because the client reads this file as
        product documentation too -- they need to see, in the file itself,
        how many seconds they have to respond before the agent gives up on
        the deletion and treats silence as a denial.

        This MUST be a relation to DEFAULT_CONFIG's live value, not a
        snapshot of 300: if upstream ever changes the default, this test
        has to go red and force the template's comment/value to be updated
        -- the alternative is the client reading a number that is no longer
        true.
        """
        assert (
            template["approvals"]["timeout"]
            == DEFAULT_CONFIG["approvals"]["timeout"]
        )

    def test_telegram_network_matches_default_config(self, template):
        """Числа связи с Телеграмом — документация, а не переопределение.

        Секция выведена в клиентский файл, потому что у клиента нет консоли:
        поправить таймаут иначе он не может никак, а поддержка не имеет
        права дописывать переменную в .env. Значения обязаны совпадать с
        DEFAULT_CONFIG — иначе файл, который клиент читает как описание
        продукта, обещает не то, что делает код.

        Отношение, а не снимок: если апстрим сменит дефолт, тест покраснеет
        и заставит поправить и число, и комментарий рядом с ним.
        """
        assert template["telegram"]["network"] == DEFAULT_CONFIG["telegram"]["network"]


class TestTelegramPlatformHint:
    """Подсказка резолвится в agent/system_prompt.py::_resolve_platform_hint,
    которая читает agent._platform_hint_overrides (заполняется в
    agent/agent_init.py из config.yaml platform_hints) и матчит его по
    agent.platform.lower() -- для Telegram это буквально "telegram"
    (plugins/platforms/telegram/adapter.py регистрирует адаптер с
    name="telegram"). Путь чтения реальный, ключ платформы верный.
    """

    def test_hint_names_the_working_folder_and_public_ports(self, template):
        hint = template["platform_hints"]["telegram"]["append"]
        assert "/workspace" in hint

        # Нижнюю границу демо-диапазона берём из самого шаблона, а не как
        # отдельный литерал: если диапазон когда-нибудь сменится, тест
        # покажет расхождение между подсказкой и конфигом, а не промолчит.
        spec = template["terminal"]["docker_extra_args"][1]
        host_range = spec.split(":", 1)[0]
        low_port = host_range.split("-", 1)[0]
        assert low_port in hint

        for word in ("публич", "ссылк"):
            assert word in hint.lower(), f"подсказка не говорит про {word}"


class TestTemplateCommentsMatchBehavior:
    """Комментарии шаблона — часть продукта, а не заметки на полях.

    Три инварианта ниже ловят ровно то, что нашло финальное ревью: пример
    строки ожидания разошёлся со словарём, а комментарий про подтверждение
    обещал вопрос там, где на самом деле будет отказ.
    """

    def test_heartbeat_example_matches_what_the_product_renders(self, template_text):
        """Пример "⏳ 3 мин — читаю файл" собираем настоящим продуктовым кодом.

        Раньше в шаблоне стояло "читаю документ", а словарь
        (``hermes_cli/trix_tool_names.py``) отдавал "читаю файл": клиент читал
        в документации одно, а видел в чате другое. Литерал в тесте эту пару
        не свёл бы — нужен именно вызов.
        """
        from hermes_cli.trix_status import build_heartbeat_text

        rendered = build_heartbeat_text(minutes=3, tool_name="read_file", lang="ru")
        assert f'"{rendered}"' in template_text, (
            f"пример в комментарии разошёлся с продуктом: ожидается {rendered!r}"
        )

    def test_approvals_comment_admits_cron_denies_instead_of_asking(self, template_text):
        """В задаче по расписанию человека нет, и удаление БЛОКИРУЕТСЯ.

        Комментарий, обещающий вопрос, обещает клиенту то, чего в этом
        сценарии не произойдёт. Привязываемся к настоящему режиму cron, а не
        к литералу: если продукт когда-нибудь начнёт спрашивать — тест
        покажет, что комментарий пора переписать обратно.
        """
        assert DEFAULT_CONFIG["approvals"]["cron_mode"] == "deny", (
            "продукт начал спрашивать в cron — комментарий шаблона пора "
            "переписывать обратно"
        )
        approvals_comment = template_text.split("approvals:", 1)[1].split("mode:", 1)[0]
        assert "по расписанию" in approvals_comment, (
            "комментарий про подтверждения молчит про задачи по расписанию, "
            "где спросить некого и удаление блокируется"
        )

    def test_agent_hint_warns_about_the_deletion_prompt(self, template):
        """Подсказка агенту обязана упомянуть сторож удаления.

        Без этого агент не может предупредить собеседника заранее и не
        понимает, почему получил отказ, — он про правило просто не знает.
        """
        hint = template["platform_hints"]["telegram"]["append"]
        lowered = hint.lower()
        assert "удал" in lowered and "подтвержд" in lowered, (
            "подсказка не говорит агенту, что удаление в рабочей папке "
            "вызовет вопрос собеседнику"
        )


class TestTimezoneSection:
    """Часовой пояс (спека 11).

    Решение владельца: в шаблоне пусто, отвечает клиент в мастере. Жёсткий
    ``Europe/Moscow`` отвергнут — он неверен для клиента из другого пояса, а
    досев вписал бы его на уже установленные машины при первом же
    ``hermes update``, то есть сменил бы пояс задним числом ровно там, где
    это признано небезопасным.
    """

    def test_template_carries_the_key_so_the_comment_reaches_the_client(self, template):
        """Ключ обязан быть В ФАЙЛЕ, а не только в умолчаниях.

        Без строки в шаблоне досеву нечего довозить, и объяснение — на что
        пояс влияет — до клиента не доедет вовсе.
        """
        assert "timezone" in template

    def test_template_value_is_empty_so_nobody_answers_for_the_client(self, template):
        assert template["timezone"] == ""

    def test_empty_template_value_agrees_with_what_the_product_does(self, template):
        """Обе стороны из разных мест: шаблон против умолчаний продукта.

        Если умолчание когда-нибудь перестанет быть пустым, комментарий
        шаблона («пусто — берётся время сервера») станет ложью, и тест
        покажет это раньше клиента.
        """
        assert template["timezone"] == DEFAULT_CONFIG["timezone"] == ""

    def test_comment_tells_the_client_what_an_empty_value_means(self, template_text):
        """Комментарий — часть продукта: клиент читает файл как документацию.

        Пустое значение — не «выключено», а «берётся время сервера», и
        именно этого клиент сам не угадает.
        """
        block = template_text.split("timezone:", 1)[0].rsplit("\n\n", 1)[-1]
        lowered = block.lower()
        assert "пусто" in lowered
        assert "сервер" in lowered

    def test_comment_names_what_actually_depends_on_the_zone(self, template_text):
        """Не общие слова: клиент должен понять, что сломается.

        От пояса зависят задачи по расписанию — это и надо назвать.
        """
        block = template_text.split("timezone:", 1)[0].rsplit("\n\n", 1)[-1]
        lowered = block.lower()
        assert "расписан" in lowered or "напомин" in lowered

    def test_wizard_answer_survives_a_later_default_stripping_save(self, tmp_path, monkeypatch):
        """Сквозь настоящую запись: пояс, выбранный клиентом, переживает
        ``save_config`` вместе с комментариями шаблона.

        ``save_config(strip_defaults=True)`` вырезает значения, равные
        умолчанию, а комментарии переживают только round-trip-писатель —
        обе половины проверяются исполнением, а не чтением исходника.
        """
        import shutil

        from hermes_constants import get_hermes_home

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        get_hermes_home().mkdir(parents=True, exist_ok=True)
        shutil.copy(TRIX_TEMPLATE_PATH, get_hermes_home() / "config.yaml")

        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["timezone"] = "Asia/Yekaterinburg"
        save_config(cfg)

        text = (get_hermes_home() / "config.yaml").read_text(encoding="utf-8")
        assert yaml.safe_load(text)["timezone"] == "Asia/Yekaterinburg"
        assert "часовой пояс" in text.lower()

    def test_untouched_empty_value_is_not_stripped_out_of_the_client_file(
        self, tmp_path, monkeypatch
    ):
        """Пустое значение равно умолчанию — и всё-таки обязано уцелеть.

        Иначе первая же запись мастера уносила бы строку вместе с
        объяснением, досев возвращал бы её на следующем обновлении, и файл
        клиента ходил бы по кругу.
        """
        import shutil

        from hermes_constants import get_hermes_home

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        get_hermes_home().mkdir(parents=True, exist_ok=True)
        shutil.copy(TRIX_TEMPLATE_PATH, get_hermes_home() / "config.yaml")

        from hermes_cli.config import load_config, save_config

        # Запись без единой правки: даже она не вправе унести строку.
        save_config(load_config())

        text = (get_hermes_home() / "config.yaml").read_text(encoding="utf-8")
        assert "timezone:" in text
        assert "часовой пояс" in text.lower()
