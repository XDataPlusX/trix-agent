"""Экспорт профиля не увозит приватный дом contained-раскладки.

До containment логины внешних CLI лежали в доме ОС и в архив профиля попасть
не могли в принципе. В contained они переехали под ``<root>/home/`` — и
``hermes profile export`` начал паковать их вместе с профилем.

Тесты проверяют СОСТАВ архива, а не содержимое файлов: единственная надёжная
граница — «ветви `home/` в архиве нет вообще». Опора на редактирование
регулярками не годится и доказывается здесь же: токен GitHub Copilot в
`hosts.json` проходит редактор насквозь.

Значения «токенов» синтетические и никогда не были живыми.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest


# Синтетические значения. Форма повторяет настоящую, содержимое — нет.
_FAKE_SECRETS: dict[str, str] = {
    "home/.claude/.credentials.json":
        '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-RAF148-NOT-REAL"}}',
    "home/.config/github-copilot/hosts.json":
        '{"github.com": {"oauth_token": "gho_RAF148NOTREAL0000000000000000000000"}}',
    "home/.modal.toml":
        '[default]\ntoken_id = "ak-RAF148NOTREAL"\ntoken_secret = "as-RAF148NOTREAL"\n',
    "home/.qwen/oauth_creds.json":
        '{"access_token": "RAF148-NOT-REAL", "refresh_token": "RAF148-NOT-REAL"}',
    "home/.codex/auth.json":
        '{"OPENAI_API_KEY": "sk-proj-RAF148NOTREAL"}',
    "home/.gemini/oauth_creds.json":
        '{"access_token": "ya29.RAF148-NOT-REAL"}',
}

# Всё, что обязано пережить экспорт: исключение дома не должно превратиться
# в исключение профиля.
_MUST_SURVIVE: dict[str, str] = {
    "config.yaml": "model: test\n",
    ".layout-version": "1\n",
    "SOUL.md": "# persona\n",
    "skills/demo/SKILL.md": "# skill\n",
}


@pytest.fixture
def profiles_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HERMES_HOME вне ``~/.hermes`` → корень профилей предсказуем."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _make_contained_profile(root: Path, name: str = "acme") -> Path:
    profile = root / "profiles" / name
    for rel, content in {**_FAKE_SECRETS, **_MUST_SURVIVE}.items():
        target = profile / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (profile / "tmp").mkdir(parents=True, exist_ok=True)
    (profile / "tmp" / "scratch.bin").write_bytes(b"\x00" * 16)
    (profile / "workspace").mkdir(parents=True, exist_ok=True)
    (profile / "workspace" / "notes.md").write_text("work\n", encoding="utf-8")
    return profile


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tf:
        return tf.getnames()


def _payloads(archive: Path) -> str:
    """Склейка содержимого всех регулярных файлов архива."""
    chunks: list[str] = []
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is None:
                continue
            chunks.append(handle.read().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def test_export_omits_contained_home_entirely(profiles_env: Path, tmp_path: Path) -> None:
    """Ни одного члена архива под ``home/``."""
    from hermes_cli.profiles import export_profile

    _make_contained_profile(profiles_env)
    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    leaked = [m for m in _members(archive) if m == "acme/home" or m.startswith("acme/home/")]
    assert leaked == [], f"приватный дом уехал в архив: {leaked}"


@pytest.mark.parametrize("rel", sorted(_FAKE_SECRETS))
def test_export_leaks_no_credential_host(profiles_env: Path, tmp_path: Path, rel: str) -> None:
    """Каждый внедрённый credential home проверяется отдельно.

    Регрессия пофайловая осознанно: общий тест на ``home/`` падает целиком,
    а этот показывает, КАКОЙ провайдер снова поехал.
    """
    from hermes_cli.profiles import export_profile

    _make_contained_profile(profiles_env)
    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    assert f"acme/{rel}" not in _members(archive)


def test_export_carries_no_live_token_shaped_payload(
    profiles_env: Path, tmp_path: Path
) -> None:
    """Содержимое архива не содержит ни одного из синтетических секретов.

    Именно этот тест хоронит опору на ``_scrub_export_secrets``: до фикса
    ``gho_…`` из ``hosts.json`` проходил редактор дословно.
    """
    from hermes_cli.profiles import export_profile

    _make_contained_profile(profiles_env)
    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    body = _payloads(archive)
    for marker in ("RAF148NOTREAL", "RAF148-NOT-REAL"):
        assert marker not in body, f"секретоподобная строка {marker!r} уехала в архив"


def test_export_keeps_profile_payload(profiles_env: Path, tmp_path: Path) -> None:
    """Исключение дома не должно вычистить сам профиль."""
    from hermes_cli.profiles import export_profile

    _make_contained_profile(profiles_env)
    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    members = set(_members(archive))
    for rel in _MUST_SURVIVE:
        assert f"acme/{rel}" in members, f"{rel} пропал из архива"


def test_export_skips_ephemeral_branches(profiles_env: Path, tmp_path: Path) -> None:
    """``tmp/`` по-прежнему исключён (не регрессировать соседнее решение)."""
    from hermes_cli.profiles import export_profile

    _make_contained_profile(profiles_env)
    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    assert not [m for m in _members(archive) if m.startswith("acme/tmp")]


def test_export_does_not_follow_home_symlink_outside_root(
    profiles_env: Path, tmp_path: Path
) -> None:
    """``home`` — симлинк наружу: ни ссылки, ни её содержимого в архиве.

    ``copytree(symlinks=True)`` копирует саму ссылку, а не цель, но член
    ``acme/home`` в архиве всё равно раскрывает путь к дому оператора.
    """
    from hermes_cli.profiles import export_profile

    outside = tmp_path / "outside"
    (outside / ".claude").mkdir(parents=True)
    (outside / ".claude" / ".credentials.json").write_text(
        '{"token": "RAF148NOTREAL"}', encoding="utf-8"
    )

    profile = profiles_env / "profiles" / "acme"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (profile / "home").symlink_to(outside, target_is_directory=True)

    archive = Path(export_profile("acme", str(tmp_path / "acme")))

    members = _members(archive)
    assert "acme/home" not in members
    assert not [m for m in members if m.startswith("acme/home/")]
    assert "RAF148NOTREAL" not in _payloads(archive)


def test_default_profile_export_omits_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Экспорт профиля ``default`` идёт другим кодом — проверяем и его."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))

    for rel, content in {**_FAKE_SECRETS, **_MUST_SURVIVE}.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    from hermes_cli.profiles import export_profile

    archive = Path(export_profile("default", str(tmp_path / "default")))

    assert not [m for m in _members(archive) if m.startswith("default/home")]
    assert "RAF148NOTREAL" not in _payloads(archive)
