"""Вторая песочница на машине клиента не должна умирать из-за портов.

Найдено на живом стенде 2026-09-09. В клиентском шаблоне публикация
портов зашита одним диапазоном:

    docker_extra_args: ["-p", "18000-18009:18000-18009"]

Первый контейнер (чат) его занимает. Любой второй — задание по
расписанию, делегированный работник — получает от докера

    Bind for 0.0.0.0:18000 failed: port is already allocated

и `docker run` падает с кодом 125. Клиент при этом не видит ни ошибки,
ни порта: он видит, что напоминание, которое он попросил, просто не
пришло.

Размен здесь неочевиден и потому проверяется тестами: публикация портов
— удобство (показать поднятый сайт), терминал — сама работа. Ронять
сессию целиком ради удобства неправильно, но и снимать заодно ВСЕ
дополнительные аргументы нельзя: среди них ограничения исходящего
трафика песочницы.
"""

import pytest

from tools.environments.docker import (
    is_port_allocation_error,
    strip_publish_args,
)


class TestRecognisingTheRealError:
    """Откат должен срабатывать на занятый порт и ни на что другое."""

    def test_the_message_from_the_live_stand(self):
        assert is_port_allocation_error(
            "docker: Error response from daemon: failed to set up container "
            "networking: driver failed programming external connectivity on "
            "endpoint probe (dfd6d5): Bind for 0.0.0.0:18000 failed: port is "
            "already allocated"
        )

    def test_the_other_wording_docker_uses(self):
        assert is_port_allocation_error("listen tcp 0.0.0.0:18000: bind: address already in use")

    @pytest.mark.parametrize(
        "stderr",
        [
            "",
            "Unable to find image 'nikolaik/python-nodejs' locally",
            "docker: Error response from daemon: no space left on device",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        ],
        ids=["пусто", "нет образа", "нет места", "демон не отвечает"],
    )
    def test_other_failures_are_not_mistaken_for_it(self, stderr):
        """Иначе откат замаскирует настоящую поломку.

        Контейнер без места на диске поднимется «успешно» без портов, и
        разбираться будут уже с последствиями, а не с причиной.
        """
        assert not is_port_allocation_error(stderr)


class TestOnlyThePortsAreDropped:
    """Всё остальное обязано пережить откат — в том числе граница сети."""

    def test_separate_form(self):
        kept, dropped = strip_publish_args(["-p", "18000-18009:18000-18009"])
        assert kept == []
        assert dropped == ["-p", "18000-18009:18000-18009"]

    def test_joined_forms(self):
        kept, dropped = strip_publish_args(["--publish=8080:80", "-p3000:3000"])
        assert kept == []
        assert len(dropped) == 2

    def test_egress_controls_are_never_dropped(self):
        """Самое важное утверждение файла.

        Соблазн «повторить запуск без дополнительных аргументов» снял бы
        вместе с портами и сеть песочницы, и переменные окружения — то
        есть тихо открыл бы контейнеру наружу всё, ради удобства.
        """
        args = [
            "--network", "hermes-egress",
            "-e", "HTTPS_PROXY=http://proxy:3128",
            "-p", "18000-18009:18000-18009",
            "--cap-drop", "ALL",
        ]
        kept, dropped = strip_publish_args(args)
        assert kept == ["--network", "hermes-egress",
                        "-e", "HTTPS_PROXY=http://proxy:3128",
                        "--cap-drop", "ALL"]
        assert dropped == ["-p", "18000-18009:18000-18009"]

    def test_nothing_to_drop_is_reported_as_nothing(self):
        """Пустой список «убранного» — это сигнал не пытаться повторять.

        Если публикации портов не было вовсе, а докер всё равно ругается
        на порт, повтор той же командой ничего не изменит и только
        удвоит ожидание.
        """
        kept, dropped = strip_publish_args(["--network", "none"])
        assert dropped == []
        assert kept == ["--network", "none"]

    def test_a_value_that_looks_like_a_flag_is_still_a_value(self):
        """`-p` забирает следующий аргумент, каким бы он ни был."""
        kept, dropped = strip_publish_args(["-p", "-weird", "--network", "none"])
        assert kept == ["--network", "none"]
        assert dropped == ["-p", "-weird"]


class TestEverySandboxGetsAWorkingRange:
    """Решение владельца 2026-09-09: ссылка, которую агент даёт клиенту,
    должна работать всегда.

    Поэтому занятый диапазон — повод ПОИСКАТЬ свободный, а не сразу
    остаться без публикации. Снятие публикации остаётся последним
    средством: рабочий терминал без ссылок лучше, чем мёртвая сессия.

    Найдено живым прогоном: агент, оказавшийся в песочнице без портов,
    поднял сервер, перебрал 18001–18009 (изнутри все «слушают», снаружи
    молчат) и потратил 39 вызовов модели, так и не получив рабочей
    ссылки.
    """

    def test_range_spec_is_understood(self):
        from tools.environments.docker import parse_publish_spec

        assert parse_publish_spec("18000-18009:18000-18009") == (18000, 18009, 10)
        assert parse_publish_spec("8080:8080") == (8080, 8080, 1)

    @pytest.mark.parametrize(
        "spec",
        ["127.0.0.1:80:80", "8080:80", "18000-18009:19000-19009", "abc:abc", "18009-18000:18009-18000"],
        ids=["привязка к адресу", "разные стороны", "разные диапазоны", "не число", "конец раньше начала"],
    )
    def test_specs_we_refuse_to_shift(self, spec):
        """Лучше не тронуть, чем сдвинуть неверно.

        Сдвинув привязку к `127.0.0.1` или несовпадающие стороны, мы
        поменяли бы смысл настройки, которую клиенту никто не покажет.
        """
        from tools.environments.docker import parse_publish_spec

        assert parse_publish_spec(spec) is None

    def test_both_sides_shift_together(self):
        """Хост и контейнер сдвигаются вместе — отображение один-в-один.

        Если сдвинуть только хост, порт внутри и снаружи разъедутся, и
        агент, привязавшийся к 18000, должен откуда-то узнать, что
        снаружи это 18010. При одинаковом сдвиге ему хватает своего
        диапазона.
        """
        from tools.environments.docker import shift_publish_args

        out = shift_publish_args(["-p", "18000-18009:18000-18009"], 10)
        assert out == ["-p", "18010-18019:18010-18019"]

    def test_unshiftable_spec_is_left_alone(self):
        from tools.environments.docker import shift_publish_args

        args = ["--network", "none", "-p", "127.0.0.1:80:80"]
        assert shift_publish_args(args, 10) == args

    def test_single_port_keeps_single_form(self):
        from tools.environments.docker import shift_publish_args

        assert shift_publish_args(["-p", "8080:8080"], 5) == ["-p", "8085:8085"]

    def test_label_names_the_range_the_agent_can_use(self):
        """Именно это значение уезжает в контейнер как TRIX_PUBLISHED_PORTS."""
        from tools.environments.docker import published_range_label, shift_publish_args

        shifted = shift_publish_args(["-p", "18000-18009:18000-18009"], 20)
        assert published_range_label(shifted) == "18020-18029"

    def test_label_is_empty_when_nothing_is_published(self):
        from tools.environments.docker import published_range_label

        assert published_range_label(["--network", "none"]) == ""


class TestTheAgentIsToldTheMachineAddress:
    """Адрес машины читается локально, а не спрашивается у интернета.

    Найдено живым прогоном 2026-09-09: агент выдал клиенту ссылку на
    выходной адрес нашего прокси, а не на машину клиента — песочница
    ходит в интернет через прокси, и любой сервис «какой у меня IP»
    честно отвечает адресом прокси.

    Локально правда есть — публичный адрес висит прямо на интерфейсе.
    """

    def test_public_address_is_preferred(self):
        from tools.environments.docker import pick_public_address

        assert pick_public_address(["172.17.0.1", "81.222.148.64"]) == "81.222.148.64"

    @pytest.mark.parametrize(
        "addr",
        ["10.0.0.5", "172.16.3.1", "172.31.255.1", "192.168.1.10", "127.0.0.1",
         "169.254.1.1", "224.0.0.1"],
        ids=["10/8", "172.16", "172.31", "192.168", "loopback", "link-local", "multicast"],
    )
    def test_addresses_that_do_not_work_from_outside_are_refused(self, addr):
        """Назвать клиенту приватный адрес — это та же нерабочая ссылка,
        только выглядит убедительнее."""
        from tools.environments.docker import pick_public_address

        assert pick_public_address([addr]) == ""

    def test_nothing_public_means_nothing_promised(self):
        """Машина за NAT: честное пустое значение лучше выдуманного адреса.

        Пустая переменная — уже описанный случай: промпт велит сказать,
        что ссылку дать нельзя, и отдать результат файлом.
        """
        from tools.environments.docker import pick_public_address

        assert pick_public_address(["10.1.2.3", "192.168.0.1"]) == ""

    def test_garbage_does_not_crash_the_pick(self):
        from tools.environments.docker import pick_public_address

        assert pick_public_address(["", "not-an-ip", "1.2.3", "1.2.3.4.5"]) == ""
