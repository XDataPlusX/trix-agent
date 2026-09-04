"""docker/stage2-hook.sh must seed config.yaml from the same curated
template install.sh uses, and must not abort the whole first-boot hook
under `set -eu` if that resolution fails.

Same class of bug as install.sh's copy_config_templates(): the hook seeds
config.yaml via `seed_one "config.yaml" "cli-config.yaml.example"`,
bypassing hermes_cli.config_template.resolve_config_template() entirely --
a container image never gets Trix's curated template (sandboxed
terminal.backend: docker, etc.), only upstream's 1700+ line example. And
because the hook runs under `set -eu`, resolving the template via a plain
`x=$(python ...)` assignment would kill the ENTIRE stage2 hook (chown,
config.yaml, SOUL.md, api key generation, config migration -- everything
after it) the instant that subprocess exits non-zero.

Extracts the real `seed_one()` function and the new config-template
selection block verbatim from docker/stage2-hook.sh, same technique as
tests/tools/test_stage2_hook_seed_one_symlinks.py -- not a
reimplementation, not a source-text assertion.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"


@pytest.fixture(scope="module")
def stage2_text() -> str:
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _path_guard_functions(text: str) -> str:
    start = text.index("path_has_symlink_component() {")
    end = text.index("\n\nchown_hermes_tree() {", start)
    return text[start:end]


def _seed_config_block(text: str) -> str:
    start = text.index("# --- Seed config files (only on first boot) ---")
    end = text.index("\n\n# --- Ensure a gateway api_server key exists", start)
    return text[start:end]


def _fake_bin_dir(tmp_path: Path) -> Path:
    """A `s6-setuidgid` shim that drops the "hermes" arg and runs the rest
    directly -- no real privilege drop needed to exercise this logic."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "s6-setuidgid"
    shim.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _distinguishing_marker_python(install_dir: Path, config_marker: Path, env_marker: Path) -> None:
    """A fake `.venv/bin/python` that tells the two resolver call sites
    apart by inspecting the heredoc script piped to it on stdin (which
    names either `resolve_config_template` or `resolve_env_template`),
    and touches the matching marker file. Always exits 1, so whichever
    branch ran also exercises the `set -eu` fallback path.

    A single fake binary is used for BOTH resolver call sites in
    docker/stage2-hook.sh (same `$INSTALL_DIR/.venv/bin/python` path), so
    telling them apart from the *content* piped to stdin -- not from
    which marker a naive stub would touch unconditionally -- is what
    makes this actually prove each resolver's gate is independent, not
    just that `seed_one()` leaves an existing file alone.
    """
    python_path = install_dir / ".venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text(
        "#!/bin/sh\n"
        'input="$(cat)"\n'
        f'if printf \'%s\' "$input" | grep -q resolve_env_template; then touch "{env_marker}"; '
        f'elif printf \'%s\' "$input" | grep -q resolve_config_template; then touch "{config_marker}"; '
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(script: str, extra_path: Path) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    env = dict(os.environ)
    env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    return subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, timeout=60, env=env
    )


def test_broken_venv_python_falls_back_without_aborting_the_hook(stage2_text, tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    home.mkdir()
    install_dir.mkdir()

    python_path = install_dir / ".venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    (install_dir / "cli-config.yaml.example").write_text(
        "model: {}\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )
    (install_dir / ".env.example").write_text("SECRET=1\n", encoding="utf-8")
    (install_dir / "docker").mkdir()
    (install_dir / "docker" / "SOUL.md").write_text("soul\n", encoding="utf-8")

    script = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f'INSTALL_DIR="{install_dir}"\n'
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_seed_config_block(stage2_text)}\n"
        "echo HOOK_COMPLETED\n"
    )
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run(script, bin_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "HOOK_COMPLETED" in result.stdout, (
        "the block after config seeding must still run -- hook aborted early"
    )
    config_path = home / "config.yaml"
    assert config_path.is_file()
    assert "UPSTREAM_EXAMPLE_MARKER" in config_path.read_text(encoding="utf-8")
    assert (home / ".env").is_file()
    assert (home / "SOUL.md").is_file()


def test_working_python_prefers_the_curated_trix_template(stage2_text, tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    home.mkdir()
    install_dir.mkdir()

    real_python = Path(os.path.realpath(REPO_ROOT / ".venv" / "bin" / "python"))
    assert real_python.is_file(), f"expected a real dev venv at {real_python}"
    venv_python = install_dir / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    os.symlink(real_python, venv_python)

    assets_dir = install_dir / "assets" / "config"
    assets_dir.mkdir(parents=True)
    trix_content = (REPO_ROOT / "assets" / "config" / "trix-config.yaml").read_text(encoding="utf-8")
    (assets_dir / "trix-config.yaml").write_text(trix_content, encoding="utf-8")

    (install_dir / "cli-config.yaml.example").write_text(
        "model: {}\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )
    (install_dir / ".env.example").write_text("SECRET=1\n", encoding="utf-8")
    (install_dir / "docker").mkdir()
    (install_dir / "docker" / "SOUL.md").write_text("soul\n", encoding="utf-8")

    script = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f'INSTALL_DIR="{install_dir}"\n'
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_seed_config_block(stage2_text)}\n"
    )
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run(script, bin_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    config_path = home / "config.yaml"
    assert config_path.is_file()
    written = config_path.read_text(encoding="utf-8")
    assert written == trix_content
    assert "UPSTREAM_EXAMPLE_MARKER" not in written


def test_resolvers_are_skipped_entirely_when_both_files_already_exist(stage2_text, tmp_path):
    """On every restart of an already-provisioned container, config.yaml
    AND .env already exist on the persistent volume -- neither resolver
    must spawn python / import hermes_cli just to compute a value nothing
    then uses."""
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    home.mkdir()
    install_dir.mkdir()
    (home / "config.yaml").write_text("EXISTING_CONFIG_UNTOUCHED\n", encoding="utf-8")
    (home / ".env").write_text("EXISTING_ENV_UNTOUCHED\n", encoding="utf-8")

    marker = tmp_path / "python_was_invoked"
    python_path = install_dir / ".venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8"
    )
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    (install_dir / "cli-config.yaml.example").write_text("model: {}\n", encoding="utf-8")
    (install_dir / ".env.example").write_text("SECRET=1\n", encoding="utf-8")
    (install_dir / "docker").mkdir()
    (install_dir / "docker" / "SOUL.md").write_text("soul\n", encoding="utf-8")

    script = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f'INSTALL_DIR="{install_dir}"\n'
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_seed_config_block(stage2_text)}\n"
    )
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run(script, bin_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert not marker.exists(), (
        "a python resolver ran even though both config.yaml and .env "
        "already existed -- every container restart pays for spawning "
        "python + importing hermes_cli for nothing"
    )
    assert (home / "config.yaml").read_text(encoding="utf-8") == "EXISTING_CONFIG_UNTOUCHED\n"
    assert (home / ".env").read_text(encoding="utf-8") == "EXISTING_ENV_UNTOUCHED\n"


def test_env_resolver_is_skipped_when_only_env_already_exists(stage2_text, tmp_path):
    """config.yaml is genuinely missing (so the config_src resolver must
    run) but .env already exists -- the env_src resolver must be skipped
    on its OWN, independent gate.

    Proven by a python stub that tells the two resolver call sites apart
    by the heredoc content piped to it on stdin and touches a
    distinguishing marker file per site -- not by "seed_one() left the
    existing .env alone", which is true regardless of whether the env_src
    resolver ran at all (seed_one()'s own `[ ! -f "$HERMES_HOME/$dest" ]`
    guard covers that unconditionally) and would not catch the env_src
    gate being dropped.
    """
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    home.mkdir()
    install_dir.mkdir()
    (home / ".env").write_text("EXISTING_ENV_UNTOUCHED\n", encoding="utf-8")

    config_marker = tmp_path / "config_resolver_invoked"
    env_marker = tmp_path / "env_resolver_invoked"
    _distinguishing_marker_python(install_dir, config_marker, env_marker)

    (install_dir / "cli-config.yaml.example").write_text(
        "model: {}\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )
    (install_dir / ".env.example").write_text("SECRET=1\n", encoding="utf-8")
    (install_dir / "docker").mkdir()
    (install_dir / "docker" / "SOUL.md").write_text("soul\n", encoding="utf-8")

    script = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f'INSTALL_DIR="{install_dir}"\n'
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_seed_config_block(stage2_text)}\n"
    )
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run(script, bin_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    # config.yaml was genuinely missing -- its resolver MUST have run.
    assert config_marker.exists(), (
        "config_src resolver never ran even though config.yaml was missing"
    )
    # .env already existed -- its resolver must NOT have run, independent
    # of config.yaml's state. This is the assertion the old version of
    # this test never made.
    assert not env_marker.exists(), (
        "env_src resolver ran even though .env already existed -- its "
        "gate is not actually independent from config_src's"
    )
    assert (home / ".env").read_text(encoding="utf-8") == "EXISTING_ENV_UNTOUCHED\n"


def test_working_python_prefers_the_curated_trix_env_template(stage2_text, tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    home.mkdir()
    install_dir.mkdir()

    real_python = Path(os.path.realpath(REPO_ROOT / ".venv" / "bin" / "python"))
    assert real_python.is_file(), f"expected a real dev venv at {real_python}"
    venv_python = install_dir / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    os.symlink(real_python, venv_python)

    assets_dir = install_dir / "assets" / "config"
    assets_dir.mkdir(parents=True)
    trix_env = (REPO_ROOT / "assets" / "config" / "trix.env.example").read_text(encoding="utf-8")
    (assets_dir / "trix.env.example").write_text(trix_env, encoding="utf-8")

    (install_dir / "cli-config.yaml.example").write_text("model: {}\n", encoding="utf-8")
    (install_dir / ".env.example").write_text(
        "SECRET=1\n# UPSTREAM_ENV_MARKER\n", encoding="utf-8"
    )
    (install_dir / "docker").mkdir()
    (install_dir / "docker" / "SOUL.md").write_text("soul\n", encoding="utf-8")

    script = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f'INSTALL_DIR="{install_dir}"\n'
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_seed_config_block(stage2_text)}\n"
    )
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run(script, bin_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    written = (home / ".env").read_text(encoding="utf-8")
    assert written == trix_env
    assert "UPSTREAM_ENV_MARKER" not in written
