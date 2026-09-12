"""Мастер задаёт домашний чат, иначе бот не может написать первым.

Домашний чат — единственный адрес, по которому бот пишет клиенту сам:
предупреждение о заканчивающемся месте и месячная сводка. Без него
такие сообщения молча уходят в лог (см. hermes_cli/trix_disk_watch.py::
send_to_home_channel).

Тесты E2E: реальные импорты, временный ``HERMES_HOME``, запись и чтение
настоящего ``.env``.
"""
import yaml

from hermes_constants import get_hermes_home

from hermes_cli.setup_wizard.apply import first_allowed_telegram_id

FORM = {
    "telegram_token": "123:abc",
    "allowed_users": "111,222",
    "proxy": "",
    "provider": {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": "sk-or-test",
        "base_url": "",
        "model": "z-ai/glm-5.2",
    },
}


# ---- выбор адреса из списка разрешённых -------------------------------

def test_first_id_is_taken_from_the_allowed_list():
    assert first_allowed_telegram_id("111, 222,333") == "111"


def test_blank_and_garbage_yield_nothing():
    assert first_allowed_telegram_id("") is None
    assert first_allowed_telegram_id("   ,  ") is None


def test_non_numeric_entries_are_skipped_not_returned():
    """@username домашним чатом быть не может: нужен числовой chat_id."""
    assert first_allowed_telegram_id("@vasya, 777") == "777"


def test_only_usernames_yield_nothing():
    assert first_allowed_telegram_id("@vasya, @petya") is None


def test_none_is_survived():
    """Поле формы может отсутствовать — падать на этом нечему."""
    assert first_allowed_telegram_id(None) is None


# ---- запись в .env ----------------------------------------------------

def test_apply_writes_the_home_channel_next_to_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))
    assert out["ok"], out
    assert "TELEGRAM_HOME_CHANNEL" in out["written"]

    env = _read_env()
    assert env["TELEGRAM_HOME_CHANNEL"] == "111"
    # Домашний чат — первый из списка, а не сам список.
    assert env["TELEGRAM_ALLOWED_USERS"] == "111,222"


def test_a_username_only_allowlist_writes_no_home_channel(tmp_path, monkeypatch):
    """Отправка требует числового id — писать @vasya в адрес бессмысленно."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["allowed_users"] = "@vasya, @petya"
    out = apply_settings(form)
    assert out["ok"], out
    assert "TELEGRAM_HOME_CHANNEL" not in out["written"]
    assert "TELEGRAM_HOME_CHANNEL" not in _read_env()


def test_no_allowlist_writes_no_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["allowed_users"] = ""
    out = apply_settings(form)
    assert out["ok"], out
    assert "TELEGRAM_HOME_CHANNEL" not in out["written"]
    assert "TELEGRAM_HOME_CHANNEL" not in _read_env()


def test_a_reapply_moves_the_home_channel_with_the_allowlist(tmp_path, monkeypatch):
    """Клиент вернулся в мастер и сменил список — адрес едет следом,
    а не остаётся указывать на прежнего человека."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    assert apply_settings(dict(FORM))["ok"]
    assert _read_env()["TELEGRAM_HOME_CHANNEL"] == "111"

    form = dict(FORM)
    form["allowed_users"] = "999"
    assert apply_settings(form)["ok"]
    assert _read_env()["TELEGRAM_HOME_CHANNEL"] == "999"


# ---- цепочка до отправителя -------------------------------------------

def test_what_the_wizard_wrote_is_what_the_gateway_reads(tmp_path, monkeypatch):
    """Ключ мастера — тот же, что читает отправитель уведомлений.

    Если имя ключа разойдётся, ``get_home_channel(TELEGRAM)`` вернёт
    None и оба уведомления уйдут в лог вместо клиента.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    assert apply_settings(dict(FORM))["ok"]

    # Шлюз читает секреты из окружения — переносим туда то, что мастер
    # записал в .env, ничего не подставляя от себя.
    for key, value in _read_env().items():
        monkeypatch.setenv(key, value)

    from gateway.config import Platform, load_gateway_config

    home = load_gateway_config().get_home_channel(Platform.TELEGRAM)
    assert home is not None
    assert home.chat_id == "111"


def test_the_wizard_does_not_touch_config_yaml_for_this(tmp_path, monkeypatch):
    """Адрес — секрет-адресат, живёт в .env рядом с токеном; в
    config.yaml ему делать нечего."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    assert apply_settings(dict(FORM))["ok"]
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "TELEGRAM_HOME_CHANNEL" not in yaml.safe_dump(raw)


def _read_env() -> dict:
    from hermes_cli.config import load_env

    return load_env()


# ---- адрес и тема переписываются ВМЕСТЕ --------------------------------


class TestTheHomeChatIsAlwaysTheClientsPrivateChat:
    """Домашний чат — всегда личка клиента, и только она.

    Продукт живёт в группе с темами, но непрошеные сообщения
    (предупреждение о месте, месячная сводка, «я снова на связи») адресованы
    ЧЕЛОВЕКУ, а не рабочей теме: в теме они были бы шумом для всех
    участников и потерялись бы среди работы.

    Финальное ревью, §3.1: мастер переписывал ``TELEGRAM_HOME_CHANNEL`` на
    каждом применении, а ``TELEGRAM_HOME_CHANNEL_THREAD_ID`` не трогал.
    Шлюз читает оба, и переменные окружения перекрывают yaml
    (``gateway/config.py::_apply_env_overrides``). Клиент, сделавший
    ``/sethome`` в теме рабочей группы, оставлял в ``.env`` тему 47;
    следующее сохранение настроек ставило личный адрес и оставляло чужую
    тему — сообщение уходило в никуда, и клиент не узнавал об этом никогда.

    ``/sethome`` у клиента остаётся: это его прямое действие, и оно
    срабатывает. Просто следующее сохранение настроек возвращает домашний
    чат в личку — оба значения переписываются вместе, а не одно из двух.
    """

    def test_the_address_and_the_thread_are_written_as_a_pair(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.setup_wizard.apply import apply_settings

        out = apply_settings(dict(FORM))
        assert out["ok"], out

        env = _read_env()
        assert env["TELEGRAM_HOME_CHANNEL"] == "111"
        assert env.get("TELEGRAM_HOME_CHANNEL_THREAD_ID", "") == ""

    def test_a_thread_left_by_sethome_does_not_outlive_the_address(
        self, tmp_path, monkeypatch
    ):
        """Тот самый сценарий: /sethome в теме группы, затем сохранение
        настроек в мастере."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.config import save_env_value
        from hermes_cli.setup_wizard.apply import apply_settings

        # Как это делает /sethome в теме рабочей группы.
        save_env_value("TELEGRAM_HOME_CHANNEL", "-1001234567890")
        save_env_value("TELEGRAM_HOME_CHANNEL_THREAD_ID", "47")

        assert apply_settings(dict(FORM))["ok"]

        env = _read_env()
        assert env["TELEGRAM_HOME_CHANNEL"] == "111"
        assert env.get("TELEGRAM_HOME_CHANNEL_THREAD_ID", "") == "", (
            "осиротевшая тема пережила смену адреса"
        )

    def test_the_gateway_ends_up_addressing_a_private_chat_with_no_thread(
        self, tmp_path, monkeypatch
    ):
        """Проверка до конца цепочки: что мастер записал — то шлюз и
        прочитал. Тема, оставшаяся от /sethome, до адресата не доезжает."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.config import save_env_value
        from hermes_cli.setup_wizard.apply import apply_settings

        save_env_value("TELEGRAM_HOME_CHANNEL", "-1001234567890")
        save_env_value("TELEGRAM_HOME_CHANNEL_THREAD_ID", "47")
        assert apply_settings(dict(FORM))["ok"]

        for key, value in _read_env().items():
            monkeypatch.setenv(key, value)

        from gateway.config import Platform, load_gateway_config

        home = load_gateway_config().get_home_channel(Platform.TELEGRAM)
        assert home is not None
        assert home.chat_id == "111"
        assert not home.thread_id, (
            f"шлюз всё ещё адресует тему {home.thread_id!r} в личном чате"
        )

    def test_the_thread_moves_on_every_reapply_not_just_the_first(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.config import save_env_value
        from hermes_cli.setup_wizard.apply import apply_settings

        assert apply_settings(dict(FORM))["ok"]
        # Клиент снова сходил в тему группы и снова вернулся в мастер.
        save_env_value("TELEGRAM_HOME_CHANNEL_THREAD_ID", "99")
        form = dict(FORM)
        form["allowed_users"] = "999"
        assert apply_settings(form)["ok"]

        env = _read_env()
        assert env["TELEGRAM_HOME_CHANNEL"] == "999"
        assert env.get("TELEGRAM_HOME_CHANNEL_THREAD_ID", "") == ""

    def test_without_a_numeric_id_neither_value_is_touched(
        self, tmp_path, monkeypatch
    ):
        """Пара переписывается целиком или не переписывается вовсе: если
        адреса нет, обнулять чужую тему не за что."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.config import save_env_value
        from hermes_cli.setup_wizard.apply import apply_settings

        save_env_value("TELEGRAM_HOME_CHANNEL", "-1001234567890")
        save_env_value("TELEGRAM_HOME_CHANNEL_THREAD_ID", "47")

        form = dict(FORM)
        form["allowed_users"] = "@vasya, @petya"
        assert apply_settings(form)["ok"]

        env = _read_env()
        assert env["TELEGRAM_HOME_CHANNEL"] == "-1001234567890"
        assert env["TELEGRAM_HOME_CHANNEL_THREAD_ID"] == "47"
