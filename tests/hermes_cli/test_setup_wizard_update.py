"""Кнопка «Обновить» в мастере и общий механизм запуска обновления.

Смысл кнопки — в том, что она работает КОГДА БОТ НЕ РАБОТАЕТ. Поэтому
проверяется не «страница отдаёт 200», а то, из-за чего клиент останется
без починки: исход обновления должен переживать смерть процесса, который
его начал, второе одновременное обновление должно отбиваться, а итог
должен доходить до клиента словами, а не кодом возврата.
"""

from unittest.mock import patch

import pytest

from hermes_cli import update_launch


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestOutcomeSurvivesTheProcessThatStartedIt:
    """Исход читается из файлов, а не из памяти.

    `hermes update` перезапускает и шлюз, и мастер: спрашивать об исходе
    придёт другой процесс, поднявшийся уже после перезапуска. Состояние
    в памяти он бы не увидел.
    """

    def test_nothing_started_reads_as_idle(self, home):
        assert update_launch.read_update_state().state == "idle"

    def test_started_but_unfinished_reads_as_running(self, home):
        update_launch.output_path().write_text("→ Pulling updates...\n")
        state = update_launch.read_update_state()
        assert state.state == "running"
        assert state.exit_code is None

    def test_zero_exit_code_reads_as_done(self, home):
        update_launch.output_path().write_text("ok\n")
        update_launch.exit_code_path().write_text("0")
        state = update_launch.read_update_state()
        assert state.state == "done"
        assert state.finished

    def test_nonzero_exit_code_reads_as_failed(self, home):
        update_launch.output_path().write_text("boom\n")
        update_launch.exit_code_path().write_text("1")
        state = update_launch.read_update_state()
        assert state.state == "failed"
        assert state.exit_code == 1

    def test_unreadable_exit_code_is_failure_not_success(self, home):
        """Файл кода возврата есть, а числа в нём нет.

        Так выглядит процесс, который успел создать файл и умер раньше
        записи. Считать это успехом — значит сказать клиенту «готово» про
        оборвавшееся обновление.
        """
        update_launch.output_path().write_text("...\n")
        update_launch.exit_code_path().write_text("")
        assert update_launch.read_update_state().state == "failed"

    def test_tail_is_bounded(self, home):
        update_launch.output_path().write_text(
            "\n".join(f"строка {i}" for i in range(500))
        )
        tail = update_launch.read_update_state().tail
        assert len(tail.splitlines()) == update_launch.TAIL_LINES
        assert "строка 499" in tail
        assert "строка 0\n" not in tail


class TestSecondUpdateIsRefused:
    """Два `hermes update` на одном рабочем дереве дерутся за git-индекс."""

    def test_start_refuses_while_one_is_running(self, home):
        update_launch.output_path().write_text("идёт\n")
        with pytest.raises(update_launch.UpdateAlreadyRunning):
            update_launch.start_detached_update()

    def test_start_is_allowed_again_after_it_finished(self, home):
        update_launch.output_path().write_text("прошлое\n")
        update_launch.exit_code_path().write_text("0")
        with (
            patch.object(update_launch, "resolve_hermes_command", return_value=["hermes"]),
            patch.object(update_launch.subprocess, "Popen") as popen,
        ):
            update_launch.start_detached_update()
        assert popen.called

    def test_previous_exit_code_is_cleared_before_the_new_run(self, home):
        """Иначе опрос сразу после нажатия увидит ПРОШЛЫЙ успех.

        Клиент нажимает «Обновить» и через секунду читает «Готово» от
        позапрошлого раза, хотя новое обновление ещё идёт.
        """
        update_launch.output_path().write_text("прошлое\n")
        update_launch.exit_code_path().write_text("0")
        with (
            patch.object(update_launch, "resolve_hermes_command", return_value=["hermes"]),
            patch.object(update_launch.subprocess, "Popen"),
        ):
            update_launch.start_detached_update()
        assert not update_launch.exit_code_path().exists()
        assert update_launch.read_update_state().state == "running"

    def test_missing_command_is_reported_not_swallowed(self, home):
        with patch.object(update_launch, "resolve_hermes_command", return_value=None):
            with pytest.raises(update_launch.HermesCommandNotFound):
                update_launch.start_detached_update()


class TestTheUpdateDoesNotInheritWhereItWasStartedFrom:
    """Рабочий каталог задаётся явно, а не наследуется от нажавшего.

    Наблюдалось на стенде 2026-09-09: процесс обновления унаследовал
    ``/root`` (0700, чужой пользователь), и обновление зависимостей
    свалилось на попытке прочитать оттуда `uv.toml` — распознавание речи
    осталось на прежней версии, а обновление отрапортовало успех.
    """

    def test_cwd_is_passed_explicitly(self, home):
        with (
            patch.object(update_launch, "resolve_hermes_command", return_value=["hermes"]),
            patch.object(update_launch.subprocess, "Popen") as popen,
        ):
            update_launch.start_detached_update()
        assert "cwd" in popen.call_args.kwargs, (
            "рабочий каталог не задан — процесс унаследует чужой"
        )

    def test_cwd_is_the_install_tree_when_it_is_known(self, home, tmp_path):
        tree = tmp_path / "install"
        tree.mkdir()
        with (
            patch.object(update_launch, "resolve_hermes_command", return_value=["hermes"]),
            patch("hermes_cli.config.get_project_root", return_value=tree),
            patch.object(update_launch.subprocess, "Popen") as popen,
        ):
            update_launch.start_detached_update()
        assert popen.call_args.kwargs["cwd"] == str(tree)


class TestSurvivingTheRestartItTriggers:
    """Обновление последним делом перезапускает службу, из которой его и
    запустили, — мастер настройки.

    Измерено на живой машине 2026-09-09: отвязанный через `setsid`
    потомок пользовательской службы УБИВАЕТСЯ вместе с ней, потому что
    systemd по умолчанию шлёт сигнал всей группе процессов. То есть
    обновление, дойдя до конца, убивало само себя и не успевало записать
    код возврата — страница показывала бы «Обновляю…» бесконечно.
    """

    def test_prefers_a_separate_transient_service(self):
        argv = update_launch.build_launcher_argv(
            "echo hi", systemd_run="/usr/bin/systemd-run", setsid="/usr/bin/setsid"
        )
        assert argv[0] == "/usr/bin/systemd-run"
        assert "--user" in argv
        # Отдельная служба живёт своей жизнью и не зависит от той, что её
        # породила; --collect убирает её за собой.
        assert any(a.startswith("--unit=") for a in argv)
        assert "--collect" in argv
        assert argv[-3:] == ["bash", "-c", "echo hi"]

    def test_falls_back_to_setsid_without_a_session_manager(self):
        argv = update_launch.build_launcher_argv(
            "echo hi", systemd_run=None, setsid="/usr/bin/setsid"
        )
        assert argv == ["/usr/bin/setsid", "bash", "-c", "echo hi"]

    def test_falls_back_to_plain_bash_when_neither_exists(self):
        argv = update_launch.build_launcher_argv("echo hi", systemd_run=None, setsid=None)
        assert argv == ["bash", "-c", "echo hi"]


class TestTheChatContractIsNotTouched:
    """Мастер не должен заставлять шлюз писать в чат.

    ``.update_pending.json`` принадлежит команде `/update` и означает
    «кому в мессенджере ответить». Записав его, мастер отправил бы
    клиенту в Telegram отчёт о том, чего тот в Telegram не просил.
    """

    def test_start_does_not_create_the_chat_marker(self, home):
        with (
            patch.object(update_launch, "resolve_hermes_command", return_value=["hermes"]),
            patch.object(update_launch.subprocess, "Popen"),
        ):
            update_launch.start_detached_update()
        assert not (home / ".update_pending.json").exists()


class TestTheWizardDoor:
    """Страница и два её маршрута."""

    def test_page_is_behind_the_same_auth_as_everything_else(self, app_env):
        client, _ = app_env
        assert client.get("/update").status_code == 401

    def test_page_names_the_version_and_offers_the_button(self, logged_in):
        r = logged_in.get("/update")
        assert r.status_code == 200
        assert "Trix Agent" in r.text
        assert "Обновить" in r.text
        # Подтверждение обязательно: обновление роняет бота и сам мастер.
        assert "Да, обновить" in r.text

    def test_start_launches_the_update(self, logged_in, tmp_path):
        with patch(
            "hermes_cli.setup_wizard.update_view.update_launch.start_detached_update",
            return_value=["hermes", "update"],
        ) as started:
            r = logged_in.post("/api/update/start", json={})
        assert r.status_code == 200
        assert r.json()["started"] is True
        assert started.called

    def test_second_start_is_refused_in_words(self, logged_in):
        with patch(
            "hermes_cli.setup_wizard.update_view.update_launch.start_detached_update",
            side_effect=update_launch.UpdateAlreadyRunning,
        ):
            r = logged_in.post("/api/update/start", json={})
        assert r.status_code == 409
        assert "уже идёт" in r.json()["error"]

    def test_status_reports_a_real_update_in_words(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        update_launch.output_path().write_text("ok\n")
        update_launch.exit_code_path().write_text("0")
        update_launch.from_version_path().write_text("trix-v0.1.22")
        with patch.object(update_launch, "installed_version", return_value="trix-v0.1.23"):
            body = logged_in.get("/api/update/status").json()
        assert body["state"] == "done"
        assert "обновлён" in body["message"]

    def test_pressing_the_button_on_the_latest_version_does_not_claim_an_update(
        self, logged_in, tmp_path, monkeypatch
    ):
        """Сказать «продукт обновлён» там, где ничего не менялось, — соврать.

        Клиент нажимает кнопку именно тогда, когда что-то не работает.
        Услышав «обновлён», он решит, что починку получил, и перестанет
        искать настоящую причину. Наблюдалось на живом стенде: кнопка на
        уже свежей машине отвечала «Готово — продукт обновлён».
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        update_launch.output_path().write_text("Already up to date\n")
        update_launch.exit_code_path().write_text("0")
        update_launch.from_version_path().write_text("trix-v0.1.23")
        with patch.object(update_launch, "installed_version", return_value="trix-v0.1.23"):
            body = logged_in.get("/api/update/status").json()
        assert body["state"] == "done"
        assert "обновлён" not in body["message"]
        assert "нечего" in body["message"]

    def test_unknown_version_change_says_neither(self, logged_in, tmp_path, monkeypatch):
        """Версию определить не вышло — молчим о том, чего не знаем."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        update_launch.output_path().write_text("ok\n")
        update_launch.exit_code_path().write_text("0")
        update_launch.from_version_path().write_text("trix-v0.1.22")
        with patch.object(update_launch, "installed_version", return_value=None):
            body = logged_in.get("/api/update/status").json()
        assert body["state"] == "done"
        assert "обновлён" not in body["message"]
        assert "нечего" not in body["message"]

    def test_status_failure_tells_the_client_nothing_is_broken(self, logged_in, tmp_path, monkeypatch):
        """Неудача обновления — не поломка машины.

        Обновление, которое не доехало, оставляет прежнюю рабочую
        версию. Клиенту надо сказать именно это, иначе он решит, что
        сломал машину кнопкой, и выключит её.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        update_launch.output_path().write_text("boom\n")
        update_launch.exit_code_path().write_text("1")
        body = logged_in.get("/api/update/status").json()
        assert body["state"] == "failed"
        assert "прежней" in body["message"]
        # Код возврата клиенту не показывается.
        assert "1" not in body["message"]


class TestTheEntryIsReachable:
    """Кнопки нет смысла, если на неё нет пути."""

    def test_rail_offers_the_update_entry_to_a_returning_client(self):
        from hermes_cli.setup_wizard.page import _rail_html

        rail = _rail_html("на 10.0.0.1", "", support_visible=True, update_visible=True)
        assert 'href="/update"' in rail

    def test_rail_stays_clean_during_the_very_first_setup(self):
        from hermes_cli.setup_wizard.page import _rail_html

        rail = _rail_html("на 10.0.0.1", "")
        assert 'href="/update"' not in rail

    def test_support_page_also_leads_to_the_update(self):
        """Клиент, пришедший чинить бота, должен видеть починку рядом."""
        from hermes_cli.setup_wizard.support_view import render_support_page

        assert 'href="/update"' in render_support_page("10.0.0.1")
