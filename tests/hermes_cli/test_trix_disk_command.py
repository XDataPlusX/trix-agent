"""`/disk` — одна команда с необязательным аргументом, в меню клиента.

Замер и уборка проверены соседними файлами (`test_trix_disk_report.py`,
`test_trix_disk_cleanup.py`). Здесь проверяется только команда: что она
объявлена во всех четырёх местах, что каждая поверхность отдаёт клиенту
один и тот же текст целиком, что показ ничего не удаляет, что уборка
сбрасывает кэш замера и что отказ уборки доходит до клиента, а не в лог.

Приём из Task 3: единица покрытия — весь поток, уходящий клиенту за один
вызов, а ожидаемое строится вызовом тех же функций форматирования, а не
вписыванием текста.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import hermes_cli.trix_disk as disk
from hermes_cli.commands import resolve_command
from hermes_cli.slash_exec import CommandContext, execute_command
from hermes_cli.trix_disk import (
    build_report,
    clean,
    format_clean_result,
    format_report,
)
from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS

GB = 1024 ** 3
MB = 1024 ** 2


class _Usage:
    def __init__(self, total: int, used: int, free: int) -> None:
        self.total, self.used, self.free = total, used, free


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _write_sparse(path: Path, size: int) -> None:
    """Разрежённый файл нужного ``st_size`` — замер считает именно его.

    Пишем дырку, а не сто пятьдесят мегабайт нулей: порог «предлагать
    уборку» задан в мегабайтах, и настоящие байты тут ни на что не
    влияют, кроме времени теста.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _populate(home: Path) -> None:
    """Дом клиента: документы, рабочая папка и служебное к уборке."""
    _write(home / "cache" / "documents" / "договор.pdf", 5_000)
    _write(home / "sandboxes" / "docker" / "default" / "workspace" / "index.html", 2_000)
    _write(home / "backups" / "pre-update.zip", 3 * MB)
    _write(home / "logs" / "agent.log.1", 512 * 1024)


@pytest.fixture(autouse=True)
def _no_sandbox_override(monkeypatch):
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Кэш замера — глобальный на процесс; тесты не должны его наследовать."""
    disk.invalidate_report_cache()
    yield
    disk.invalidate_report_cache()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    _populate(h)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


@pytest.fixture
def steady_disk(monkeypatch):
    """Занятость раздела — константа: иначе два замера подряд разойдутся
    на том, что на машине пишет кто-то ещё, и сверка потока станет флаки."""
    monkeypatch.setattr(
        disk, "_disk_usage", lambda p: (p, _Usage(100 * GB, 96 * GB, 4 * GB))
    )


def _event(text: str):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"),
    )


def _telegram_stream(text: str) -> list[str]:
    """Всё, что уходит клиенту в Telegram за один вызов команды."""
    from gateway.run import GatewayRunner

    reply = asyncio.run(GatewayRunner._handle_disk_command(None, _event(text)))
    return [reply] if reply else []


def _cli_stream(text: str) -> list[str]:
    """Всё, что печатает CLI за один вызов команды."""
    from cli import HermesCLI

    printed: list[str] = []
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._console_print = lambda payload, **_kwargs: printed.append(payload)
    assert cli_obj.process_command(text) is True
    return printed


# ---------------------------------------------------------------------------
# Четыре парные записи: без любой из них команда не появится у клиента
# ---------------------------------------------------------------------------


class TestTheCommandIsDeclaredEverywhere:
    def test_disk_is_one_command_with_an_optional_argument(self):
        cmd = resolve_command("disk")
        assert cmd is not None
        assert cmd.execute == "disk"
        assert "clean" in cmd.args_hint
        assert "clean" in cmd.subcommands

    def test_disk_is_in_the_client_menu(self):
        assert "disk" in CLIENT_MENU_COMMANDS

    def test_menu_cap_equals_the_curated_command_count(self, tmp_path, monkeypatch):
        """Инвариант спеки 4: кап меню равен числу отобранных команд.

        Поднять одно и забыть другое — освободившиеся слоты заполнятся
        командами навыков по алфавиту, и у клиента в меню появятся чужие
        английские названия. С Task 1b `max_commands` в шаблоне вообще не
        прописан — кап выводится из ``len(CLIENT_MENU_COMMANDS)`` в коде
        (``hermes_cli.commands._telegram_command_menu_config``), поэтому
        здесь ставится реальный шаблон в отдельный ``HERMES_HOME`` и
        проверяется выведенное значение, а не буквальный ключ YAML.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        template_text = Path("assets/config/trix-config.yaml").read_text(encoding="utf-8")
        (tmp_path / "config.yaml").write_text(template_text, encoding="utf-8")

        from hermes_cli.commands import telegram_menu_max_commands

        cap = telegram_menu_max_commands()
        assert cap == len(CLIENT_MENU_COMMANDS)

    def test_the_telegram_menu_entry_is_described_in_russian(self, monkeypatch):
        """Пункт меню без русского описания клиент читает по-английски.

        Язык выставляется здесь явно, потому что вся сюита по умолчанию
        пришпилена к английскому (`tests/conftest.py`): иначе апстримные
        тесты, замораживающие английские формулировки, ломались бы самим
        фактом локализации. Этот тест — про противоположное, про то, что
        клиент читает меню по-русски, поэтому пин ему надо снять. Без этой
        строки он проверял бы, что английское описание написано кириллицей,
        и молча краснел бы навсегда.
        """
        from agent import i18n
        from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()

        menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
        described = dict(menu)
        assert "disk" in described
        assert any("а" <= ch.lower() <= "я" for ch in described["disk"])


# ---------------------------------------------------------------------------
# Что именно уходит клиенту за один вызов — весь поток целиком
# ---------------------------------------------------------------------------


class TestTheWholeClientStream:
    def test_a_bare_disk_sends_exactly_the_report(self, home, steady_disk):
        stream = _telegram_stream("/disk")
        assert stream == [format_report(build_report(home))]

    def test_disk_clean_sends_exactly_the_cleanup_result(self, tmp_path, monkeypatch, steady_disk):
        """Ожидаемое считается на дереве-близнеце: уборка необратима."""
        real = tmp_path / "real" / ".hermes"
        twin = tmp_path / "twin" / ".hermes"
        _populate(real)
        _populate(twin)
        monkeypatch.setenv("HERMES_HOME", str(real))

        stream = _telegram_stream("/disk clean")
        assert stream == [format_clean_result(clean(twin))]

    def test_the_gateway_keeps_serving_everyone_else_while_measuring(
        self, home, steady_disk, monkeypatch
    ):
        """Обход диска идёт вне цикла событий.

        Цикл событий шлюза один на все разговоры и все платформы: замер
        на секунды прямо в нём — это молчание всего бота, а не только
        того, кто спросил. Сцена ниже отпускает замер ИЗ цикла событий,
        поэтому она завершается только если цикл в это время жив.
        """
        import threading

        released = threading.Event()
        real = disk.build_report

        def blocking(home_path, **kwargs):
            assert released.wait(5), "замер идёт в цикле событий — отпустить некому"
            return real(home_path, **kwargs)

        monkeypatch.setattr(disk, "build_report", blocking)

        async def scenario():
            from gateway.run import GatewayRunner

            task = asyncio.ensure_future(
                GatewayRunner._handle_disk_command(None, _event("/disk"))
            )
            await asyncio.sleep(0)
            released.set()
            return await task

        assert "%" in asyncio.run(scenario())

    def test_the_cli_says_the_same_thing_as_telegram(self, home, steady_disk):
        cli_text = execute_command("disk", CommandContext(surface="cli")).text
        gateway_text = execute_command("disk", CommandContext(surface="gateway")).text
        assert cli_text == gateway_text

    def test_the_cli_prints_the_report_and_nothing_else(self, home, steady_disk):
        printed = _cli_stream("/disk")
        assert printed == [format_report(build_report(home))]

    def test_the_cli_passes_the_argument_through(self, home, steady_disk):
        """Аргумент, потерянный по дороге, превратил бы уборку в показ."""
        printed = _cli_stream("/disk clean")
        assert not (home / "backups" / "pre-update.zip").exists()
        assert len(printed) == 1
        assert "освобожд" in printed[0].lower()


# ---------------------------------------------------------------------------
# Показ не удаляет; уборка удаляет; чужой аргумент — не уборка
# ---------------------------------------------------------------------------


class TestReportNeverDeletes:
    def test_bare_disk_reports_and_does_not_delete(self, home, steady_disk):
        reply = execute_command("disk", CommandContext(surface="gateway"))
        assert "%" in reply.text
        assert (home / "backups" / "pre-update.zip").exists(), "показ не должен удалять"
        assert (home / "logs" / "agent.log.1").exists()

    def test_disk_clean_deletes_service_files(self, home, steady_disk):
        reply = execute_command("disk", CommandContext(surface="gateway", args="clean"))
        assert not (home / "backups" / "pre-update.zip").exists()
        assert "освобожд" in reply.text.lower()
        assert (home / "cache" / "documents" / "договор.pdf").exists()

    @pytest.mark.parametrize("args", ["кло", "cleen", "clean --all", "clean up", "-clean"])
    def test_an_unknown_argument_deletes_nothing(self, args, home, steady_disk):
        """Только точное `clean` убирает: опечатка не должна сносить файлы.

        (Что клиенту при этом говорят — в TestTheThreeArgumentCases.)
        """
        reply = execute_command("disk", CommandContext(surface="gateway", args=args))
        assert "%" in reply.text
        assert (home / "backups" / "pre-update.zip").exists()

    @pytest.mark.parametrize("args", ["clean", " CLEAN ", "Clean"])
    def test_the_cleanup_word_is_recognized_however_it_is_typed(
        self, args, home, steady_disk
    ):
        execute_command("disk", CommandContext(surface="gateway", args=args))
        assert not (home / "backups" / "pre-update.zip").exists()

    def test_the_cleanup_does_not_go_near_docker(self, home, steady_disk, monkeypatch):
        """Образы Docker команда не трогает — решение, а не забывчивость.

        На машине клиента ровно один образ и один контейнер: агент до
        сокета Docker не дотягивается и образов не собирает. А вызов
        синхронный, в процессе шлюза, и на забитой машине идёт десятки
        секунд, пока человек ждёт ответа в чате. Размер образов он видит
        в разделе «Остальное».
        """
        called: list = []
        monkeypatch.setattr(disk, "docker_prune", lambda *a, **kw: called.append(a) or 0)
        execute_command("disk", CommandContext(surface="gateway", args="clean"))
        assert not called


# ---------------------------------------------------------------------------
# Три случая, которые нельзя перепутать: пусто / clean / что-то ещё
# ---------------------------------------------------------------------------


class TestTheThreeArgumentCases:
    """Нераспознанный аргумент не должен сваливаться ни в один из двух других.

    Свалится в уборку — снесёт файлы по опечатке. Свалится в голый показ —
    клиент прочтёт отчёт как «уборка прошла, убирать было нечего»,
    перестанет ждать и вернётся к молчащему боту на кончившемся диске.
    """

    def test_the_bare_form_is_the_report_and_nothing_more(self, home, steady_disk):
        text = execute_command("disk", CommandContext(surface="gateway")).text
        assert text == format_report(build_report(home))
        assert disk.UNRECOGNIZED_ARGUMENT not in text

    def test_an_unrecognized_argument_is_named_before_the_same_report(
        self, home, steady_disk
    ):
        text = execute_command("disk", CommandContext(surface="gateway", args="cleen")).text
        assert text.startswith(disk.UNRECOGNIZED_ARGUMENT)
        assert text.endswith(format_report(build_report(home)))

    def test_the_notice_offers_the_command_that_actually_cleans(self):
        """Готовое действие, а не упрёк: клиенту незачем гадать, как правильно."""
        assert "/disk clean" in disk.UNRECOGNIZED_ARGUMENT

    def test_the_recognized_word_gets_no_notice(self, home, steady_disk):
        text = execute_command("disk", CommandContext(surface="gateway", args="clean")).text
        assert disk.UNRECOGNIZED_ARGUMENT not in text
        assert "освобожд" in text.lower()

    def test_all_three_answers_differ(self, tmp_path, monkeypatch, steady_disk):
        """Каждый случай считается на своём доме: уборка необратима."""
        answers = []
        for name, args in (("bare", ""), ("typo", "cleen"), ("clean", "clean")):
            home_dir = tmp_path / name / ".hermes"
            _populate(home_dir)
            monkeypatch.setenv("HERMES_HOME", str(home_dir))
            answers.append(
                execute_command("disk", CommandContext(surface="gateway", args=args)).text
            )
        assert len(set(answers)) == 3


# ---------------------------------------------------------------------------
# Кэш замера: обход стоит секунды, а клиент ждёт ответа в чате
# ---------------------------------------------------------------------------


class TestTheMeasurementIsCached:
    @staticmethod
    def _count_measurements(monkeypatch) -> list:
        """Считаем обходы дерева — дорогую половину, ради которой кэш и есть.

        Не ``build_report``: он теперь вызывается на каждый ответ, потому
        что занятость раздела кэшировать нельзя (см. ``_TreeScan``).
        """
        calls: list = []
        real = disk._scan_tree

        def counted(home_path):
            calls.append(home_path)
            return real(home_path)

        monkeypatch.setattr(disk, "_scan_tree", counted)
        return calls

    def test_a_second_report_within_the_window_does_not_re_measure(
        self, home, steady_disk, monkeypatch
    ):
        calls = self._count_measurements(monkeypatch)
        first = execute_command("disk", CommandContext(surface="gateway")).text
        second = execute_command("disk", CommandContext(surface="gateway")).text
        assert len(calls) == 1, "второй /disk обязан прийти из кэша"
        assert first == second

    def test_the_window_expires(self, home, steady_disk, monkeypatch):
        calls = self._count_measurements(monkeypatch)
        clock = [1_000.0]
        monkeypatch.setattr(disk, "_now", lambda: clock[0])

        execute_command("disk", CommandContext(surface="gateway"))
        clock[0] += disk._REPORT_CACHE_TTL - 1
        execute_command("disk", CommandContext(surface="gateway"))
        assert len(calls) == 1, "внутри окна замер не повторяется"

        clock[0] += 2
        execute_command("disk", CommandContext(surface="gateway"))
        assert len(calls) == 2, "кэш не может держать числа вечно"

    def test_cleanup_drops_the_cache_so_the_client_sees_new_numbers(
        self, home, steady_disk, monkeypatch
    ):
        calls = self._count_measurements(monkeypatch)
        before = execute_command("disk", CommandContext(surface="gateway")).text
        execute_command("disk", CommandContext(surface="gateway", args="clean"))
        after = execute_command("disk", CommandContext(surface="gateway")).text
        assert len(calls) == 2, "после уборки замер обязан повториться"
        assert before != after, "клиент не должен читать доуборочные числа"

    def test_another_home_is_not_served_from_the_first_ones_cache(
        self, tmp_path, monkeypatch, steady_disk
    ):
        """Один процесс шлюза обслуживает несколько профилей."""
        one = tmp_path / "one" / ".hermes"
        two = tmp_path / "two" / ".hermes"
        _populate(one)
        _write(two / "backups" / "b.zip", 40 * MB)
        calls = self._count_measurements(monkeypatch)

        monkeypatch.setenv("HERMES_HOME", str(one))
        first = execute_command("disk", CommandContext(surface="gateway")).text
        monkeypatch.setenv("HERMES_HOME", str(two))
        second = execute_command("disk", CommandContext(surface="gateway")).text

        assert len(calls) == 2
        assert first != second


# ---------------------------------------------------------------------------
# Что кэшировать нельзя: занятость раздела стоит один системный вызов
# ---------------------------------------------------------------------------


class TestTheCheapHalfIsNeverFrozen:
    """Дорогой обход кэшируется, дешёвая занятость раздела — нет.

    Заморозив их вместе, мы получаем неподвижный процент на
    заполняющемся диске и непроизнесённый переход через порог — то есть
    ровно то предупреждение, ради которого команду и спрашивают.
    """

    @staticmethod
    def _usage_sequence(monkeypatch, *pairs) -> list:
        """Занятость раздела, меняющаяся от вызова к вызову."""
        remaining = list(pairs)
        seen: list = []

        def next_usage(path):
            used, free = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            seen.append(used)
            return path, _Usage(100 * GB, used, free)

        monkeypatch.setattr(disk, "_disk_usage", next_usage)
        return seen

    def test_the_percentage_moves_while_the_walk_stays_cached(self, home, monkeypatch):
        scans = TestTheMeasurementIsCached._count_measurements(monkeypatch)
        self._usage_sequence(monkeypatch, (40 * GB, 60 * GB), (70 * GB, 30 * GB))

        first = execute_command("disk", CommandContext(surface="gateway")).text
        second = execute_command("disk", CommandContext(surface="gateway")).text

        assert len(scans) == 1, "обход обязан остаться закэшированным"
        assert "40 %" in first and "70 %" in second

    def test_crossing_the_warning_threshold_is_not_muffled_by_the_cache(
        self, home, monkeypatch
    ):
        scans = TestTheMeasurementIsCached._count_measurements(monkeypatch)
        self._usage_sequence(monkeypatch, (40 * GB, 60 * GB), (96 * GB, 4 * GB))

        calm = execute_command("disk", CommandContext(surface="gateway")).text
        alarming = execute_command("disk", CommandContext(surface="gateway")).text

        assert len(scans) == 1
        assert "Места почти нет" not in calm
        assert "Места почти нет" in alarming

    def test_space_freed_by_someone_else_shows_up_at_once(self, home, monkeypatch):
        """Место освободил сам агент по просьбе клиента — числа обязаны сойтись.

        Иначе клиент читает отчёт как «агент соврал»: он видел, как файл
        удаляли, а команда показывает старое.
        """
        scans = TestTheMeasurementIsCached._count_measurements(monkeypatch)
        self._usage_sequence(monkeypatch, (90 * GB, 10 * GB), (50 * GB, 50 * GB))

        before = execute_command("disk", CommandContext(surface="gateway")).text
        after = execute_command("disk", CommandContext(surface="gateway")).text

        assert len(scans) == 1
        assert before != after
        assert disk._size(50 * GB) in after


# ---------------------------------------------------------------------------
# Гонка обхода с уборкой: окно равно длительности обхода
# ---------------------------------------------------------------------------


class TestACleanupDuringAScan:
    """Обход, обесцененный уборкой, не отдаётся клиенту и не садится в кэш.

    Окно равно длительности обхода — тем самым десяткам секунд, ради
    которых кэш и заведён, а ``busy_policy="dispatch"`` специально
    разрешает второй вызов поверх первого.

    Синхронизация явная, событиями: часы здесь ни при чём.
    """

    @staticmethod
    def _gate(monkeypatch, *, invalidate_after=()):
        """Придержать N-й обход и (по желанию) уронить в него уборку.

        Возвращает ``(scans, scan_started, may_finish)``. Придерживается
        только первый обход: повтор обязан пройти без задержки.
        """
        import threading

        scan_started = threading.Event()
        may_finish = threading.Event()
        scans: list = []
        real = disk._scan_tree

        def gated(home_path):
            result = real(home_path)
            scans.append(home_path)
            if len(scans) == 1:
                scan_started.set()
                assert may_finish.wait(10), "первый обход никто не отпустил"
            if len(scans) in invalidate_after:
                disk.invalidate_report_cache(home_path)
            return result

        monkeypatch.setattr(disk, "_scan_tree", gated)
        return scans, scan_started, may_finish

    @staticmethod
    def _ask_in_background(sink: dict, key: str = "text"):
        import threading

        def ask():
            sink[key] = execute_command(
                "disk", CommandContext(surface="gateway")
            ).text

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        return worker

    def test_the_early_asker_is_re_measured_not_served_stale(
        self, home, steady_disk, monkeypatch
    ):
        """Сторож сработал — значит меряем заново, а не отдаём обесцененное.

        Тот, кто спросил до уборки, получает диск таким, какой он стал:
        иначе он один раз, но прочтёт доуборочные числа сразу после
        «освобождено» — и перестанет верить обоим сообщениям.
        """
        scans, started, release = self._gate(monkeypatch)
        early: dict = {}
        worker = self._ask_in_background(early)
        assert started.wait(10), "обход не начался"

        # Уборка приходит, пока обход ещё держится. Она обязана пройти
        # НЕ дожидаясь его: замок стоит только на правках кэша.
        cleaned = execute_command(
            "disk", CommandContext(surface="gateway", args="clean")
        ).text
        assert "освобожд" in cleaned.lower()
        assert not (home / "backups" / "pre-update.zip").exists()

        release.set()
        worker.join(10)
        assert not worker.is_alive()

        assert len(scans) == 2, "обесцененный обход обязан быть перемерян"

        later = execute_command("disk", CommandContext(surface="gateway")).text
        assert len(scans) == 2, "перемеренный обход обязан осесть в кэше"

        # Эталон считается последним: он сам делает ещё один обход.
        fresh = format_report(build_report(home))
        assert early["text"] == fresh, "клиенту ушли доуборочные числа"
        assert later == fresh

    def test_the_three_contradicting_answers_are_gone(
        self, home, steady_disk, monkeypatch
    ):
        """Сценарий ревьюера целиком, в порядке доставки клиенту.

        Было: «Освобождено 150 МБ» → отчёт с доуборочными числами и
        приглашением «сократить на 150 МБ — команда /disk clean» →
        «Служебных файлов не нашлось». Инвариант, который это убивает:
        если отчёт зовёт убирать, следующая уборка обязана что-то
        освободить.
        """
        _write_sparse(home / "backups" / "big.zip", 150 * MB)
        scans, started, release = self._gate(monkeypatch)
        early: dict = {}
        worker = self._ask_in_background(early)
        assert started.wait(10)

        delivered = [
            execute_command("disk", CommandContext(surface="gateway", args="clean")).text
        ]
        release.set()
        worker.join(10)
        delivered.append(early["text"])

        assert "освобожд" in delivered[0].lower()
        assert not (home / "backups" / "big.zip").exists()

        invited = "/disk clean" in delivered[1]
        follow_up = execute_command(
            "disk", CommandContext(surface="gateway", args="clean")
        ).text
        freed = "освобожд" in follow_up.lower()
        assert invited == freed, (
            "отчёт зовёт убирать то, чего нет"
            if invited else "уборка нашла то, о чём отчёт промолчал"
        )
        assert not invited, "убирать уже нечего — звать некуда"
        assert len(scans) == 2

    def test_the_retry_happens_at_most_once(self, home, steady_disk, monkeypatch):
        """Уборка успевает и в повтор — отдаём что померили, а не кружим.

        Иначе при частых уборках клиент не получит ответа вообще.
        """
        scans, started, release = self._gate(monkeypatch, invalidate_after=(1, 2, 3))
        answer: dict = {}
        worker = self._ask_in_background(answer)
        assert started.wait(10)
        release.set()
        worker.join(10)

        assert not worker.is_alive(), "команда закружилась на перемерах"
        assert len(scans) == 2, f"обходов должно быть ровно два, а не {len(scans)}"
        assert "%" in answer["text"], "клиент остался без ответа"

    def test_the_client_is_not_invited_to_clean_what_is_already_gone(
        self, home, monkeypatch
    ):
        """Тот же инвариант для СЛЕДУЮЩЕГО спросившего — он читает кэш."""
        monkeypatch.setattr(
            disk, "_disk_usage", lambda p: (p, _Usage(100 * GB, 96 * GB, 4 * GB))
        )
        _write_sparse(home / "backups" / "big.zip", 200 * MB)

        scans, started, release = self._gate(monkeypatch)
        worker = self._ask_in_background({})
        assert started.wait(10)
        execute_command("disk", CommandContext(surface="gateway", args="clean"))
        release.set()
        worker.join(10)

        report = execute_command("disk", CommandContext(surface="gateway")).text
        assert "Места почти нет" in report, "порог должен сработать: занято 96 %"
        assert "/disk clean" not in report, "убирать уже нечего — звать некуда"


# ---------------------------------------------------------------------------
# Место чаще всего спрашивают, когда что-то встало
# ---------------------------------------------------------------------------


class TestItWorksWhileTheAgentIsBusy:
    def test_the_registry_declares_the_command_dispatchable_mid_run(self):
        assert resolve_command("disk").busy_policy == "dispatch"

    def test_a_busy_agent_still_gets_the_client_an_answer(self, home, steady_disk):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        result = asyncio.run(
            runner._dispatch_busy_slash_command(
                event=_event("/disk"),
                cmd_def=resolve_command("disk"),
                quick_key="k",
                source=None,
            )
        )
        assert result == format_report(build_report(home))
        assert "mid-turn" not in result


# ---------------------------------------------------------------------------
# Отказ уборки — клиенту словами, а не в лог
# ---------------------------------------------------------------------------


class TestRefusalsReachTheClient:
    @pytest.fixture
    def unremovable(self, monkeypatch):
        def refuse(path):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(disk, "_remove_tree", refuse)

    def test_the_client_is_told_the_cleanup_failed(self, home, steady_disk, unremovable):
        """Причина отказа доходит до клиента — но его словами.

        Раньше здесь ожидался английский ``Permission denied``, прилетавший
        из ``str(OSError)`` вместе с абсолютным путём на хосте. Клиенту
        полезна причина, а не код и не устройство сервера.
        """
        stream = _telegram_stream("/disk clean")
        assert len(stream) == 1
        assert "Убрать ничего не удалось" in stream[0]
        assert "не хватило прав" in stream[0]
        assert "Permission denied" not in stream[0]
        assert str(home) not in stream[0]
        assert (home / "backups" / "pre-update.zip").exists()

    def test_the_failure_carries_the_same_advice_as_a_full_disk(
        self, home, steady_disk, unremovable
    ):
        """Клиент читает эти два сообщения подряд — совет в них один.

        Ожидаемое берётся из общей константы модуля, а не вписывается
        текстом: инвариант — «оба текста зовут в одно и то же место»,
        а не «сегодня там такая строка».
        """
        failure = _telegram_stream("/disk clean")[0]
        full_disk = format_report(build_report(home))
        assert disk._ASK_THE_OWNER in full_disk
        assert disk._ASK_THE_OWNER in failure

    def test_a_partial_success_is_not_dressed_up_as_hopeless(
        self, home, steady_disk, monkeypatch
    ):
        """Часть убрана — совет «своими силами не решается» был бы ложью."""
        real = disk._remove_tree

        def refuse_logs_only(path):
            if "logs" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        monkeypatch.setattr(disk, "_remove_tree", refuse_logs_only)
        reply = _telegram_stream("/disk clean")[0]
        assert "освобожд" in reply.lower()
        assert "не хватило прав" in reply
        assert "Permission denied" not in reply
        assert disk._ASK_THE_OWNER not in reply


# ---------------------------------------------------------------------------
# Процент без запаса врёт на обоих концах диска.
#
# Найдено клиентом на живой машине 2026-09-04: бот прислал «Места на диске
# остаётся немного» и совет «нужен диск побольше или уборка на самом
# сервере» — при 84 свободных гигабайтах. Совет выдавался по одному
# условию, «убирать у себя мне почти нечего», а свободное место не
# проверялось вовсе.
#
# Тревога теперь смотрит и на запас. Проверки — про правило, а не про
# конкретные числа: они задают запас относительно порога, а не «19.6 ГБ».
# ---------------------------------------------------------------------------

_GB = 1024 ** 3


def _report_at(total_gb: float, used_percent: float):
    from hermes_cli.trix_disk import DiskReport

    total = int(total_gb * _GB)
    used = int(total * used_percent / 100)
    return DiskReport(
        total=total,
        used=used,
        free=total - used,
        used_percent=used_percent,
        documents_bytes=0,
        workspace_bytes=0,
        sessions_bytes=0,
        # Убирать нечего — именно эта ветка и печатала неверный совет.
        service_bytes=1024,
        other_bytes=used - 1024,
    )


class TestAlarmNeedsBothPercentAndHeadroom:
    def test_big_disk_over_the_percent_but_with_headroom_is_quiet(self):
        """Процент перейдён, а запаса вдоволь — говорить не о чем."""
        from hermes_cli.trix_disk import is_alarming

        assert is_alarming(80.0, 80.0, free_bytes=40 * _GB, min_free_bytes=10 * _GB) is False

    def test_small_disk_over_the_percent_without_headroom_still_alarms(self):
        """Тот же процент, но запаса нет — предупреждение обязано остаться."""
        from hermes_cli.trix_disk import is_alarming

        assert is_alarming(80.0, 80.0, free_bytes=2 * _GB, min_free_bytes=10 * _GB) is True

    def test_unknown_headroom_falls_back_to_the_percent(self):
        """Запас неизвестен — судим как раньше, молчать нельзя."""
        from hermes_cli.trix_disk import is_alarming

        assert is_alarming(80.0, 80.0, free_bytes=None, min_free_bytes=10 * _GB) is True

    def test_wrong_advice_is_not_offered_while_there_is_headroom(self):
        """Главное: клиента не отправляют просить диск побольше впустую."""
        from hermes_cli.trix_disk import WARN_SOFT, format_warning

        text = format_warning(
            WARN_SOFT, _report_at(98, 80.0), min_free_bytes=10 * _GB
        )
        assert "диск побольше" not in text, text

    def test_advice_is_still_offered_when_the_disk_really_is_full(self):
        """И не наоборот: на действительно забитом диске совет обязан быть."""
        from hermes_cli.trix_disk import WARN_SOFT, format_warning

        text = format_warning(
            WARN_SOFT, _report_at(20, 95.0), min_free_bytes=10 * _GB
        )
        assert "диск побольше" in text, text

    def test_headroom_threshold_is_configurable(self):
        """Число — настройка клиента, а не константа в коде."""
        from hermes_cli.trix_disk import disk_thresholds

        assert disk_thresholds({"disk": {"min_free_gb": 3}}).min_free_bytes == 3 * _GB
