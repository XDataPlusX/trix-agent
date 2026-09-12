"""Tests for the Trix curated .env template and its resolver.

``resolve_env_template()`` is the single, testable place that decides which
file a fresh install copies — see ``hermes_cli/config_template.py``.

**Сентябрь 2026, решение владельца: шаблон стал полным.** Раньше здесь было
~45 строк на 8 переменных вместо апстримных 496 — и узнать про остальные было
негде. Теперь кураторская часть (те же 8 с русскими пояснениями) осталась
ровно как была, а под ней перечислены ВСЕ переменные реестра,
закомментированными. Собирается это ``scripts/build_trix_env.py``.

Почему для ``.env`` это дешевле, чем для ``config.yaml``: за ``.env`` не
стоит ``DEFAULT_CONFIG``, поэтому закомментированная переменная и
отсутствующая — для программы буквально одно и то же. Досеянная часть ничего
не включает и ничего не «замораживает»; это чистая документация.

Правила, которые от этого НЕ изменились и проверяются ниже: заполнять клиенту
нужно по-прежнему не больше шести строк (остальные под решёткой и в бюджет не
входят), в ``.env`` по-прежнему только секреты, и ни одного постороннего имени.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

# Именно ``hermes_cli.config``, а не ``config_defaults``: первый при импорте
# досыпает в тот же объект ``OPTIONAL_ENV_VARS`` переменные всех найденных
# плагинов-провайдеров (``_inject_profile_env_vars``). Импорт
# ``config_defaults`` дал бы полный набор только при удачном порядке импортов
# в процессе, а при неудачном — усечённый, и тесты зависели бы от того, кто
# загрузился первым. Плагины при этом ищутся в репозитории и в
# ``$HERMES_HOME/plugins``, а conftest уводит HERMES_HOME в временный каталог,
# так что личные плагины разработчика в набор не попадают — так же, как при
# сборке шаблона.
from hermes_cli.config import OPTIONAL_ENV_VARS
from hermes_cli.config_template import resolve_env_template

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_ENV_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix.env.example"

# Exactly three variables the customer MUST fill in (per spec §3.5 /
# §6): the Telegram bot token, the allow-list of Telegram user ids, and
# ONE of two alternative provider-key lines (both ship empty; the
# customer fills whichever provider they picked).
REQUIRED_VAR_NAMES = frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"})
REQUIRED_PROVIDER_KEY_ALTERNATIVES = frozenset({"OPENROUTER_API_KEY", "GLM_API_KEY"})

# Names that are real, code-consumed env vars but are NOT part of
# hermes_cli.config_defaults.OPTIONAL_ENV_VARS (the setup-wizard/`hermes
# tools` prompt catalog) -- so the "every name is known to the product"
# check below needs an explicit allowance for them, same pattern as task
# 1's _VERSION_MARKER_KEYS.
#
# - TELEGRAM_HOME_CHANNEL: read by tools/environments/local.py,
#   hermes_cli/setup.py, hermes_cli/status.py, and the Telegram adapter's
#   cron-delivery target. Not in OPTIONAL_ENV_VARS because the setup
#   wizard never *prompts* for it directly -- it is normally captured via
#   the `/sethome` chat command instead (see hermes_cli/setup.py:1947).
# - NO_PROXY / no_proxy: standard proxy-bypass env vars consumed by
#   Python's own proxy resolution and by
#   tools/environments/docker.py / plugins/platforms/slack/adapter.py's
#   is_host_excluded_by_no_proxy(). Not a credential, so it was never a
#   candidate for the setup-wizard prompt catalog in the first place.
KNOWN_BUT_NOT_IN_OPTIONAL_ENV_VARS = frozenset(
    {"TELEGRAM_HOME_CHANNEL", "NO_PROXY", "no_proxy"}
)

# Suffixes that mark a name as a non-secret behavioral setting under the
# project rule "secrets only in .env" (see CLAUDE.md's "What we don't
# want" section) -- these belong in config.yaml, never in a .env template.
_NON_SECRET_SUFFIXES = ("_TIMEOUT", "_DEBUG", "_INTERVAL", "_LIMIT")

# Client input required, per spec §1's "half-ready loop" acceptance bar.
_MAX_REQUIRED_INPUT_VARS = 6


@pytest.fixture(scope="module")
def template_text() -> str:
    return TRIX_ENV_TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def active_values(template_text: str) -> dict:
    """Variables python-dotenv actually parses as active (uncommented)
    assignments -- exactly what a real ``load_dotenv()`` call would see."""
    return dotenv_values(stream=io.StringIO(template_text))


@pytest.fixture(scope="module")
def all_declared_names(template_text: str) -> set:
    """Every variable name mentioned in the template, whether active or
    commented out in the optional section.

    Uses python-dotenv's real parser (not a hand-rolled name regex) --
    a copy of the file with every ``# NAME=...`` line uncommented is fed
    through the same ``dotenv_values()`` the active-values fixture uses,
    so quoting/whitespace parsing stays identical to what the loader
    itself would do if the line were active.
    """
    uncommented = re.sub(
        r"^#\s*([A-Za-z_][A-Za-z0-9_]*=.*)$", r"\1", template_text, flags=re.MULTILINE
    )
    return set(dotenv_values(stream=io.StringIO(uncommented)).keys())


# ---------------------------------------------------------------------------
# resolve_env_template()
# ---------------------------------------------------------------------------


class TestResolveEnvTemplate:
    def test_prefers_trix_template_when_present(self, tmp_path):
        assets_dir = tmp_path / "assets" / "config"
        assets_dir.mkdir(parents=True)
        trix_file = assets_dir / "trix.env.example"
        trix_file.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
        upstream_file = tmp_path / ".env.example"
        upstream_file.write_text("SECRET=1\n", encoding="utf-8")

        result = resolve_env_template(tmp_path)

        assert result == trix_file

    def test_falls_back_to_upstream_example(self, tmp_path):
        upstream_file = tmp_path / ".env.example"
        upstream_file.write_text("SECRET=1\n", encoding="utf-8")

        result = resolve_env_template(tmp_path)

        assert result == upstream_file

    def test_returns_none_when_neither_exists(self, tmp_path):
        result = resolve_env_template(tmp_path)

        assert result is None


# ---------------------------------------------------------------------------
# assets/config/trix.env.example content
# ---------------------------------------------------------------------------


class TestTrixEnvTemplateContent:
    def test_template_file_exists(self):
        assert TRIX_ENV_TEMPLATE_PATH.is_file(), (
            f"expected curated template at {TRIX_ENV_TEMPLATE_PATH}"
        )

    def test_dotenv_values_gives_a_non_empty_dict(self, active_values):
        assert isinstance(active_values, dict)
        assert active_values, "dotenv_values() found no active assignments"

    def test_required_vars_present_and_empty(self, active_values):
        for name in REQUIRED_VAR_NAMES:
            assert name in active_values, f"required var {name} missing from template"
            assert not active_values[name], (
                f"required var {name} must ship EMPTY -- a non-empty value in "
                "the template would mean a leaked secret"
            )

    def test_at_least_one_provider_key_alternative_present_and_empty(
        self, active_values
    ):
        present = REQUIRED_PROVIDER_KEY_ALTERNATIVES & active_values.keys()
        assert present, (
            f"template must include at least one of "
            f"{sorted(REQUIRED_PROVIDER_KEY_ALTERNATIVES)}"
        )
        for name in present:
            assert not active_values[name], (
                f"provider key {name} must ship EMPTY -- a non-empty value "
                "would mean a leaked secret"
            )

    def test_every_declared_name_is_known_to_the_product(self, all_declared_names):
        # ``EXTRA`` — переменные, которые код читает и апстрим документирует,
        # а реестр не знает (проверено поимённо по коду: GROQ_API_KEY читает
        # tools/voice_mode.py, TELEGRAM_CRON_THREAD_ID — cron/scheduler.py).
        # Реестру они неизвестны потому, что мастер настройки о них не
        # спрашивает, а не потому, что их никто не читает.
        extra = _build_script_module().EXTRA
        unknown = [
            name
            for name in all_declared_names
            if name not in OPTIONAL_ENV_VARS
            and name not in extra
            and name not in REQUIRED_VAR_NAMES
            and name not in REQUIRED_PROVIDER_KEY_ALTERNATIVES
            and name not in KNOWN_BUT_NOT_IN_OPTIONAL_ENV_VARS
        ]
        assert not unknown, (
            f"unknown env var name(s) in trix.env.example: {unknown} -- a typo "
            "here means the customer fills in a variable nothing reads"
        )

    def test_no_non_secret_settings_in_template(self, all_declared_names):
        """Project rule: '.env' is for secrets only -- timeouts, debug
        flags, intervals, and limits belong in config.yaml."""
        offenders = [
            name
            for name in all_declared_names
            if name.upper().endswith(_NON_SECRET_SUFFIXES)
        ]
        assert not offenders, (
            f"non-secret setting(s) leaked into the .env template: {offenders} "
            "-- behavioral settings belong in config.yaml"
        )

    def test_client_input_count_within_half_ready_loop_budget(self, active_values):
        """Spec §1's acceptance bar for the 'half-ready loop': at most six
        uncommented, empty variables that require the customer to type
        something in before the agent can run."""
        needs_input = [name for name, value in active_values.items() if not value]
        assert len(needs_input) <= _MAX_REQUIRED_INPUT_VARS, (
            f"template asks the customer to fill in {len(needs_input)} "
            f"variable(s) ({sorted(needs_input)}), more than the budget of "
            f"{_MAX_REQUIRED_INPUT_VARS}"
        )


class TestTrixEnvTemplateProseFixes:
    """Regression coverage for wording issues a review round found: the
    template must not point the customer at a file it never installs
    (Important 3), must not sell a key that does nothing without a
    matching config.yaml edit (Important 4), must explain how to
    uncomment an optional line (Minor), and must not overclaim that a
    missing required var stops the agent from starting at all (Nit) --
    TELEGRAM_ALLOWED_USERS empty just denies everyone, and an empty
    provider key lets the process start and only fails on the first
    message.
    """

    def test_does_not_point_at_a_file_the_customer_does_not_have(self, template_text):
        """.env.example lives in the install directory
        ($HERMES_HOME/hermes-agent/.env.example), not next to the
        customer's ~/.hermes/.env -- the template must not send the
        customer looking for a file that was never copied there."""
        assert ".env.example" not in template_text

    def test_optional_section_explains_how_to_activate_a_line(self, template_text):
        optional_start = template_text.index("ПО ЖЕЛАНИЮ")
        first_var_start = template_text.index("# TELEGRAM_HOME_CHANNEL=")
        header = template_text[optional_start:first_var_start]
        assert "#" in header and (
            "уберите" in header.lower() or "удалите" in header.lower()
        ), "optional-section header must explain removing the leading '#' to activate a line"

    def test_firecrawl_comment_explains_the_config_yaml_toolset_swap(self, template_text):
        firecrawl_idx = template_text.index("FIRECRAWL_API_KEY=")
        # The explanatory comment block sits directly above the variable.
        preceding = template_text[max(0, firecrawl_idx - 500):firecrawl_idx]
        assert "config.yaml" in preceding, (
            "FIRECRAWL_API_KEY's comment must say the key alone does nothing -- "
            "web_extract also requires swapping 'search' for 'web' in "
            "platform_toolsets.telegram in config.yaml"
        )
        assert "web" in preceding and "search" in preceding

    def test_required_section_header_does_not_overclaim(self, template_text):
        assert "не запустится" not in template_text, (
            "an empty TELEGRAM_ALLOWED_USERS still lets the process start (it just "
            "denies everyone), and an empty provider key starts and fails on the "
            "first message -- neither prevents the agent from starting at all"
        )


# ---------------------------------------------------------------------------
# Полный список (сентябрь 2026)
#
# Кураторская часть выше проверяется тестами, которые были здесь до перехода
# на полный шаблон, — они не изменились ни на строку. Ниже — контракт самой
# досеянной части.
# ---------------------------------------------------------------------------


def _build_script_module():
    """``scripts/build_trix_env.py`` — не пакет, грузим по пути."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_trix_env", REPO_ROOT / "scripts" / "build_trix_env.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTemplateListsEveryVariable:
    def test_every_registry_variable_is_listed_or_deliberately_excluded(
        self, all_declared_names
    ):
        """Полнота: переменная либо в шаблоне, либо исключена с причиной."""
        script = _build_script_module()
        missing = [
            name
            for name, meta in OPTIONAL_ENV_VARS.items()
            if name not in all_declared_names and not script.is_excluded(name, meta)
        ]
        assert not missing, (
            f"в шаблоне .env нет {len(missing)} переменных: {sorted(missing)[:15]} — "
            "довести: python3 scripts/build_trix_env.py"
        )

    def test_upstream_documents_nothing_the_template_passes_over_in_silence(
        self, all_declared_names
    ):
        """Второй источник полноты — апстримный ``.env.example``.

        Реестр не полон: 50 переменных, которые код читает и апстрим
        документирует (``GROQ_API_KEY``, ``TELEGRAM_WEBHOOK_*``,
        ``TERMINAL_SSH_*``), ему неизвестны вовсе — на одном реестре они
        пропали из клиентского файла молча. Имя, которого реестр не знает,
        поэтому обязано быть либо в файле, либо в ``EXCLUDED``: правило про
        каналы связи к нему неприменимо, у него нет категории.
        """
        script = _build_script_module()
        upstream = script._declared_names(
            script.UPSTREAM_EXAMPLE.read_text(encoding="utf-8")
        )
        assert upstream, "апстримный .env.example не прочитался — источник потерян"

        missing = [
            name
            for name in sorted(upstream)
            if name not in all_declared_names
            and name not in OPTIONAL_ENV_VARS
            and name not in script.EXCLUDED
        ]
        assert not missing, (
            f"апстрим документирует {len(missing)} переменных, о которых наш "
            f"файл молчит: {missing[:15]} — вписать в EXTRA или в EXCLUDED "
            "с причиной"
        )

    def test_every_exclusion_carries_a_real_reason(self):
        """Исключение без письменного обоснования — тихая дыра, не решение."""
        script = _build_script_module()
        for name, reason in script.EXCLUDED.items():
            assert isinstance(reason, str) and len(reason.strip()) >= 40, (
                f"EXCLUDED[{name!r}] без внятной причины ({reason!r})"
            )
        assert len(script.CHANNELS_REASON.strip()) >= 40, (
            "правило «каналы связи, кроме Telegram» накрывает 186 переменных "
            "разом — тем более обязано нести письменную причину"
        )

    def test_other_channels_are_gone_and_telegram_stayed(self, all_declared_names):
        """Продуктовое решение: в файле один канал — Telegram.

        Дословно то, ради чего файл переписан: 186 переменных категории
        ``messaging`` (Matrix, Mattermost, iMessage, QQ, WeCom, Feishu…) —
        это 664 строки английского текста про каналы, которых в поставке
        нет. Правило проверяется на самом реестре, а не на списке имён,
        поэтому канал, который апстрим добавит завтра, тоже не просочится.
        """
        script = _build_script_module()

        leaked = [
            name
            for name, meta in OPTIONAL_ENV_VARS.items()
            if meta.get("category") == "messaging"
            and not name.startswith(script.KEPT_MESSAGING_PREFIXES)
            and name in all_declared_names
        ]
        assert not leaked, f"каналы связи, которых в поставке нет: {leaked}"

        assert {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"} <= all_declared_names, (
            "вместе с чужими каналами вымело и наш собственный"
        )

    def test_every_description_override_reaches_the_file(self, all_declared_names):
        """Переписанное по-русски описание мёртвой переменной — мусор.

        Текст, который никто никогда не увидит, потому что сама строка в
        файл не попадает. Ровно это и случилось, когда из файла ушли каналы
        связи: одиннадцать переопределений (Matrix, IRC, ntfy, Photon…)
        остались описывать то, чего в файле больше нет.
        """
        dead = [
            name
            for name in _build_script_module().DESCRIPTION_OVERRIDES
            if name not in all_declared_names
        ]
        assert not dead, (
            f"описания переписаны для переменных, которых в файле нет: {dead}"
        )

    def test_sudo_password_is_not_offered_to_the_client(self, all_declared_names):
        """Строка под пароль root клиенту не предлагается.

        Владелец снял sudo с машины клиента (NOPASSWD убран, в код sudo не
        добавляем). Готовая строка ``SUDO_PASSWORD=`` приглашала бы вернуть
        ровно то, что сняли, — и положить пароль root в файл, который читает
        кто угодно с доступом к машине.
        """
        assert "SUDO_PASSWORD" not in all_declared_names
        assert "SUDO_PASSWORD" in _build_script_module().EXCLUDED

    def test_the_added_part_is_entirely_commented_out(self, active_values):
        """Досеянное ничего не включает: живых строк там нет.

        Это то, на чём держится «для .env это бесплатно»: закомментированная
        переменная и отсутствующая — для программы одно и то же. Живой
        строкой в шаблоне может быть только то, что было в кураторской части
        до перехода.
        """
        curated = {
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USERS",
            "OPENROUTER_API_KEY",
            "GLM_API_KEY",
            "NO_PROXY",
            "no_proxy",
        }
        extra = set(active_values) - curated
        assert not extra, (
            f"досеянная часть включила переменные живыми: {sorted(extra)} — "
            "каждая строка ниже маркера обязана оставаться под решёткой"
        )


class TestTemplateDoesNotAdvertiseAnotherProduct:
    """В тексте нет ИМЕНИ чужого продукта. Имя команды и имя переменной — можно.

    Три случая, которые нельзя мерить одной линейкой:

    * ``HERMES_LANGFUSE_SECRET_KEY`` — имя ПЕРЕМЕННОЙ. Переименовать нельзя,
      его читает код, и клиент видит его в сообщениях об ошибках.
    * ``hermes photon setup``, ``hermes config set`` — имя КОМАНДЫ, которую
      клиент набирает сам. Исполняемый файл называется так; заменить его в
      тексте значило бы дать неработающий совет.
    * ``Hermes``, ``Nous`` с большой буквы — имя ПРОДУКТА и компании. Вот
      этому в клиентском файле места нет.

    Отсюда правило: запрещено имя с большой буквы, разрешено в нижнем
    регистре. Единственный литерал-исключение — фраза слова-активатора
    ``Hey Hermes``: это то, что произносят вслух, скрыть его значило бы
    соврать в документации.
    """

    _PRODUCT_NAME = re.compile(r"\bHermes\b|\bNous\b|\bnous\w*")
    _LITERAL_OK = re.compile(r"Hey Hermes")

    def test_no_product_name_in_the_prose(self, template_text):
        offenders = [
            line
            for line in template_text.splitlines()
            if self._PRODUCT_NAME.search(line) and not self._LITERAL_OK.search(line)
        ]
        assert not offenders, (
            "имя чужого продукта в тексте клиентского .env:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_variable_names_themselves_are_left_alone(self, all_declared_names):
        """Обратная страховка: имена переменных НЕ переписаны.

        Замена ``HERMES_`` на своё в ИМЕНИ сделала бы строку молча
        нерабочей — код читает исходное имя. Тест держит это явно, чтобы
        попытка «дочистить брендинг» не поехала в имена.
        """
        hermes_named = {n for n in all_declared_names if n.startswith("HERMES_")}
        assert hermes_named, (
            "ни одной переменной с префиксом HERMES_ — похоже, имена всё-таки "
            "переписали, и эти строки больше ничего не включают"
        )
        assert hermes_named <= set(OPTIONAL_ENV_VARS), (
            f"имена, которых реестр не знает: "
            f"{sorted(hermes_named - set(OPTIONAL_ENV_VARS))}"
        )


class TestResolveTrixEnvTemplateOnly:
    """Для дописывания в ЖИВОЙ ``.env`` апстримного фолбэка быть не должно.

    ``resolve_env_template`` годится для «скопировать в пустое место»: нет
    нашего шаблона — апстримный лучше, чем ничего. Для дописывания в файл
    клиента он опасен: в апстримном ``.env.example`` одиннадцать ЖИВЫХ строк с
    поведенческими настройками (``TERMINAL_TIMEOUT``, ``BROWSER_*``,
    ``*_DEBUG``), и они включились бы у клиента сами. Ровно та же причина, по
    которой у ``config.yaml`` есть ``resolve_trix_config_template_only``.
    """

    def test_returns_the_curated_template_when_present(self, tmp_path):
        from hermes_cli.config_template import resolve_trix_env_template_only

        assets = tmp_path / "assets" / "config"
        assets.mkdir(parents=True)
        ours = assets / "trix.env.example"
        ours.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
        (tmp_path / ".env.example").write_text("TERMINAL_TIMEOUT=60\n", encoding="utf-8")

        assert resolve_trix_env_template_only(tmp_path) == ours

    def test_no_upstream_fallback_when_ours_is_absent(self, tmp_path):
        from hermes_cli.config_template import (
            resolve_env_template,
            resolve_trix_env_template_only,
        )

        upstream = tmp_path / ".env.example"
        upstream.write_text("TERMINAL_TIMEOUT=60\n", encoding="utf-8")

        # «в пустое место» — апстримный сойдёт
        assert resolve_env_template(tmp_path) == upstream
        # «дописать в живой файл» — только наш, или ничего
        assert resolve_trix_env_template_only(tmp_path) is None
