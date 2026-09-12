"""Tests for the Docker sandbox preflight check.

``terminal.backend: docker`` is baked into the curated config template
(``assets/config/trix-config.yaml``). Docker itself is never installed by
``scripts/install.sh`` — that is root's job at VM-creation time (see
``docs/product/Root vs user — зависимости без sudo.md``). Without a
preflight check, a VM whose provisioning never ran only discovers the gap
the first time the client asks the agent to run a command.

Every case here drives the REAL ``check_docker_backend()`` against a real
subprocess — a throwaway executable named ``docker`` planted at the front
of ``PATH`` — never a mock of the function's internals. This matches the
technique used by the config/env template resolver tests and the
install.sh harness tests.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from hermes_cli.docker_preflight import check_docker_backend


def _plant_fake_docker(bin_dir: Path, script_body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker_path = bin_dir / "docker"
    docker_path.write_text(script_body, encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return docker_path


@pytest.fixture
def isolated_path(tmp_path, monkeypatch):
    """Point PATH at an empty directory so no real 'docker' on the host
    (or lack thereof) leaks into the test result."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


class TestBinaryNotFound:
    def test_missing_binary_reports_not_found_in_russian(self, isolated_path):
        # isolated_path is on PATH but has nothing in it.
        result = check_docker_backend()

        assert result.binary_found is False
        assert result.daemon_responds is False
        assert result.usable_without_sudo is False
        assert result.ok is False
        assert "docker" in result.message.lower()
        assert "не найден" in result.message.lower()
        # Cyrillic content -- proves the message is actually Russian, not
        # just an English string that happens to contain "docker".
        assert any("а" <= ch <= "я" or ch == "ё" for ch in result.message.lower())


class TestDaemonDown:
    def test_nonzero_exit_without_permission_text_reports_daemon_not_responding(self, isolated_path):
        _plant_fake_docker(
            isolated_path,
            "#!/bin/sh\n"
            'echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. '
            'Is the docker daemon running?" >&2\n'
            "exit 1\n",
        )

        result = check_docker_backend()

        assert result.binary_found is True
        assert result.daemon_responds is False
        assert result.usable_without_sudo is False
        assert result.ok is False
        assert "не отвечает" in result.message.lower()


class TestPermissionDenied:
    def test_permission_denied_text_reports_needs_group_membership(self, isolated_path):
        _plant_fake_docker(
            isolated_path,
            "#!/bin/sh\n"
            'echo "permission denied while trying to connect to the Docker daemon '
            'socket at unix:///var/run/docker.sock: Get '
            '\\"http://%2Fvar%2Frun%2Fdocker.sock/v1.24/info\\": dial unix '
            '/var/run/docker.sock: connect: permission denied" >&2\n'
            "exit 1\n",
        )

        result = check_docker_backend()

        assert result.binary_found is True
        assert result.usable_without_sudo is False
        assert result.ok is False
        # This is the one case whose remedy is different from "daemon is
        # down" -- the message must actually say so (group membership).
        assert "группу docker" in result.message or "docker group" in result.message.lower()


class TestSuccess:
    def test_healthy_daemon_reports_ok(self, isolated_path):
        _plant_fake_docker(isolated_path, "#!/bin/sh\necho ok\nexit 0\n")

        result = check_docker_backend()

        assert result.binary_found is True
        assert result.daemon_responds is True
        assert result.usable_without_sudo is True
        assert result.ok is True


class TestTimeout:
    def test_hung_daemon_is_bounded_by_timeout_not_left_hanging(self, isolated_path):
        # `isolated_path` sets PATH to *only* this directory (see fixture),
        # so an external `sleep` binary would fail to resolve inside the
        # script and exit immediately instead of hanging. A pure shell
        # builtin busy-loop hangs regardless of PATH.
        _plant_fake_docker(isolated_path, "#!/bin/sh\nwhile :; do :; done\n")

        started = time.monotonic()
        result = check_docker_backend(timeout=0.5)
        elapsed = time.monotonic() - started

        # The whole point of the short timeout: the installer must never
        # hang on an unresponsive daemon. Give generous slack over the 0.5s
        # bound for process-spawn overhead on a loaded CI box, but this
        # must be nowhere near what an unbounded busy-loop would take to
        # exhaust on its own (it never would -- it's killed by the timeout).
        assert elapsed < 5.0, f"check_docker_backend() took {elapsed:.2f}s -- not bounded by timeout"
        assert result.binary_found is True
        assert result.daemon_responds is False
        assert result.usable_without_sudo is False
        assert result.ok is False
        assert "не ответил" in result.message.lower()


class TestResultShape:
    def test_ok_is_true_only_when_all_three_flags_are_true(self, isolated_path):
        """Behavioral contract, not a snapshot: `ok` must track the AND of
        the three underlying flags for every case above, not just for the
        success case."""
        _plant_fake_docker(isolated_path, "#!/bin/sh\necho ok\nexit 0\n")
        healthy = check_docker_backend()
        assert healthy.ok == (
            healthy.binary_found and healthy.daemon_responds and healthy.usable_without_sudo
        )
        assert healthy.ok is True

        os.remove(isolated_path / "docker")
        missing = check_docker_backend()
        assert missing.ok == (
            missing.binary_found and missing.daemon_responds and missing.usable_without_sudo
        )
        assert missing.ok is False
