"""The Docker media-delivery startup warning must describe the real world.

The warning (``gateway/run.py``) predates the container→host media path
translation in ``gateway/platforms/base.py``
(``_translate_docker_container_media_path``): it was written when the only
host-visible route out of the sandbox was an explicit ``host:/output`` bind,
and it still asks only that question.  A persistent Docker sandbox binds
``/workspace`` and ``/root`` to real host directories, so an agent whose cwd is
``/workspace`` already produces deliverable files with ``docker_volumes: []``
— the configuration every Trix machine ships with.  Warning there is a false
alarm on every gateway start.

These tests pin the contract as an invariant rather than a snapshot: the
warning fires exactly when the configuration leaves **no** host-visible route
for the agent's output, and stays silent when one exists.
"""

from __future__ import annotations

import json
import logging
import types

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _docker_media_delivery_route


# ---------------------------------------------------------------------------
# The pure resolver
# ---------------------------------------------------------------------------


class TestDockerMediaDeliveryRoute:
    """``_docker_media_delivery_route`` takes config as data, not as a host."""

    def test_persistent_sandbox_is_a_route_without_any_volume(self):
        """The shipped Trix configuration: persistent container, no volumes.

        ``/workspace`` and ``/root`` are bind mounts of
        ``<sandbox>/docker/<task>/{workspace,home}``, so files land on the host.
        """
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=True,
            mount_cwd_to_workspace=False,
            volumes=[],
        )

    def test_ephemeral_sandbox_without_mounts_has_no_route(self):
        """tmpfs ``/workspace`` + tmpfs ``/root`` — genuinely invisible to the host."""
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=[],
        ) is None

    def test_explicit_output_mount_is_a_route(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/home/user/.hermes/cache/documents:/output"],
        )

    def test_outputs_plural_mount_is_a_route(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/srv/out:/outputs"],
        )

    def test_volume_covering_the_working_directory_is_a_route(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/srv/ws:/workspace"],
        )

    def test_volume_on_an_ancestor_of_the_working_directory_is_a_route(self):
        """A mount at ``/srv`` makes everything the agent writes under
        ``/srv/project`` host-visible."""
        assert _docker_media_delivery_route(
            cwd="/srv/project",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/data/srv:/srv"],
        )

    def test_unrelated_volume_is_not_a_route(self):
        """A read-only corpus mounted at ``/data`` does not help ``/workspace``."""
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/srv/corpus:/data:ro"],
        ) is None

    def test_volume_mode_suffix_does_not_defeat_matching(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/srv/ws:/workspace:rw"],
        )

    def test_sibling_prefix_is_not_an_ancestor(self):
        """``/workspace-old`` must not count as covering ``/workspace``."""
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["/srv/old:/workspace-old"],
        ) is None

    def test_host_cwd_bound_to_workspace_is_a_route(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=True,
            volumes=[],
        )

    def test_malformed_volume_entries_are_ignored_not_trusted(self):
        assert _docker_media_delivery_route(
            cwd="/workspace",
            container_persistent=False,
            mount_cwd_to_workspace=False,
            volumes=["no-colon-here", "", 17],
        ) is None

    def test_unknown_cwd_still_resolves_through_the_persistent_binds(self):
        """``TERMINAL_CWD`` unset (container default) must not force a warning."""
        assert _docker_media_delivery_route(
            cwd="",
            container_persistent=True,
            mount_cwd_to_workspace=False,
            volumes=[],
        )


# ---------------------------------------------------------------------------
# The startup check that consumes it
# ---------------------------------------------------------------------------


def _runner_with_platforms(*platforms):
    """Minimal stand-in for the parts of GatewayRunner the check touches."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(get_connected_platforms=lambda: list(platforms))
    )


def _warn(stub) -> None:
    GatewayRunner._warn_if_docker_media_delivery_is_risky(stub)


def _warning_lines(caplog) -> list:
    return [r.message for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def docker_gateway_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.delenv("TERMINAL_DOCKER_VOLUMES", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", raising=False)


class TestStartupWarning:

    def test_shipped_configuration_does_not_warn(self, docker_gateway_env, caplog):
        """The regression: docker + telegram + persistent + no volumes was
        warning on every single gateway start."""
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        assert _warning_lines(caplog) == []

    def test_ephemeral_container_still_warns(self, docker_gateway_env, monkeypatch, caplog):
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        assert len(_warning_lines(caplog)) == 1

    def test_ephemeral_container_with_output_mount_does_not_warn(
        self, docker_gateway_env, monkeypatch, caplog
    ):
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        monkeypatch.setenv(
            "TERMINAL_DOCKER_VOLUMES", json.dumps(["/home/user/out:/output"])
        )
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        assert _warning_lines(caplog) == []

    def test_non_docker_backend_never_warns(self, docker_gateway_env, monkeypatch, caplog):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        assert _warning_lines(caplog) == []

    def test_no_messaging_platform_never_warns(self, docker_gateway_env, monkeypatch, caplog):
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.LOCAL))
        assert _warning_lines(caplog) == []

    def test_unparseable_volumes_do_not_crash_the_check(
        self, docker_gateway_env, monkeypatch, caplog
    ):
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "{not json")
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        assert len(_warning_lines(caplog)) == 1

    def test_warning_names_the_actual_remedy(self, docker_gateway_env, monkeypatch, caplog):
        """The old text told users to bind ``:/output``. The real fix for the
        only case that still warns is to turn persistence back on."""
        monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
        with caplog.at_level(logging.DEBUG, logger="gateway.run"):
            _warn(_runner_with_platforms(Platform.TELEGRAM))
        message = _warning_lines(caplog)[0]
        assert "container_persistent" in message
        assert "docker_volumes" in message
