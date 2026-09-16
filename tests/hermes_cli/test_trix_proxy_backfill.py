"""Спека 17, Ruling 10 — досев прокси-переменных в .env.

Найдено живым прогоном: правки Rulings 1-6 пустили песочницу через
прокси по https, но http остался прямым, потому что на уже
установленной машине `.env` писал старый мастер и `HTTP_PROXY` в нём
нет. Эти тесты охраняют решение и, главное, его границы: досев обязан
молчать там, где клиент высказался сам.
"""

import pytest

from hermes_cli.trix_proxy_backfill import (
    backfill_notice,
    plan_proxy_backfill,
)


class TestPlanProxyBackfill:
    def test_derives_missing_names_from_https_proxy(self):
        """Машина, настроенная старым мастером: только HTTPS_PROXY и NO_PROXY."""
        plan = plan_proxy_backfill({
            "HTTPS_PROXY": "http://u:p@1.2.3.4:8080",
            "NO_PROXY": "localhost,127.0.0.1",
        })
        assert plan == {
            "HTTP_PROXY": "http://u:p@1.2.3.4:8080",
            "http_proxy": "http://u:p@1.2.3.4:8080",
            "https_proxy": "http://u:p@1.2.3.4:8080",
            "no_proxy": "localhost,127.0.0.1",
        }

    def test_idempotent_when_everything_present(self):
        """Второй прогон не находит работы — это и есть идемпотентность."""
        env = {
            "HTTPS_PROXY": "http://p:1", "http_proxy": "http://p:1",
            "HTTP_PROXY": "http://p:1", "https_proxy": "http://p:1",
            "NO_PROXY": "localhost", "no_proxy": "localhost",
        }
        assert plan_proxy_backfill(env) == {}

    def test_never_overwrites_a_value_the_client_set(self):
        """Клиент направил http на другой адрес — досев обязан молчать."""
        plan = plan_proxy_backfill({
            "HTTPS_PROXY": "http://secure:8080",
            "HTTP_PROXY": "http://plain:3128",
        })
        assert "HTTP_PROXY" not in plan
        assert plan["http_proxy"] == "http://secure:8080"

    def test_empty_value_counts_as_a_deliberate_choice(self):
        """`HTTP_PROXY=` — это «выключено», а не «не задано»."""
        plan = plan_proxy_backfill({
            "HTTPS_PROXY": "http://p:1",
            "HTTP_PROXY": "",
        })
        assert "HTTP_PROXY" not in plan

    def test_no_proxy_configured_means_nothing_to_do(self):
        """Машина без прокси не должна обрасти пустыми переменными."""
        assert plan_proxy_backfill({"TELEGRAM_BOT_TOKEN": "x"}) == {}

    def test_all_proxy_is_never_guessed(self):
        """SOCKS-адрес не выводится из http-прокси: это разные протоколы."""
        plan = plan_proxy_backfill({"HTTPS_PROXY": "http://p:1"})
        assert "ALL_PROXY" not in plan
        assert "all_proxy" not in plan

    def test_no_proxy_alone_still_derives_its_twin(self):
        """NO_PROXY без HTTPS_PROXY — законная настройка, двойник нужен."""
        assert plan_proxy_backfill({"NO_PROXY": "localhost"}) == {
            "no_proxy": "localhost",
        }


class TestBackfillNotice:
    def test_silent_when_nothing_written(self):
        assert backfill_notice([]) is None

    def test_names_what_it_added_and_why_it_mattered(self):
        text = backfill_notice(["HTTP_PROXY", "http_proxy"])
        assert "HTTP_PROXY" in text and "http_proxy" in text
        assert "песочниц" in text


class TestBackfillWritesEnv:
    def test_writes_only_missing_names_and_is_idempotent(self, tmp_path, monkeypatch):
        """Сквозная проверка через настоящий писатель .env, не мок."""
        import os

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / ".env").write_text(
            "HTTPS_PROXY=http://u:p@1.2.3.4:8080\nNO_PROXY=localhost\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        from hermes_cli import config as cfg
        from hermes_cli.trix_proxy_backfill import backfill_proxy_env

        cfg._env_cache = None
        written = backfill_proxy_env()
        assert set(written) == {"HTTP_PROXY", "http_proxy", "https_proxy", "no_proxy"}

        text = (home / ".env").read_text()
        assert "HTTP_PROXY=http://u:p@1.2.3.4:8080" in text
        assert "no_proxy=localhost" in text
        # исходные строки не тронуты
        assert "HTTPS_PROXY=http://u:p@1.2.3.4:8080" in text

        assert backfill_proxy_env() == []


class TestUpdateWiring:
    """Досев обязан жить в `hermes update`: клиенту его запустить нечем."""

    def test_prints_what_it_added(self, capsys, monkeypatch):
        from hermes_cli.update_cmd import _backfill_proxy_env

        monkeypatch.setattr(
            "hermes_cli.trix_proxy_backfill.backfill_proxy_env",
            lambda: ["HTTP_PROXY", "http_proxy"],
        )
        _backfill_proxy_env(quiet=False)
        out = capsys.readouterr().out
        assert "HTTP_PROXY" in out

    def test_silent_on_a_machine_without_proxy(self, capsys, monkeypatch):
        from hermes_cli.update_cmd import _backfill_proxy_env

        monkeypatch.setattr(
            "hermes_cli.trix_proxy_backfill.backfill_proxy_env", lambda: [],
        )
        _backfill_proxy_env(quiet=False)
        assert capsys.readouterr().out == ""

    def test_never_fails_the_update(self, capsys, monkeypatch):
        """Досев — улучшение, а не условие: его падение не роняет обновление."""
        def boom():
            raise RuntimeError("диск только на чтение")

        from hermes_cli.update_cmd import _backfill_proxy_env

        monkeypatch.setattr(
            "hermes_cli.trix_proxy_backfill.backfill_proxy_env", boom,
        )
        _backfill_proxy_env(quiet=False)  # не должно бросить
        assert capsys.readouterr().out == ""
