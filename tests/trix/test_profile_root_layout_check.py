"""Фаза 5.4 плана 2026-09-14: проверка раскладки в hermes doctor.

Главное здесь — тип файловой системы под корнем. mountinfo подменяется
файлом в tmp_path, чтобы проверка исполнялась, а не мокалась.
"""

import pytest

import hermes_constants
from hermes_cli.trix_layout_check import (
    check_layout_containment,
    filesystem_for,
    is_network_filesystem,
)


def _mountinfo(tmp_path, rows: list[tuple[str, str]]):
    """Собрать mountinfo с переменным числом необязательных полей.

    Формат настоящий: поля до `-` переменной длины, поэтому парсер обязан
    искать разделитель, а не считать индексы.
    """
    lines = []
    for i, (point, fstype) in enumerate(rows):
        lines.append(
            f"{i + 20} 30 0:{i + 40} / {point} rw,relatime shared:1 - {fstype} src rw"
        )
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_longest_mount_point_wins(tmp_path):
    info = _mountinfo(tmp_path, [("/", "ext4"), (str(tmp_path), "nfs4")])
    assert filesystem_for(tmp_path / "profile", info) == "nfs4"


def test_root_mount_is_used_when_nothing_deeper(tmp_path):
    info = _mountinfo(tmp_path, [("/", "btrfs")])
    assert filesystem_for(tmp_path / "profile", info) == "btrfs"


def test_missing_mountinfo_is_unknown(tmp_path):
    assert filesystem_for(tmp_path, tmp_path / "nope") is None


def test_mount_point_with_a_space_is_parsed(tmp_path):
    point = tmp_path / "with space"
    point.mkdir()
    info = _mountinfo(tmp_path, [("/", "ext4"), (str(point).replace(" ", "\\040"), "cifs")])
    assert filesystem_for(point, info) == "cifs"


@pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smb3", "9p", "vboxsf", "fuse.sshfs"])
def test_network_filesystems_are_recognized(fstype):
    assert is_network_filesystem(fstype) is True


@pytest.mark.parametrize("fstype", ["ext4", "btrfs", "xfs", "overlay", "tmpfs", "zfs",
                                    "fuse.gvfsd-fuse", "fuse.portal", None])
def test_local_filesystems_are_not_flagged(fstype):
    assert is_network_filesystem(fstype) is False


# ── сама проверка доктора ────────────────────────────────────────────

@pytest.fixture
def doctor_root(monkeypatch, tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    for var in ("HERMES_ISOLATION", "HERMES_TMP_DIR", "TERMINAL_CWD", "TERMINAL_HOME_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    hermes_constants.reset_isolation_warnings()
    return profile


def _run(monkeypatch, fstype: str, tmp_path) -> tuple[list, str]:
    """Прогнать проверку доктора с заданным типом ФС под корнем."""
    import hermes_cli.trix_layout_check as mod

    monkeypatch.setattr(mod, "filesystem_for", lambda *a, **k: fstype)
    issues: list = []
    assert mod.check_layout_containment(issues, should_fix=False) == 0
    return issues, ""


def test_contained_on_a_network_fs_is_a_failure(doctor_root, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    issues, _ = _run(monkeypatch, "nfs4", tmp_path)
    assert any("nfs4" in i for i in issues), issues


def test_shared_host_on_a_network_fs_is_only_a_warning(doctor_root, monkeypatch, tmp_path, capsys):
    """Так живут и сегодня — отказывать задним числом нельзя."""
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    issues, _ = _run(monkeypatch, "nfs4", tmp_path)
    assert issues == []


def test_local_fs_is_clean(doctor_root, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    issues, _ = _run(monkeypatch, "ext4", tmp_path)
    assert issues == []


def test_branch_outside_the_root_is_reported(doctor_root, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("HERMES_TMP_DIR", str(tmp_path / "outside"))
    issues, _ = _run(monkeypatch, "ext4", tmp_path)
    assert any("вне его корня" in i for i in issues), issues


def test_shared_host_does_not_complain_about_branches(doctor_root, monkeypatch, tmp_path, capsys):
    """В shared-host ветви и должны быть снаружи — это не неполадка."""
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    issues, _ = _run(monkeypatch, "ext4", tmp_path)
    assert issues == []


def test_output_names_the_mode_and_its_source(doctor_root, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    _run(monkeypatch, "ext4", tmp_path)
    out = capsys.readouterr().out
    assert "contained" in out
    assert "HERMES_ISOLATION" in out


def test_marker_is_named_as_the_source(doctor_root, monkeypatch, tmp_path, capsys):
    (doctor_root / ".layout-version").write_text("2", encoding="utf-8")
    _run(monkeypatch, "ext4", tmp_path)
    assert ".layout-version" in capsys.readouterr().out
