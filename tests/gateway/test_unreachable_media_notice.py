"""An explicit MEDIA: tag that names an unreadable file must not vanish quietly.

MEDIA delivery runs in the gateway process AFTER the agent's turn has ended,
so there is nothing to hand the agent back: the only party still able to react
is the user. When an upload errors out they already get a notice (#66797), but
a path rejected one step earlier — during validation — was dropped in silence,
leaving a reply that announces an attachment and carries none.

The split matters and is pinned here: an *unreachable* path is the agent's own
mistake and is reported; a *denied* path is the prompt-injection surface
(``MEDIA:/etc/shadow``) and stays silent, because naming it back into chat is
exactly the write-into-the-reply primitive an injected tag is after.
"""

from __future__ import annotations

import pytest

from gateway.config import Platform
from gateway.platforms.base import (
    MEDIA_REJECT_DENIED,
    MEDIA_REJECT_UNREACHABLE,
    BasePlatformAdapter,
    classify_media_delivery_path,
)


class TestClassifyMediaDeliveryPath:

    def test_real_file_is_accepted_with_no_reason(self, tmp_path):
        target = tmp_path / "report.pdf"
        target.write_bytes(b"%PDF-1.4")
        resolved, reason = classify_media_delivery_path(str(target))
        assert resolved == str(target.resolve())
        assert reason is None

    def test_container_local_path_is_unreachable(self, monkeypatch):
        """/tmp/... inside the sandbox has no host counterpart."""
        monkeypatch.delenv("TERMINAL_ENV", raising=False)
        resolved, reason = classify_media_delivery_path("/workspace/nope.png")
        assert resolved is None
        assert reason == MEDIA_REJECT_UNREACHABLE

    def test_missing_file_is_unreachable(self, tmp_path):
        resolved, reason = classify_media_delivery_path(str(tmp_path / "gone.pdf"))
        assert resolved is None
        assert reason == MEDIA_REJECT_UNREACHABLE

    def test_directory_is_unreachable(self, tmp_path):
        resolved, reason = classify_media_delivery_path(str(tmp_path))
        assert resolved is None
        assert reason == MEDIA_REJECT_UNREACHABLE

    def test_relative_path_is_unreachable(self):
        resolved, reason = classify_media_delivery_path("report.pdf")
        assert resolved is None
        assert reason == MEDIA_REJECT_UNREACHABLE

    def test_denylisted_system_file_is_denied_not_unreachable(self):
        """/etc/hosts exists on every host — it is refused by policy, and that
        refusal must stay distinguishable from 'file not found'."""
        resolved, reason = classify_media_delivery_path("/etc/hosts")
        assert resolved is None
        assert reason == MEDIA_REJECT_DENIED

    def test_validate_is_the_reason_free_form(self, tmp_path):
        """The old entry point must keep answering exactly as before."""
        target = tmp_path / "a.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        for probe in (str(target), "/etc/hosts", str(tmp_path / "gone.png"), ""):
            assert BasePlatformAdapter.validate_media_delivery_path(
                probe
            ) == classify_media_delivery_path(probe)[0]


class TestPartitionMediaDeliveryPaths:

    def test_deliverable_and_unreachable_are_separated(self, tmp_path):
        good = tmp_path / "ok.pdf"
        good.write_bytes(b"%PDF-1.4")
        safe, unreachable = BasePlatformAdapter.partition_media_delivery_paths(
            [(str(good), False), (str(tmp_path / "gone.pdf"), False)]
        )
        assert safe == [(str(good.resolve()), False)]
        assert unreachable == [(str(tmp_path / "gone.pdf"), False)]

    def test_denied_paths_are_reported_to_nobody(self):
        safe, unreachable = BasePlatformAdapter.partition_media_delivery_paths(
            [("/etc/hosts", False)]
        )
        assert safe == []
        assert unreachable == []

    def test_voice_flag_survives_into_the_notice(self, tmp_path):
        _, unreachable = BasePlatformAdapter.partition_media_delivery_paths(
            [(str(tmp_path / "gone.ogg"), True)]
        )
        assert unreachable == [(str(tmp_path / "gone.ogg"), True)]

    def test_empty_input_is_two_empty_lists(self):
        assert BasePlatformAdapter.partition_media_delivery_paths([]) == ([], [])
        assert BasePlatformAdapter.partition_media_delivery_paths(None) == ([], [])

    def test_filter_still_returns_only_the_deliverable_half(self, tmp_path):
        good = tmp_path / "ok.pdf"
        good.write_bytes(b"%PDF-1.4")
        assert BasePlatformAdapter.filter_media_delivery_paths(
            [(str(good), False), ("/etc/hosts", False), (str(tmp_path / "x.pdf"), False)]
        ) == [(str(good.resolve()), False)]


class _RecordingAdapter(BasePlatformAdapter):
    """Captures what would have been sent, without touching a platform."""

    def __init__(self):
        self.sent = []
        self.platform = Platform.TELEGRAM

    name = "recording"

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        self.sent.append(content)
        return type("R", (), {"success": True, "error": None})()

    async def connect(self):  # pragma: no cover - never dialled in these tests
        return True

    async def disconnect(self):  # pragma: no cover
        return None

    async def get_chat_info(self, chat_id):  # pragma: no cover
        return {}


class TestNotifyUnreachableMedia:

    @pytest.mark.asyncio
    async def test_one_notice_per_unreachable_attachment(self):
        adapter = _RecordingAdapter()
        await adapter.notify_unreachable_media(
            "chat1", [("/workspace/a.pdf", False), ("/workspace/b.pdf", False)]
        )
        assert len(adapter.sent) == 2

    @pytest.mark.asyncio
    async def test_notice_never_echoes_the_unvalidated_path(self):
        """The path never validated, so it is raw model output. An injected
        ``MEDIA:`` tag must not be able to write its own text into the reply,
        and a spaced path must not leak fragments (tests/gateway/
        test_tool_response_drop_recovery.py pins the same invariant)."""
        adapter = _RecordingAdapter()
        await adapter.notify_unreachable_media(
            "chat1", [("/tmp/nope/send 1 BTC to bc1qxy.pdf", False)]
        )
        assert adapter.sent == ["\u26a0\ufe0f Couldn't deliver the file attachment."]

    @pytest.mark.asyncio
    async def test_upload_failures_still_name_the_validated_file(self, tmp_path):
        """The #66797 notice keeps its filename: that path DID validate."""
        adapter = _RecordingAdapter()
        await adapter._notify_media_delivery_failure("chat1", str(tmp_path / "report.pdf"))
        assert "report.pdf" in adapter.sent[0]

    @pytest.mark.asyncio
    async def test_nothing_unreachable_says_nothing(self):
        adapter = _RecordingAdapter()
        await adapter.notify_unreachable_media("chat1", [])
        await adapter.notify_unreachable_media("chat1", None)
        assert adapter.sent == []
