"""Срок жизни медиа-кэшей задаётся конфигом, а не литералом.

Клиент присылает файл в чат, считая, что тот сохранён. До этой правки
шлюз удалял его через 24 часа — единственная дверь для файлов вела в
каталог со сроком годности.
"""

from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.run import _DEFAULT_MEDIA_RETENTION_HOURS, resolve_media_retention_hours
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_defaults_match_upstream_behaviour_when_unconfigured():
    assert resolve_media_retention_hours({}, "images") == 24
    assert resolve_media_retention_hours({}, "documents") == 24


def test_per_kind_override_wins():
    config = {"gateway": {"media_retention_hours": {"default": 24, "documents": 0}}}
    assert resolve_media_retention_hours(config, "documents") == 0
    assert resolve_media_retention_hours(config, "images") == 24


def test_zero_means_never_delete():
    config = {"gateway": {"media_retention_hours": {"documents": 0}}}
    assert resolve_media_retention_hours(config, "documents") == 0


def test_garbage_values_fall_back_to_the_default():
    config = {"gateway": {"media_retention_hours": {"documents": "скоро"}}}
    assert resolve_media_retention_hours(config, "documents") == 24


def test_client_template_keeps_documents_forever():
    """Шаблон, который едет клиенту, обязан выключить метлу для документов."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    template = yaml.safe_load(
        (root / "assets" / "config" / "trix-config.yaml").read_text(encoding="utf-8")
    )
    assert resolve_media_retention_hours(template, "documents") == 0
    assert resolve_media_retention_hours(template, "images") == 24


class TestMalformedGatewaySectionSurvives:
    """`gateway: telegram` (опечатка — скаляр вместо словаря) — реальная
    ошибка, которую легко сделать руками. Резолвер обязан пережить любую
    форму на КАЖДОМ уровне пути, а не только на уровне
    ``media_retention_hours``, который был проверен раньше."""

    def test_gateway_section_is_a_scalar(self):
        config = {"gateway": "telegram"}
        assert resolve_media_retention_hours(config, "documents") == 24

    def test_gateway_section_is_a_list(self):
        config = {"gateway": ["telegram", "slack"]}
        assert resolve_media_retention_hours(config, "documents") == 24

    def test_gateway_section_is_none(self):
        config = {"gateway": None}
        assert resolve_media_retention_hours(config, "documents") == 24

    def test_media_retention_hours_is_a_scalar(self):
        config = {"gateway": {"media_retention_hours": 24}}
        assert resolve_media_retention_hours(config, "documents") == 24

    def test_top_level_config_is_none(self):
        assert resolve_media_retention_hours(None, "documents") == 24


def test_fractional_hours_fall_back_instead_of_truncating_to_zero():
    """``documents: 0.5`` must not silently become 0 ("never delete") via
    int() truncation — a half-hour request flipped into "keep forever" is
    the wrong direction. Reject the fractional value and fall through to
    the built-in default instead of guessing."""
    config = {"gateway": {"media_retention_hours": {"documents": 0.5}}}
    assert resolve_media_retention_hours(config, "documents") == 24


def test_whole_number_float_is_still_accepted():
    config = {"gateway": {"media_retention_hours": {"documents": 12.0}}}
    assert resolve_media_retention_hours(config, "documents") == 12


def test_default_config_matches_builtin_fallback():
    """Two sources of truth for "24" must never drift apart.

    DEFAULT_CONFIG's gateway.media_retention_hours.default is what
    ``hermes config`` / the setup wizard show the client; the gateway
    reads config.yaml directly and, absent any file, falls back to the
    resolver's own built-in ``_DEFAULT_MEDIA_RETENTION_HOURS``. If these
    ever disagree, the client sees one number in the UI and lives under
    another one at runtime, with no test catching it.
    """
    assert (
        DEFAULT_CONFIG["gateway"]["media_retention_hours"]["default"]
        == _DEFAULT_MEDIA_RETENTION_HOURS
    )


class _NTickStopEvent:
    """Runs the housekeeping loop for exactly ``n`` ticks, then stops.

    ``is_set()`` is polled at the top of each loop iteration and
    ``wait()`` is called once at the bottom — so with n=60 the loop body
    runs for tick_count 1..60 (hitting the IMAGE_CACHE_EVERY=60 hourly
    branch exactly once) and stops before tick 61.
    """

    def __init__(self, n):
        self.n = n
        self.waits = 0

    def is_set(self):
        return self.waits >= self.n

    def wait(self, timeout=None):
        self.waits += 1
        return True


class TestHousekeepingWiresResolverToCleanups:
    """The resolver and the YAML template are each tested in isolation
    above; this class tests the wire between them — that
    ``_start_gateway_housekeeping`` actually calls each cleanup function
    with the hours ``resolve_media_retention_hours`` computed for it, and
    skips the call entirely when the resolved value is 0.

    Rewinding the fix under review (restoring the literal
    ``max_age_hours=24`` and dropping ``if hours == 0: continue``) makes
    every test in this class fail — verified by mutation, see the task-5
    report.
    """

    @pytest.fixture(autouse=True)
    def _patch_cleanups(self, monkeypatch):
        import gateway.platforms.base as base

        self.mocks = {
            "images": MagicMock(return_value=0),
            "documents": MagicMock(return_value=0),
            "audio": MagicMock(return_value=0),
            "videos": MagicMock(return_value=0),
            "screenshots": MagicMock(return_value=0),
        }
        monkeypatch.setattr(base, "cleanup_image_cache", self.mocks["images"])
        monkeypatch.setattr(base, "cleanup_document_cache", self.mocks["documents"])
        monkeypatch.setattr(base, "cleanup_audio_cache", self.mocks["audio"])
        monkeypatch.setattr(base, "cleanup_video_cache", self.mocks["videos"])
        monkeypatch.setattr(base, "cleanup_screenshot_cache", self.mocks["screenshots"])

        # Everything else that shares the hourly tick is best-effort and
        # already individually tested elsewhere — stub it so this test only
        # exercises the media-retention wiring it's named for.
        monkeypatch.setattr("hermes_cli.mem_trim.trim_memory", lambda **kw: False)
        monkeypatch.setattr(
            "agent.curator.maybe_run_curator", lambda **kw: None
        )
        monkeypatch.setattr(
            "tools.skills_sync_client.maybe_pull_skills", lambda: None
        )
        monkeypatch.setattr(
            "tools.skills_sync_client.maybe_pull_org_skills", lambda: None
        )
        monkeypatch.setattr(
            "hermes_cli.debug._sweep_expired_pastes", lambda: (0, 0)
        )

    def _run_one_hourly_tick(self, config, monkeypatch):
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
        gateway_run._start_gateway_housekeeping(_NTickStopEvent(60), interval=0)

    def test_empty_config_calls_all_five_with_the_builtin_default(self, monkeypatch):
        self._run_one_hourly_tick({}, monkeypatch)

        for kind, mock in self.mocks.items():
            mock.assert_called_once_with(max_age_hours=24)

    def test_client_template_skips_documents_but_keeps_the_rest_at_24(
        self, monkeypatch
    ):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[2]
        template = yaml.safe_load(
            (root / "assets" / "config" / "trix-config.yaml").read_text(
                encoding="utf-8"
            )
        )

        self._run_one_hourly_tick(template, monkeypatch)

        for kind in ("images", "audio", "videos", "screenshots"):
            self.mocks[kind].assert_called_once_with(max_age_hours=24)
        self.mocks["documents"].assert_not_called()

    def test_default_zero_calls_none_of_the_five(self, monkeypatch):
        config = {"gateway": {"media_retention_hours": {"default": 0}}}

        self._run_one_hourly_tick(config, monkeypatch)

        for mock in self.mocks.values():
            mock.assert_not_called()
