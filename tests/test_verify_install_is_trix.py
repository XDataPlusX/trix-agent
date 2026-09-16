"""Проверка отличает наш продукт от апстримного.

Поддельные установки собираются в tmp_path: настоящая установка для
этого не нужна, а проверяемое поведение -- ровно чтение origin и тега.
"""

import subprocess
from pathlib import Path

from hermes_cli.release_source import RELEASE_REPO_NAME, RELEASE_REPO_OWNER

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = REPO_ROOT / "scripts" / "verify_install_is_trix.sh"

OUR_HTTPS_ORIGIN = f"https://github.com/{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}.git"
OUR_SSH_ORIGIN = f"git@github.com:{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}.git"
# Регистр не должен иметь значения -- проверяем это отдельным тестом ниже.
OUR_SSH_ORIGIN_MIXED_CASE = (
    f"git@github.com:{RELEASE_REPO_OWNER.upper()}/{RELEASE_REPO_NAME.title()}.git"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fake_install(tmp_path: Path, origin: str, tag: str | None) -> Path:
    install = tmp_path / "install"
    install.mkdir()
    _git(tmp_path, "init", "-q", "-b", "release", str(install))
    _git(install, "config", "user.email", "t@example.com")
    _git(install, "config", "user.name", "t")
    _git(install, "remote", "add", "origin", origin)
    (install / "file.txt").write_text("x\n")
    _git(install, "add", "-A")
    _git(install, "commit", "-qm", "one")
    if tag:
        _git(install, "tag", "-a", tag, "-m", "r")
    return install


def _run(install: Path):
    return subprocess.run(
        ["bash", str(VERIFIER), str(install)],
        capture_output=True, text=True, timeout=30,
    )


def test_our_install_passes(tmp_path: Path):
    install = _fake_install(tmp_path, OUR_HTTPS_ORIGIN, "trix-v0.1.0")
    result = _run(install)
    assert result.returncode == 0, result.stderr


def test_upstream_install_is_rejected(tmp_path: Path):
    install = _fake_install(
        tmp_path, "https://github.com/NousResearch/hermes-agent.git", None
    )
    result = _run(install)
    assert result.returncode == 1
    assert "NousResearch" in result.stderr or "не Trix" in result.stderr


def test_our_repo_without_a_release_tag_warns_but_passes(tmp_path: Path):
    """Origin наш, тега нет -- предупреждение, но не отказ.

    Клиентский клон -- `--depth 1 --branch release` -- видит тег только
    пока тот указывает на вершину ветки. Любой коммит после тегирования
    оставляет честную установку без тега, а деплой здесь необратим
    (systemd-юнит уже отцеплен от синхронной части рецепта) -- отказывать
    из-за одной лишь метки версии нельзя.
    """
    install = _fake_install(tmp_path, OUR_SSH_ORIGIN, None)
    result = _run(install)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert "trix-v" in result.stderr


def test_ssh_and_https_forms_are_both_accepted(tmp_path: Path):
    install = _fake_install(tmp_path, OUR_SSH_ORIGIN_MIXED_CASE, "trix-v9.9.9")
    result = _run(install)
    assert result.returncode == 0, result.stderr
