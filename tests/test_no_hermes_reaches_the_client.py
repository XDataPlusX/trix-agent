"""Главный приёмочный тест продукта: клиент не должен услышать «Hermes».

Проверяются ВСЕ каналы, которыми имя чужого продукта может дойти до
человека в чате, и проверяются они по СОБРАННЫМ объектам, а не по тексту
исходников: системный промпт целиком, схемы инструментов, которые видит
модель, и тексты скиллов, которые она читает.

Исключения перечислены поимённо и обоснованы: это настоящие имена,
переименование которых ломает работу ради подписи. Список закрытый —
новое имя сюда добавляется только вместе с обоснованием.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Настоящие идентификаторы: модуль, который агент импортирует, и команда,
#: которая действительно так называется. Переименование ломает работу.
ALLOWED_IDENTIFIERS = {
    "hermes_tools",   # генерируемый модуль, агент делает `from hermes_tools import`
    "hermes-agent",   # каталог исходников и апстримный репозиторий
    "hermes_cli",     # пакет кода
    "HERMES_HOME",    # переменная окружения
    "hermes curator", # настоящая подкоманда
    "hermes mcp",
    "hermes desktop", "hermes gui", "hermes dashboard", "hermes proxy",
    "hermes --tui",
    "hermes …",       # в тексте запрета: «не запускай hermes …»
    "which hermes",   # там же
    "hermes-",        # префикс имён тулсетов (hermes-telegram и т.п.)
    "HERMES.md", ".hermes.md",  # реальные имена файлов контекста
    "metadata.hermes",          # реальный ключ схемы frontmatter
    "HermesCLI",                # имя класса
    "hermes@", "HermesAgent",   # адреса/User-Agent в сторонних скриптах
    "NousResearch/hermes-agent",
}

#: Контексты, в которых имя стоит законно, потому что текст его ЗАПРЕЩАЕТ
#: или потому что это ключ схемы в YAML-виде (`metadata:` / `  hermes:`).
ALLOWED_CONTEXTS = (
    "cli does not exist",      # предупреждение в справочниках
    "cli is not in it",        # запрет в теле скилла
    "never run these",
    "do not run",
    "metadata:",               # frontmatter-ключ в YAML-форме
    "metadata namespace",      # историческая заметка о переносе скилла
    "cannot follow",           # запрет советовать клиенту команды
    "does not have",           # «которого у твоей песочницы нет»
    "built for [hermes agent]",  # авторская ссылка апстрима — сохраняется
    "get-shit-done-cc",        # флаг чужого npm-пакета, не наше имя
    "hermes_home:-",           # ${HERMES_HOME:-~/.hermes} — настоящий дефолт
    "_hermes_env",             # локальная переменная внутри shell-фрагмента
)


def _uncovered(text: str) -> list:
    """Вернуть упоминания, не покрытые списком настоящих идентификаторов."""
    out = []
    for m in re.finditer(r"[Hh]ermes", text or ""):
        # Пробелы схлопываются: в markdown фраза легко разорвана переносом
        # строки, и «does not\nhave» не совпало бы с «does not have».
        window = re.sub(r"\s+", " ", text[max(0, m.start() - 70): m.end() + 70])
        low = window.lower()
        if any(a.lower() in low for a in ALLOWED_IDENTIFIERS):
            continue
        if any(c in low for c in ALLOWED_CONTEXTS):
            continue
        out.append(window.strip())
    return out


@pytest.fixture(scope="module")
def telegram_prompt():
    """Настоящий системный промпт шлюзовой сессии, все три яруса."""
    import importlib.util

    from agent.system_prompt import build_system_prompt_parts

    # Загружаем помощник ПО ПУТИ, а не через пакет `tests`: добавление
    # каталога тестов в sys.path заслоняет настоящий hermes_cli его
    # одноимённым тестовым подкаталогом (ровно ловушка из CLAUDE.md).
    spec = importlib.util.spec_from_file_location(
        "_sysprompt_helper", ROOT / "tests" / "agent" / "test_system_prompt.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    _make_agent = helper._make_agent

    agent = _make_agent(platform="telegram",
                        valid_tool_names=["terminal", "secret_request"])
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)


class TestInternalIdentifiersStayInside:
    """Клиенту называется модель, но не внутренние ярлыки машины.

    Найдено владельцем 2026-09-08 по живому ответу: на «ты кто такой и на
    чём сделан» агент ответил «поверх модели GLM (glm-4.6) от провайдера
    **zai-coding-plan**». Имя модели — факт клиента: он сам выбрал
    провайдера и платит своим ключом. А `zai-coding-plan` — идентификатор
    профиля внутри продукта, клиент его нигде не вводил и не увидит.

    Правило добавлено в промпт, потому что раньше про модель там не было
    сказано НИЧЕГО, и агент просто отвечал на буквальный вопрос.
    """

    def test_prompt_keeps_the_model_out_of_introductions(self, telegram_prompt):
        """Решение владельца 2026-09-09, в две итерации.

        Сначала промпт разрешал называть модель — клиент сам её выбрал и
        платит своим ключом. На живых ответах вышло плохо: агент вставлял
        её туда, где не спрашивали («Я — Trix Agent… Модель GLM, обучена
        Z.AI») и в ответы про расходы.

        Первая правка запретила называть вовсе — и это оказалось
        перегибом: «пусть знает какая у него модель, просто не пишет при
        представлении». Итог — не секрет, а уместность.
        """
        whole = "\n".join(str(v) for v in (
            telegram_prompt.values() if isinstance(telegram_prompt, dict)
            else [telegram_prompt])).lower()
        assert "do not volunteer which model" in whole
        assert "say it plainly" in whole, (
            "промпт запрещает называть модель даже по прямому вопросу — "
            "это перегиб, снятый владельцем"
        )

    def test_prompt_forbids_internal_identifiers(self, telegram_prompt):
        whole = "\n".join(str(v) for v in (
            telegram_prompt.values() if isinstance(telegram_prompt, dict)
            else [telegram_prompt]))
        assert "provider profile id" in whole
        assert "toolset name" in whole


class TestRemoteTerminalBackendHint:
    """Подсказка про удалённый терминал — её видит ТОЛЬКО клиент.

    У клиента терминал работает в docker-песочнице, и тогда промпт
    получает блок «твои инструменты работают внутри этого окружения, а не
    на машине, где запущен агент». На машине разработчика бэкенд
    локальный, поэтому блок не собирается вовсе — и утечка в нём жила
    незамеченной, пока промпт не собрали на самом стенде 2026-09-08.

    Оба ветвления проверяются отдельно: с ответившей пробой и без неё.
    Тексты у них РАЗНЫЕ, и чинить пришлось оба.
    """

    @pytest.mark.parametrize(
        "probe",
        [None, "user=root\n$HOME=/root\ncwd=/workspace"],
        ids=["проба не ответила", "проба ответила"],
    )
    def test_no_stray_mention_in_remote_backend_hint(self, probe):
        from agent.prompt_builder import build_environment_hints

        with (
            patch.dict(os.environ, {"TERMINAL_ENV": "docker"}),
            patch("agent.prompt_builder._probe_remote_backend", return_value=probe),
        ):
            hints = build_environment_hints()

        assert "docker" in hints.lower(), (
            "блок про удалённый бэкенд не собрался — тест ничего не проверил"
        )
        assert _uncovered(hints) == []


class TestSystemPrompt:
    """Промпт читается моделью на каждом ходу — самый частый канал."""

    def test_no_stray_mention_in_any_tier(self, telegram_prompt):
        bad = {}
        for tier, text in telegram_prompt.items():
            found = _uncovered(text)
            if found:
                bad[tier] = found
        assert not bad, json.dumps(bad, ensure_ascii=False, indent=2)

    def test_the_agent_is_told_it_is_trix(self, telegram_prompt):
        joined = " ".join(telegram_prompt.values())
        assert "Trix" in joined


class TestToolSchemas:
    """Схемы уезжают модели с КАЖДЫМ запросом."""

    @pytest.fixture(scope="class")
    def defs(self):
        import contextlib
        import io

        import model_tools

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return model_tools.get_tool_definitions(enabled_toolsets=[
                "terminal", "file", "search", "skills", "memory", "todo",
                "clarify", "secrets", "session_search", "cronjob",
            ])

    def test_no_stray_mention_in_any_schema(self, defs):
        bad = {}
        for d in defs:
            name = (d.get("function") or {}).get("name", "?")
            found = _uncovered(json.dumps(d, ensure_ascii=False))
            if found:
                bad[name] = found
        assert not bad, json.dumps(bad, ensure_ascii=False, indent=2)

    def test_the_client_facing_secret_tool_is_present(self, defs):
        """Заодно охраняет спеку 19: инструмент не должен исчезнуть из схемы."""
        names = {(d.get("function") or {}).get("name") for d in defs}
        assert "secret_request" in names


class TestSkillsTheAgentReads:
    """Скилл грузится целиком в ответ инструмента и читается моделью.

    Граница проведена по тому, ЧТО грузится и когда:

    * тело `SKILL.md` и все прочие скиллы — чисто, без оговорок: это
      читается при обычной работе;
    * справочники `trix-agent/references/*` называют настоящие команды
      (`hermes config set` — она правда так называется), но обязаны нести
      предупреждение В НАЧАЛЕ, в том же ответе инструмента. Удалить оттуда
      имена значит сломать справочник для нас и поддержки, у которых CLI
      работает.
    """

    REFERENCES = ROOT / "skills" / "autonomous-ai-agents" / "trix-agent" / "references"

    def test_every_skill_outside_the_host_references_is_clean(self):
        bad = {}
        for f in sorted((ROOT / "skills").rglob("*.md")):
            if self.REFERENCES in f.parents:
                continue
            found = _uncovered(f.read_text(errors="replace"))
            if found:
                bad[str(f.relative_to(ROOT))] = found[:3]
        assert not bad, json.dumps(bad, ensure_ascii=False, indent=2)[:3000]

    def test_every_reference_that_names_a_command_carries_the_warning(self):
        """Предупреждение работает именно тем, что стоит рядом с командой —
        факт в системном промпте на этом месте проигрывал (наблюдение
        2026-09-06)."""
        missing = []
        for f in sorted(self.REFERENCES.glob("*.md")):
            text = f.read_text(errors="replace")
            if not re.search(r"`hermes [a-z]", text):
                continue
            head = text[:900]
            if "**Environment.**" not in head:
                missing.append(f.name)
        assert not missing, f"справочники с командами без предупреждения: {missing}"

    def test_the_warning_forbids_relaying_commands_to_the_client(self):
        """Само предупреждение обязано запрещать пересказ, а не только запуск."""
        text = (self.REFERENCES / "configuration.md").read_text(encoding="utf-8")
        head = text[:900]
        assert "never run these" in head
        assert "never pass them on" in head, "предупреждение не запрещает пересказ"
        assert "administrator" in head, (
            "предупреждение не закрывает обход через выдуманное третье лицо — "
            "модель адресовала команду «администратору машины», которого нет"
        )


class TestNothingPromisesAnUnreachableCommand:
    """Отдельно от имени: обещание команды, которой у клиента нет, —
    такая же поломка обещания, и её ловил живой прогон."""

    def test_skill_does_not_offer_skin_over_chat(self):
        body = (ROOT / "skills" / "autonomous-ai-agents" / "trix-agent"
                / "SKILL.md").read_text(encoding="utf-8")
        assert "sends /skin" not in body
        assert "applies a skin with `/skin`" not in body

    def test_skin_really_is_unavailable_in_the_gateway(self):
        """Основание для теста выше — проверяем, а не верим на слово."""
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS

        assert "skin" not in GATEWAY_KNOWN_COMMANDS
