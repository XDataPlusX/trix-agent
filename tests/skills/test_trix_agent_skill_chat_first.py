"""Справочник по продукту не должен гнать агента в CLI, которого у него нет.

Наблюдение за живым агентом 2026-09-06: на «увеличь таймаут» и «смени
тему» он читал этот скилл, выполнял `hermes config get`, не находил
бинарник, искал его по /usr/local/bin, /opt, /root/.local/bin, и в итоге
советовал клиенту выполнить `docker rm -f` на хосте. Клиент общается
через Телеграм и шелла не имеет.

Причина была не в незнании: факт «терминал в песочнице» уже стоял в
системном промпте, и агент его пересказывал верно. Он подчинялся скиллу,
который требовал проверить `--help` прежде, чем говорить «не могу».
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "autonomous-ai-agents" / "trix-agent"


@pytest.fixture(scope="module")
def body():
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


class TestTheHuntIsForbidden:
    def test_no_longer_orders_a_help_probe_before_saying_no(self, body):
        """Была формулировка «check --help before giving a negative answer» —
        предусловие на право отказать, из-за которого и шёл обыск."""
        assert "before giving a negative answer" not in body

    def test_forbids_probing_for_the_binary_explicitly(self, body):
        low = body.lower()
        assert "never probe for it" in low
        assert "which hermes" in low  # назван как то, чего делать НЕ надо

    def test_forbids_handing_a_command_to_anyone(self, body):
        """Второй наблюдавшийся провал: советы `docker rm -f` и journalctl.

        Запрет расширен 2026-09-08 после живого прогона релизной сборки:
        формулировку «не говори КЛИЕНТУ выполнять команду» модель обошла,
        адресовав команду третьему лицу — «передайте администратору
        машины команду `hermes config set terminal.timeout 300`».
        Администратора не существует: клиент владеет машиной и достаёт до
        неё только через этот чат. Обход — тот же запрещённый ответ в
        другой обёртке.
        """
        assert "Never hand a command to anyone" in body
        assert "administrator" in body, (
            "запрет не называет обход через выдуманное третье лицо"
        )

    def test_forbids_describing_internal_topology_to_the_client(self, body):
        assert "Never describe your own plumbing" in body


class TestChatPathsAreOffered:
    def test_skin_is_no_longer_forbidden(self, body):
        """Скилл ЗАПРЕЩАЛ единственный работающий путь: «don't tell the user
        to run /skin»."""
        assert "don't tell the user to run `/skin`" not in body
        assert "/skin" in body

    @pytest.mark.parametrize("path", ["/model", "/setup", "/debug", "secret_request",
                                      "cronjob", "skill_manage"])
    def test_names_the_paths_that_exist(self, body, path):
        assert path in body

    def test_spawning_section_points_at_delegate_task(self, body):
        """Раздел велел запускать `hermes chat` через терминал — гарантированный
        провал целой заявленной возможности."""
        assert "hermes chat" not in body
        assert "delegate_task" in body

    def test_config_invariant_no_longer_orders_the_cli(self, body):
        assert "use `hermes config set KEY VAL`" not in body


def _wizard_form_fields() -> set:
    """Поля, которые мастер настройки ДЕЙСТВИТЕЛЬНО принимает от клиента.

    Источник — сам мастер (`form.get("...")` в apply.py), а не память и не
    документация: договор здесь между текстом скилла и кодом, и проверять
    его надо по коду.
    """
    src = (ROOT / "hermes_cli" / "setup_wizard" / "apply.py").read_text(encoding="utf-8")
    return set(re.findall(r'form\.get\("([a-z_]+)"', src))


class TestReferencesDoNotOpenWithAnInstruction:
    """Забор и калитка не должны стоять рядом.

    Найдено живым прогоном 2026-09-08, третьим по счёту. В шапке
    `configuration.md` стояло предупреждение «никогда не запускай и никому
    не пересказывай», а ЧЕРЕЗ ДВЕ СТРОКИ — повелительное «Edit with
    `hermes config set section.key value`». Агент прочитал вторую строку и
    выдал клиенту ровно эту команду, помянув «администратора машины».

    Тот же урок в третий раз: свежая инструкция в загруженном справочнике
    бьёт правило в скилле. Значит правило обязано стоять ТАМ, где
    искушение, а не только в шапке.
    """

    def test_no_reference_opens_with_an_edit_instruction(self):
        """Первые строки справочника не должны учить, КАК менять настройку."""
        offenders = []
        for path in sorted((SKILL_DIR / "references").glob("*.md")):
            head = path.read_text(encoding="utf-8")[:1200]
            if re.search(r"^(Edit|Run|Use|Set) (with |it with )?`hermes ",
                         head, re.M):
                offenders.append(path.name)
        assert not offenders, (
            f"справочник открывается инструкцией, как менять: {offenders}"
        )

    def test_configuration_reference_names_the_chat_side_answer(self):
        """Мало запретить — надо сказать, что отвечать вместо этого."""
        text = (SKILL_DIR / "references" / "configuration.md").read_text(encoding="utf-8")
        head = text[:1400]
        assert "cannot be" in head and "chat surface" in head, (
            "справочник не говорит, что из чата настройки не меняются"
        )
        assert "XDataPlus support" in head, (
            "справочник не называет адрес, куда отправлять клиента"
        )


class TestSetupWizardPromiseMatchesTheWizard:
    """Скилл не должен обещать мастеру того, чего в нём нет.

    Найдено владельцем 2026-09-08 по живому ответу агента: на «увеличь
    таймаут терминала» агент отправил клиента в «/setup, раздел Terminal,
    поле timeout». Раздела нет, поля нет — мастер принимает пятнадцать
    полей, и таймаута среди них не значится. Агент не выдумывал: скилл
    трижды говорил «любая настройка → /setup», а справочник верно
    перечислял ключи config.yaml. Модель сложила одно с другим.

    Это тот же класс, что маршрут в удалённый themes.md: обещание пути,
    которого нет. Только теперь обещание сверяется с кодом.
    """

    def test_the_wizard_scan_actually_found_fields(self):
        """Иначе тест ниже проверял бы пустое множество."""
        assert len(_wizard_form_fields()) >= 10

    def test_every_field_the_skill_attributes_to_the_wizard_is_real(self, body):
        """Каждое поле, названное в строке про мастер, обязано быть в мастере."""
        row = next((ln for ln in body.splitlines()
                    if "`/setup`" in ln and "It covers exactly" in ln), None)
        assert row, "в скилле нет строки, перечисляющей охват мастера"
        claimed = set(re.findall(r"`([a-z_]+)`", row))
        real = _wizard_form_fields()
        invented = sorted(claimed - real)
        assert not invented, f"скилл приписывает мастеру несуществующие поля: {invented}"

    def test_settings_outside_the_wizard_go_to_support(self, body):
        """Решение владельца 2026-09-08: адреса ровно два, и третьего нет.

        Прежняя формулировка говорила только «из чата не меняется», и
        живой агент заполнял пустоту сам: сначала выдумал раздел мастера,
        потом выдал команду «для администратора машины». Пустое место в
        инструкции модель заполняет, поэтому названо, куда именно слать.
        """
        assert "XDataPlus support" in body, "не назван второй адрес — поддержка"
        assert "do not invent a wizard section" in body, (
            "скилл не запрещает выдумывать раздел мастера"
        )
        assert "support is the only address" in body, (
            "скилл не закрывает рассуждения о том, кто ещё мог бы это сделать"
        )


class TestRoutingTableGoesNowhereDead:
    """Скилл не должен посылать агента в файл, которого нет.

    Ровно этим кончилась просьба «смени тему»: таблица маршрутизации
    вела в `references/themes.md`, агент грузил его и шёл искать
    `hermes skin list` — хотя абзацем ниже тот же скилл говорил, что тем
    в чате не бывает. Справочник удалён 2026-09-08 (решение владельца:
    «скин мне не нужен, у меня Telegram»), и тест закрывает весь класс, а
    не один файл: любой маршрут обязан существовать на диске.
    """

    def test_every_routed_path_exists(self, body):
        routed = set(re.findall(r"`(references/[\w.-]+|templates/[\w.-]+)`", body))
        assert routed, "маршрутов не найдено — тест ничего не проверил"
        missing = sorted(r for r in routed if not (SKILL_DIR / r).exists())
        assert not missing, f"скилл ведёт в несуществующее: {missing}"

    def test_theming_is_not_routed_anywhere(self, body):
        """Тем в чате нет — значит и грузить по этому поводу нечего."""
        assert "themes.md" not in body
        assert "skin.yaml" not in body


def _references_that_name_host_commands():
    """Справочники, где названы команды хоста, — их и надо предупреждать.

    Список СЧИТАЕТСЯ С ДИСКА, а не держится руками. Прежняя версия
    перечисляла десять файлов поимённо, и пять справочников — среди них
    slash-commands.md, desktop-plugins.md и tui-widgets.md — годами не
    проверялись просто потому, что их забыли вписать. Найдено 2026-09-08
    сплошной проверкой каталога.
    """
    out = []
    for path in sorted((SKILL_DIR / "references").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"`hermes |`/[a-z]+|`npm |`systemctl |`docker ", text):
            out.append(path.name)
    return out


class TestReferencesCarryTheWarning:
    @pytest.mark.parametrize("name", _references_that_name_host_commands())
    def test_reference_warns_before_its_commands(self, name):
        """Предупреждение стоит в ТОМ ЖЕ ответе инструмента, что и команды —
        именно поэтому оно работает там, где факт в промпте проиграл."""
        t = (SKILL_DIR / "references" / name).read_text(encoding="utf-8")
        head = t[:t.index("\n#", 5)] if "\n#" in t[5:] else t[:600]
        assert "**Environment.**" in head, f"{name}: предупреждение не в начале"

    def test_the_sweep_actually_found_references(self):
        """Иначе параметризация свернулась бы в ноль и тест молчал."""
        assert len(_references_that_name_host_commands()) >= 8

    def test_no_reference_orders_the_agent_to_run_the_cli(self):
        """petdex.md велел «Use the `terminal` tool to run `hermes pets`» —
        ровно то предписание, из-за которого агент уходил искать бинарник."""
        offenders = []
        for path in sorted((SKILL_DIR / "references").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"use the `terminal` tool to run"
                         r"|^\s*run `hermes "
                         r"|then run `hermes ", text, re.M | re.I):
                offenders.append(path.name)
        assert not offenders, f"справочник велит запускать CLI: {offenders}"

    def test_cli_catalog_is_not_routed_to_anymore(self, body):
        """148 строк каталога команд, ни одна из которых неприменима."""
        assert "references/cli-reference.md" not in body


class TestItStaysUsableOnTheHost:
    def test_reference_content_is_kept_not_deleted(self):
        """Мы и поддержка читаем тот же скилл там, где CLI работает.
        Команды не удалены — они помечены как хостовые."""
        t = (SKILL_DIR / "references" / "configuration.md").read_text(encoding="utf-8")
        assert "hermes config set" in t
