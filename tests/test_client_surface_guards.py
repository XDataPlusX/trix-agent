"""Сторожа поставки: что клиенту доезжать не должно.

Четыре инварианта, каждый — про уже случившийся класс ошибок, а не про
гипотезу. Все четыре ломались или могут сломаться от мёржа апстрима:
апстрим развивает десктоп, TUI и дашборд, и его тексты возвращаются в
наши файлы вместе с полезными правками.

Тесты проверяют СОБРАННЫЕ объекты и настоящие списки, а не текст
исходников: индекс скиллов собирается сборщиком промпта, список
исключений витрины читается из установщика.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

#: Поверхности САМОГО продукта, которых у клиента нет и не будет: он
#: живёт в чате и в мастере настройки на своей машине. Упоминание любой
#: из них в тексте, который читает модель, кончается тем, что модель
#: предлагает их клиенту.
#:
#: Список намеренно узкий — про наш продукт, а не про слова вообще.
#: «Desktop app» как название поля в консоли Google, «obsidian desktop»
#: как чужое приложение и инструкции по curl под Windows в скилле про
#: Notion сюда не попадают и не должны.
FOREIGN_SURFACES = (
    r"hermes\s+desktop",
    r"hermes\s+gui",
    r"hermes\s+dashboard",
    r"hermes\s+proxy",
    r"hermes\s+--tui",
    r"\bElectron\b",
    r"\bInk TUI\b",
)


def _skill_texts():
    """Все markdown-тексты поставки, которые может прочитать модель.

    `templates/` исключены: это файлы-заготовки, которые копируются
    пользователю, а не читаются как инструкция.
    """
    for path in sorted(SKILLS.rglob("*.md")):
        if "/templates/" in path.as_posix():
            continue
        yield path, path.read_text(encoding="utf-8", errors="replace")


class TestNoSkillAdvertisesASurfaceTheClientHasNot:
    """Инвариант 1 — ни один поставляемый скилл не рекламирует
    десктоп, дашборд, TUI или локальный прокси продукта.

    Ломалось по-настоящему: раздел «Other Surfaces» в скилле
    `trix-agent` существовал ровно для того, чтобы агент рассказывал про
    Electron-приложение под Windows и веб-дашборд, когда клиент
    спрашивает «что ты ещё умеешь».
    """

    def test_no_surface_mentions(self):
        bad = {}
        for path, text in _skill_texts():
            hits = [
                m.group(0)
                for pattern in FOREIGN_SURFACES
                for m in re.finditer(pattern, text, re.IGNORECASE)
            ]
            if hits:
                bad[path.relative_to(ROOT).as_posix()] = sorted(set(hits))
        assert not bad, (
            "скиллы рекламируют поверхности, которых у клиента нет: " + repr(bad)
        )


class TestBuiltIndexNamesNoConsoleCommands:
    """Инвариант 2 — собранный индекс скиллов не называет команд консоли.

    Индекс уходит в системный промпт на КАЖДОМ ходу. Его преамбула
    когда-то обещала модели, что у скилла «есть настоящие команды, вроде
    `hermes config set …`» — и модель предлагала их клиенту, у которого
    нет ни консоли, ни этого бинарника в песочнице.
    """

    @pytest.fixture(scope="class")
    def index(self):
        from unittest.mock import patch

        import agent.prompt_builder as pb

        # Индекс собирается по НАСТОЯЩЕЙ поставке репозитория, а не по
        # пустому временному HERMES_HOME, иначе тест проверяет пустую строку.
        with patch.object(pb, "get_skills_dir", lambda: SKILLS):
            text = pb.build_skills_system_prompt()
        assert text, "индекс собрался пустым — тест ничего не проверил"
        return text

    def test_index_is_not_empty_and_lists_the_product_skill(self, index):
        assert "trix-agent" in index

    def test_no_console_command_is_offered(self, index):
        commands = re.findall(r"`hermes [^`]{0,60}`", index)
        assert not commands, (
            "индекс скиллов называет команды консоли: " + repr(commands)
        )


class TestReleaseExclusionsMatchTheInstaller:
    """Инвариант 3 — витрина исключает ровно то, что исключает установщик.

    Два списка живут в разных файлах и на разных языках: питоновский
    кортеж в сборщике витрины и аргументы `git sparse-checkout` в
    установщике. Разъехавшись, они дают либо мусор в публичном
    репозитории, либо отсутствующий на машине клиента файл — и то и
    другое замечается не сразу.
    """

    @staticmethod
    def _installer_exclusions() -> set:
        text = (ROOT / "scripts" / "install.sh").read_text()
        m = re.search(r"sparse-checkout set --no-cone(.*?);\s*then", text, re.S)
        assert m, "в установщике не найден вызов sparse-checkout"
        # '!/scripts/desktop-update*' → 'scripts/desktop-update'
        return {
            p.lstrip("!/").rstrip("*")
            for p in re.findall(r"'(![^']+)'", m.group(1))
        }

    def test_lists_are_identical(self):
        from hermes_cli.release_tree import EXCLUDED_PATHS

        assert self._installer_exclusions() == set(EXCLUDED_PATHS)


class TestProductSkillDoesNotLinkOutOfTheDelivery:
    """Инвариант 4 — `related_skills` продуктового скилла не ведут туда,
    куда клиент дойти не может.

    До курирования там стояли `claude-code`, `codex`, `opencode` — чужие
    продукты, которых нет ни в поставке, ни в `optional-skills`. Модель
    читает эти имена как «рядом есть такой навык».
    """

    @staticmethod
    def _related() -> list:
        import yaml

        text = (SKILLS / "autonomous-ai-agents" / "trix-agent" / "SKILL.md").read_text()
        front = text.split("---", 2)[1]
        meta = yaml.safe_load(front)
        return (meta.get("metadata", {}).get("hermes", {}) or {}).get(
            "related_skills", []
        ) or []

    def test_every_related_skill_is_actually_delivered(self):
        delivered = {p.parent.name for p in SKILLS.rglob("SKILL.md")}
        dangling = [name for name in self._related() if name not in delivered]
        assert not dangling, (
            "related_skills ведут в скиллы, которых нет в поставке: "
            + repr(dangling)
        )


class TestTheRulesTheAgentCannotDiscoverAreInThePrompt:
    """Два факта окружения обязаны быть в промпте, а не только в скилле.

    Проверено живым прогоном 2026-09-09: правила стояли в `SKILL.md`, и в
    тех самых пробах, где они были нужны, агент скилл **не открывал
    вовсе** — цепочки шли сразу в `terminal`. Правило, которого агент не
    читает, не работает; поэтому эти три переехали в блок, уходящий в
    промпт на каждом ходу.

    Общее у них одно: изнутри песочницы неудача выглядит как удача.
    Порт вне диапазона привязывается; уснувшая фоновая команда «работает»
    до перезагрузки; голосовой ответ выглядит отправленным. Убедиться
    попыткой нельзя — значит, надо сказать заранее.
    """

    @pytest.fixture(scope="class")
    def prompt(self):
        from agent.prompt_builder import TRIX_AGENT_HELP_GUIDANCE

        return TRIX_AGENT_HELP_GUIDANCE

    def test_ports_the_agent_may_actually_use_are_named(self, prompt):
        assert "TRIX_PUBLISHED_PORTS" in prompt
        assert "scan" in prompt.lower()

    def test_a_promise_to_return_means_a_scheduled_job(self, prompt):
        low = prompt.lower()
        assert "cronjob" in low
        assert "promis" in low

    def test_the_model_is_not_volunteered_but_is_not_a_secret(self, prompt):
        """Уточнение владельца 2026-09-09: пусть знает и отвечает, если
        спросили, — просто не суёт это в представление.

        Первая редакция запрещала называть модель вовсе; это перегиб.
        Клиент сам выбрал провайдера и платит своим ключом — на прямой
        вопрос он вправе получить прямой ответ. Проблема была не в
        знании, а в том, что агент вставлял название туда, где его не
        спрашивали: «Я — Trix Agent… Модель GLM, обучена Z.AI».
        """
        low = prompt.lower()
        assert "do not volunteer which model" in low
        assert "introduce yourself" in low
        # И обратная сторона: прямой вопрос не остаётся без ответа.
        assert "say it plainly" in low


class TestCronScriptJobsAreNotOfferedToTheClient:
    """Скриптовые задания cron на клиентской раскладке не работают, и
    текст, который читает модель, не должен их предлагать.

    Почему это отдельный сторож. Команды агента идут в docker-песочницу, а
    скрипт задания cron запускается ХОСТОВЫМ процессом и ищется под
    `~/.hermes/scripts/` (`cron/scheduler.py` — код апстримный, у нас не
    менялся). Это две разные файловые системы: скрипт, который агент
    написал, лежит в `/workspace`, планировщик его там не ищет, и задание
    отказывает НЕ в момент создания, а в момент срабатывания — молча до
    первого тика.

    Апстрим при `backend: local` наоборот прямо учит агента писать такой
    скрипт (`website/docs/guides/cron-script-only.md` — режим `no_agent`,
    ноль токенов). Его справочники доезжают к нам мёржем, поэтому правило
    «здесь так нельзя» обязано жить в НАШЕМ тексте и быть закреплено.
    """

    @pytest.fixture(scope="class")
    def skill(self):
        return (SKILLS / "autonomous-ai-agents" / "trix-agent" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    @pytest.fixture(scope="class")
    def background_systems(self):
        return (
            SKILLS
            / "autonomous-ai-agents"
            / "trix-agent"
            / "references"
            / "background-systems.md"
        ).read_text(encoding="utf-8")

    def test_the_always_loaded_skill_forbids_both_knobs(self, skill):
        """Правило стоит в самом скилле, а не только в справочнике: до
        справочника модель может и не дойти, а запрет нужен ровно в тот
        момент, когда она заводит задание."""
        assert "no_agent" in skill
        assert "script" in skill
        row = next(line for line in skill.split("\n") if "cronjob" in line)
        assert "Never pass" in row, row
        assert "no_agent" in row, row

    def test_the_reference_explains_why_and_gives_the_working_shape(
        self, background_systems
    ):
        assert "Do NOT use `script` or `no_agent`" in background_systems
        # Причина названа: разные файловые системы, а не «не поддерживается».
        assert "host" in background_systems
        assert "/workspace" in background_systems
        # И дана рабочая замена, а не только запрет.
        assert "cronjob(action=" in background_systems

    def test_the_reference_no_longer_advertises_them_as_knobs(
        self, background_systems
    ):
        """Раньше строка «Per-job knobs» перечисляла `script` и
        `no_agent=True` наравне с рабочими полями — то есть текст,
        который читает модель, сам подсказывал недоступный путь."""
        knobs_line = next(
            line for line in background_systems.split("\n") if "Per-job knobs" in line
        )
        block = background_systems[background_systems.index(knobs_line) :]
        knobs_block = block[: block.index("- **Do NOT use")]
        assert "no_agent" not in knobs_block, knobs_block
        assert "`script`" not in knobs_block, knobs_block
