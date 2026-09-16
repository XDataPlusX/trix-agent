"""RAF-176: с какими списками доступа рождается свежий профиль.

Прод-состояние, которое нашёл владелец: в
``~/.hermes/profiles/system_admin/.env`` лежала голая строка
``TELEGRAM_ALLOWED_USERS=``, при том что главный ``~/.hermes/.env`` был
заполнен. Источник — ``create_profile``: он сеял ``.env`` копией шаблона
``assets/config/trix.env.example``, где эта строка стоит пустой и
РАСКОММЕНТИРОВАННОЙ (в отличие от upstream-овского ``.env.example``, где она
закомментирована), а значение родителя не наследовал ничем.

Под ``gateway.multiplex_profiles`` это не косметика:
``build_profile_secret_scope`` собирает скоуп профиля ТОЛЬКО из его
собственного ``.env``, а ``_platform_gate_env`` считает скоуп авторитетным —
так что профиль без своего списка доступа не пускает никого, независимо от
того, что написано в главном ``.env``.

Здесь проверяются обе половины правки генератора:
  * пустые заготовки шаблона не попадают в профиль присваиваниями;
  * списки доступа (и только они) наследуются от установки-родителя.
"""

import os
from pathlib import Path

import pytest

from agent.secret_scope import load_env_file
from hermes_cli.config_template import comment_out_empty_assignments


# ---------------------------------------------------------------------------
# comment_out_empty_assignments
# ---------------------------------------------------------------------------


class TestCommentOutEmptyAssignments:
    def test_bare_assignment_is_commented(self):
        out = comment_out_empty_assignments("TELEGRAM_ALLOWED_USERS=\n")
        assert out == "# TELEGRAM_ALLOWED_USERS=\n"
        assert "TELEGRAM_ALLOWED_USERS" not in load_env_file_text(out)

    def test_export_prefix_and_trailing_space(self):
        out = comment_out_empty_assignments("export OPENROUTER_API_KEY=  \n")
        assert out == "# export OPENROUTER_API_KEY=  \n"

    def test_non_empty_values_survive_byte_for_byte(self):
        text = 'NO_PROXY=localhost,127.0.0.1\nKEY="quoted # value"\n'
        assert comment_out_empty_assignments(text) == text

    def test_explicitly_empty_string_is_left_alone(self):
        """``KEY=""`` is somebody's written-down choice, not an unfilled blank."""
        text = 'TELEGRAM_ALLOWED_USERS=""\n'
        assert comment_out_empty_assignments(text) == text

    def test_comments_and_blank_lines_are_untouched(self):
        text = "# 2. Ваш Telegram user id.\n\n#TELEGRAM_PROXY=\n"
        assert comment_out_empty_assignments(text) == text

    def test_shipped_client_template_has_no_bare_allowlist_after_transform(self):
        """Якорь на настоящем шаблоне, а не на выдуманной строке."""
        repo_root = Path(__file__).resolve().parents[2]
        template = repo_root / "assets" / "config" / "trix.env.example"
        raw = template.read_text(encoding="utf-8")
        # Шаблон ДЕЙСТВИТЕЛЬНО несёт пустую раскомментированную строку —
        # если однажды перестанет, этот тест должен об этом сказать.
        assert "TELEGRAM_ALLOWED_USERS" in load_env_file_text(raw)
        assert "TELEGRAM_ALLOWED_USERS" not in load_env_file_text(
            comment_out_empty_assignments(raw)
        )


def load_env_file_text(text: str) -> dict:
    """``load_env_file`` поверх строки — через временный файл, чтобы разбор
    был ровно тот же, каким его видит секретный скоуп профиля."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = Path(fh.name)
    try:
        return load_env_file(path)
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# _inherit_authz_gate_keys
# ---------------------------------------------------------------------------


@pytest.fixture
def _hermes_root(tmp_path, monkeypatch):
    """Подменить корень установки: default-инсталл + место под профили."""
    from hermes_cli import profiles as profiles_mod

    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setattr(
        profiles_mod, "_get_default_hermes_home", lambda: default_home
    )
    return default_home


class TestInheritAuthzGateKeys:
    def test_allowlists_are_inherited_from_the_parent_install(
        self, tmp_path, _hermes_root
    ):
        from hermes_cli.profiles import _inherit_authz_gate_keys

        (_hermes_root / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=123:secret\n"
            "TELEGRAM_ALLOWED_USERS=351788701\n"
            "TELEGRAM_GROUP_ALLOWED_CHATS=-100500\n"
            "GATEWAY_ALLOWED_USERS=351788701\n"
            "OPENROUTER_API_KEY=sk-parent\n"
            "GATEWAY_ALLOW_ALL_USERS=true\n",
            encoding="utf-8",
        )
        env_path = tmp_path / "profile.env"
        env_path.write_text("# seeded from template\n", encoding="utf-8")

        copied = _inherit_authz_gate_keys(env_path)
        seeded = load_env_file(env_path)

        assert copied == [
            "GATEWAY_ALLOWED_USERS",
            "TELEGRAM_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_CHATS",
        ]
        assert seeded["TELEGRAM_ALLOWED_USERS"] == "351788701"
        assert seeded["TELEGRAM_GROUP_ALLOWED_CHATS"] == "-100500"
        assert seeded["GATEWAY_ALLOWED_USERS"] == "351788701"

        # Ни одного секрета и ни одного расширяющего флага.
        assert "TELEGRAM_BOT_TOKEN" not in seeded
        assert "OPENROUTER_API_KEY" not in seeded
        assert "GATEWAY_ALLOW_ALL_USERS" not in seeded

    def test_empty_parent_value_is_not_inherited(self, tmp_path, _hermes_root):
        from hermes_cli.profiles import _inherit_authz_gate_keys

        (_hermes_root / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=\n", encoding="utf-8"
        )
        env_path = tmp_path / "profile.env"
        env_path.write_text("# seeded\n", encoding="utf-8")

        assert _inherit_authz_gate_keys(env_path) == []
        assert env_path.read_text(encoding="utf-8") == "# seeded\n"

    def test_value_already_in_the_profile_wins(self, tmp_path, _hermes_root):
        from hermes_cli.profiles import _inherit_authz_gate_keys

        (_hermes_root / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=351788701\n", encoding="utf-8"
        )
        env_path = tmp_path / "profile.env"
        env_path.write_text("TELEGRAM_ALLOWED_USERS=42\n", encoding="utf-8")

        assert _inherit_authz_gate_keys(env_path) == []
        assert load_env_file(env_path)["TELEGRAM_ALLOWED_USERS"] == "42"

    def test_missing_parent_env_is_not_an_error(self, tmp_path, _hermes_root):
        from hermes_cli.profiles import _inherit_authz_gate_keys

        env_path = tmp_path / "profile.env"
        env_path.write_text("# seeded\n", encoding="utf-8")
        assert _inherit_authz_gate_keys(env_path) == []

    def test_quoted_parent_value_round_trips(self, tmp_path, _hermes_root):
        """``load_env_file`` отдаёт РАЗОБРАННОЕ значение — при обратной записи
        оно обязано снова закавычиться, иначе профиль получит мусор."""
        from hermes_cli.profiles import _inherit_authz_gate_keys

        (_hermes_root / ".env").write_text(
            'SIMPLEX_ALLOWED_USERS="Ivan Petrov, Anna"\n', encoding="utf-8"
        )
        env_path = tmp_path / "profile.env"
        env_path.write_text("# seeded\n", encoding="utf-8")

        _inherit_authz_gate_keys(env_path)
        assert load_env_file(env_path)["SIMPLEX_ALLOWED_USERS"] == "Ivan Petrov, Anna"


# ---------------------------------------------------------------------------
# create_profile end-to-end
# ---------------------------------------------------------------------------


class TestCreateProfileEnvSeed:
    def test_new_profile_is_born_with_a_working_allowlist(
        self, tmp_path, monkeypatch
    ):
        """Приёмка задачи: новый профиль создаётся с ненулевым
        ``TELEGRAM_ALLOWED_USERS`` и без пустых присваиваний из шаблона."""
        from hermes_cli import profiles as profiles_mod

        default_home = tmp_path / ".hermes"
        (default_home / "profiles").mkdir(parents=True)
        (default_home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_ALLOWED_USERS=351788701\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            profiles_mod, "_get_default_hermes_home", lambda: default_home
        )
        monkeypatch.setattr(
            profiles_mod, "_get_profiles_root", lambda: default_home / "profiles"
        )
        monkeypatch.setenv("HERMES_HOME", str(default_home))

        profiles_mod.create_profile("system_admin")

        env_path = default_home / "profiles" / "system_admin" / ".env"
        seeded = load_env_file(env_path)

        # Прод-состояние RAF-176 (ключ есть, значение пустое) больше не
        # воспроизводится ни в одну сторону: значение унаследовано.
        assert seeded.get("TELEGRAM_ALLOWED_USERS") == "351788701"
        # Токен бота НЕ скопирован — иначе два профиля подрались бы за одну
        # и ту же учётку на первом же `hermes gateway`.
        assert "TELEGRAM_BOT_TOKEN" not in seeded
        # Ни одного пустого присваивания из шаблона.
        assert not [k for k, v in seeded.items() if not str(v).strip()]
        assert oct(os.stat(env_path).st_mode)[-3:] == "600"
