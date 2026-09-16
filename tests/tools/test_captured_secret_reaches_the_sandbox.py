"""Ключ, который клиент прислал в чат, доходит до песочницы.

Снято с клиентской установки 2026-09-10. Клиент прислал вебхук Битрикса
через `secret_request` — продукт принял его и сохранил в `.env`. Дальше
агент позвал вебхук из `terminal`, то есть ИЗ КОНТЕЙНЕРА, и прочитал там
пустую строку: переменной внутри не было. Проверить ключ не вышло, и агент
спросил его во второй раз.

Проверено на живой машине: `docker exec <контейнер> echo $BITRIX24_WEBHOOK_URL`
возвращал пусто при том, что в `.env` значение лежало.

Причина не «пустая настройка», а отсутствующее звено. В песочницу
переменные попадают через набор проброса (`tools/env_passthrough.py`),
который на каждом `docker exec` подставляется рантайм-флагами `-e`. Набор
складывается из трёх источников: встроенные прокси-имена, объявленные
скиллом и `terminal.env_passthrough` из конфига. Захват секрета не
добавлял имя ни в один из них — две функции строились порознь.

**Ключи провайдеров моделей сюда не попадают, и это не должно измениться.**
Спека 19, Ruling 7: провайдерский ключ требует РЕСТАРТА и намеренно не
экспортируется в текущий процесс, а `_HERMES_PROVIDER_ENV_BLOCKLIST`
отдельно вычитает такие имена из проброса (GHSA-rhgp-j443-p4rf). Тесты
держат обе стороны: ключ инструмента доезжает, ключ провайдера — нет.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _clear_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Свой HERMES_HOME и чистый набор проброса на каждый тест."""
    from tools.env_passthrough import clear_env_passthrough

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_capture_state()
    clear_env_passthrough()
    # Кэш конфигового источника живёт на модуле — иначе соседний тест
    # подсунет свой config.yaml.
    import tools.env_passthrough as ep

    monkeypatch.setattr(ep, "_config_passthrough", None)
    yield home
    clear_env_passthrough()


def _capture(env_var: str, value: str, *, provider_probe=None):
    """Провести ключ по настоящему пути захвата и вернуть вердикт."""
    from tools import secret_capture_gateway as scg

    scg.register("req-1", "sess-1", env_var, "проверка")
    probe = provider_probe or {"ok": True, "reachable": True, "reason": None}
    with patch("hermes_cli.config.save_env_value_secure"), \
         patch("hermes_cli.config.load_env", return_value={env_var: value}), \
         patch("hermes_cli.credential_probes.probe_provider_key", return_value=probe):
        return scg.resolve_secret_reply("sess-1", value)


class TestToolKeyReachesTheSandbox:
    def test_captured_name_passes_through(self):
        """Ровно тот случай, на котором клиент застрял."""
        from tools.env_passthrough import is_env_passthrough

        outcome = _capture("BITRIX24_WEBHOOK_URL", "https://x/rest/1/abc/")

        assert outcome["provider"] is False
        assert is_env_passthrough("BITRIX24_WEBHOOK_URL"), (
            "имя не попало в набор проброса — в контейнере переменной не будет"
        )

    def test_the_name_survives_a_restart(self, isolated):
        """Набор проброса сессионный. Если имя нигде не осело, после
        перезапуска шлюза ключ снова станет невидимым — и клиент, ничего не
        меняя, увидит ту же поломку второй раз."""
        import json

        from tools.env_passthrough import CAPTURED_PASSTHROUGH_FILENAME

        _capture("BITRIX24_WEBHOOK_URL", "https://x/rest/1/abc/")

        names = json.loads(
            (isolated / CAPTURED_PASSTHROUGH_FILENAME).read_text(encoding="utf-8")
        )
        assert "BITRIX24_WEBHOOK_URL" in names

    def test_capturing_twice_does_not_duplicate(self, isolated):
        import json

        from tools.env_passthrough import CAPTURED_PASSTHROUGH_FILENAME

        # НЕ TAVILY_API_KEY: это ключ провайдера поиска, которым продукт
        # пользуется в главном процессе, и блоклист песочницы его не пускает
        # (первая редакция теста на этом и упала — поведение верное).
        _capture("BITRIX24_WEBHOOK_URL", "https://x/rest/1/a/")
        _clear_capture_state()
        _capture("BITRIX24_WEBHOOK_URL", "https://x/rest/1/b/")

        names = json.loads(
            (isolated / CAPTURED_PASSTHROUGH_FILENAME).read_text(encoding="utf-8")
        )
        assert names.count("BITRIX24_WEBHOOK_URL") == 1


class TestProviderKeyStillNeverReachesTheSandbox:
    def test_provider_key_is_not_registered(self):
        """Спека 19 Ruling 7 + GHSA-rhgp-j443-p4rf. Ключ провайдера едет
        через рестарт и в песочницу не попадает никогда."""
        from tools.env_passthrough import is_env_passthrough

        outcome = _capture("OPENAI_API_KEY", "sk-should-not-leak")

        assert outcome["provider"] is True
        assert not is_env_passthrough("OPENAI_API_KEY")

    def test_provider_key_is_not_persisted(self, isolated):
        import json

        from tools.env_passthrough import CAPTURED_PASSTHROUGH_FILENAME

        _capture("OPENAI_API_KEY", "sk-should-not-leak")

        path = isolated / CAPTURED_PASSTHROUGH_FILENAME
        names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        assert "OPENAI_API_KEY" not in names


class TestFailureDoesNotRegister:
    def test_a_failed_save_registers_nothing(self):
        """Не сохранили — значит и пробрасывать нечего."""
        from tools import secret_capture_gateway as scg
        from tools.env_passthrough import is_env_passthrough

        scg.register("req-f", "sess-f", "SOME_TOOL_KEY", "проверка")
        with patch(
            "hermes_cli.config.save_env_value_secure", side_effect=OSError("диск")
        ):
            outcome = scg.resolve_secret_reply("sess-f", "value")

        assert outcome["success"] is False
        assert not is_env_passthrough("SOME_TOOL_KEY")
