"""Установка системных пакетов обязана ЖДАТЬ замок apt, а не падать об него.

На свежей облачной машине наш установщик стартует одновременно со
службами самой системы — `apt-daily`, `unattended-upgrades`, — и те
держат `/var/lib/dpkg/lock-frontend`. Без указания ждать apt отказывает
СРАЗУ («Could not get lock»), а установка едет дальше без ripgrep и
ffmpeg. Отсюда «иногда ставится не всё»: гонка со службами системы,
воспроизводимая ровно настолько, насколько повезёт со временем загрузки.

Здесь исполняется НАСТОЯЩАЯ `install_system_packages()` из install.sh
(извлечённая тем же приёмом, что в соседних тестах) против подставного
`apt` на PATH, который записывает свои аргументы. Проверяется поведение:
какую команду установщик на самом деле выполняет.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.live_system_guard_bypass


def _run_install_system_packages(tmp_path: Path, *, distro: str) -> subprocess.CompletedProcess:
    """Run the real install_system_packages() with a stubbed package manager.

    PATH is reduced to one directory we control, so ``rg`` and ``ffmpeg``
    are genuinely absent whatever the host has installed, and ``apt`` is
    our recorder. ``id`` is overridden to report root, which selects the
    root branch deterministically — the sudo branches build the very same
    ``$install_cmd`` string, so picking one is not a loss of coverage, and
    it keeps the test off the host's sudo configuration.
    """
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    recorder = tmp_path / "apt-argv"

    apt = fakebin / "apt"
    apt.write_text(
        "#!/bin/sh\n" f'printf "%s\\n" "$*" >> {recorder}\n' "exit 0\n",
        encoding="utf-8",
    )
    apt.chmod(0o755)

    # sed is needed to extract the function; nothing else from the host is.
    sed = shutil.which("sed")
    assert sed, "sed is required to extract the shell function under test"
    (fakebin / "sed").symlink_to(sed)

    script = f"""
set -e
export PATH={fakebin!s}
DISTRO={distro!r}
OS=linux
IS_INTERACTIVE=false
NON_INTERACTIVE=false
HAS_RIPGREP=false
HAS_FFMPEG=false
log_info() {{ :; }}
log_warn() {{ :; }}
log_success() {{ :; }}
log_error() {{ :; }}
show_manual_install_hint() {{ :; }}
id() {{ echo 0; }}
eval "$(sed -n '/^install_system_packages()/,/^}}/p' {INSTALL_SH!s})"
install_system_packages || true
"""
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    result.recorded = recorder.read_text(encoding="utf-8") if recorder.exists() else ""  # type: ignore[attr-defined]
    return result


@pytest.mark.parametrize("distro", ["ubuntu", "debian"])
def test_apt_is_asked_to_wait_for_the_dpkg_lock(tmp_path, distro):
    result = _run_install_system_packages(tmp_path, distro=distro)
    recorded = result.recorded  # type: ignore[attr-defined]
    assert recorded, (
        "подставной apt не был вызван вовсе — тест не проверил ничего:\n"
        + result.stdout
        + result.stderr
    )
    assert "DPkg::Lock::Timeout" in recorded, (
        "apt вызван без указания ждать замок — на занятой машине это "
        f"немедленный отказ. Записано: {recorded!r}"
    )


@pytest.mark.parametrize("distro", ["ubuntu", "debian"])
def test_the_wait_is_longer_than_apt_own_default(tmp_path, distro):
    """`apt` уже ждёт 120 секунд сам; ради 120 секунд менять было бы нечего.

    Разблокировка после unattended-upgrades занимает минуты, поэтому
    значение обязано быть заметно больше собственного умолчания apt.
    """
    recorded = _run_install_system_packages(tmp_path, distro=distro).recorded  # type: ignore[attr-defined]
    import re

    match = re.search(r"DPkg::Lock::Timeout=(\d+)", recorded)
    assert match, recorded
    assert int(match.group(1)) >= 300, recorded
