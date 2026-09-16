"""Tests for ``hermes debug`` CLI command and debug utilities."""

import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Set up an isolated HERMES_HOME with minimal logs."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Create log files
    logs_dir = home / "logs"
    logs_dir.mkdir()
    (logs_dir / "agent.log").write_text(
        "2026-04-12 17:00:00 INFO agent: session started\n"
        "2026-04-12 17:00:01 INFO tools.terminal: running ls\n"
        "2026-04-12 17:00:02 WARNING agent: high token usage\n"
    )
    (logs_dir / "errors.log").write_text(
        "2026-04-12 17:00:05 ERROR gateway.run: connection lost\n"
    )
    (logs_dir / "gateway.log").write_text(
        "2026-04-12 17:00:10 INFO gateway.run: started\n"
    )
    (logs_dir / "gui.log").write_text(
        "2026-04-12 17:00:12 INFO hermes_cli.web_server: dashboard request\n"
    )
    (logs_dir / "desktop.log").write_text(
        "2026-04-12 17:00:15 INFO desktop: backend spawned\n"
    )

    return home


# ---------------------------------------------------------------------------
# Unit tests for upload helpers
# ---------------------------------------------------------------------------

class TestUploadPasteRs:
    """Test paste.rs upload path."""

    def test_upload_paste_rs_success(self):
        from hermes_cli.debug import _upload_paste_rs

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"https://paste.rs/abc123\n"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("hermes_cli.debug.urllib.request.urlopen", return_value=mock_resp):
            url = _upload_paste_rs("hello world")

        assert url == "https://paste.rs/abc123"


    def test_upload_paste_rs_network_error(self):
        from hermes_cli.debug import _upload_paste_rs

        with patch(
            "hermes_cli.debug.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(urllib.error.URLError):
                _upload_paste_rs("test")




class TestUploadToPastebin:
    """upload_to_pastebin is permanently disabled: it must raise immediately,
    before touching the network, regardless of what the underlying
    paste-service helpers (_upload_paste_rs / _upload_dpaste_com) would do.

    Before the privacy fix, this class tested the paste.rs → dpaste.com
    fallback chain (one succeeding, or both failing). That fallback logic is
    now unreachable dead code below the top-of-function raise, so these
    tests instead prove the raise holds even when a helper is mocked to
    *succeed* -- the exact scenario a bad merge resolution could reintroduce.
    """

    def test_raises_with_no_mocks_installed(self):
        from hermes_cli.debug import upload_to_pastebin

        with pytest.raises(RuntimeError, match="never uploads"):
            upload_to_pastebin("content")

    def test_never_uploads_even_when_paste_rs_would_succeed(self):
        from hermes_cli.debug import upload_to_pastebin

        with patch("hermes_cli.debug._upload_paste_rs",
                    return_value="https://paste.rs/abc123") as pr, \
             patch("hermes_cli.debug._upload_dpaste_com",
                    return_value="https://dpaste.com/TEST") as dp:
            with pytest.raises(RuntimeError, match="never uploads"):
                upload_to_pastebin("content")

        pr.assert_not_called()
        dp.assert_not_called()


# ---------------------------------------------------------------------------
# Log reading
# ---------------------------------------------------------------------------

class TestCaptureLogSnapshot:
    """Test _capture_log_snapshot for log reading and truncation."""




    def test_race_truncate_after_resolve_reports_empty(self, hermes_home, monkeypatch):
        """If the log is truncated between resolve and stat, say 'empty', not 'missing'."""
        log_path = hermes_home / "logs" / "agent.log"
        from hermes_cli import debug

        monkeypatch.setattr(debug, "_resolve_log_path", lambda _name: log_path)
        log_path.write_text("")

        snap = debug._capture_log_snapshot("agent", tail_lines=10)
        assert snap.path == log_path
        assert snap.full_text is None
        assert snap.tail_text == "(file empty)"


    def test_keeps_first_line_when_truncation_on_boundary(self, hermes_home):
        """When truncation lands on a line boundary, keep the first full line."""
        from hermes_cli.debug import _capture_log_snapshot

        # File must exceed the initial chunk_size (8192) used by the
        # backward-reading loop so the truncation path actually fires.
        line = "A" * 99 + "\n"  # 100 bytes per line
        num_lines = 200  # 20000 bytes
        (hermes_home / "logs" / "agent.log").write_text(line * num_lines)

        # max_bytes = 1000 = 100 * 10 → cut at byte 20000 - 1000 = 19000,
        # and byte 19000 - 1 is '\n'.  Boundary hit → keep all 10 lines.
        snap = _capture_log_snapshot("agent", tail_lines=5, max_bytes=1000)
        assert snap.full_text is not None
        assert "truncated" in snap.full_text
        raw = snap.full_text.split("\n", 1)[1]
        kept = [l for l in raw.strip().splitlines() if l.startswith("A")]
        assert len(kept) == 10


class TestMissingLogNote:
    """A missing log explains itself when the writer isn't this backend.

    `hermes debug share` runs on the backend, so a desktop connected to a
    remote/docker/SSH backend can never contribute desktop.log. Reporting a
    bare absence sends triage after a client-side bug it cannot see.
    """

    def test_backend_written_log_reports_plain_absence(self, hermes_home):
        from hermes_cli.debug import _capture_log_snapshot

        (hermes_home / "logs" / "agent.log").unlink()

        snap = _capture_log_snapshot("agent", tail_lines=10)
        assert snap.full_text is None
        assert snap.tail_text == "(file not found)"

    def test_client_written_log_names_its_writer_and_path(self, hermes_home):
        from hermes_cli.debug import _capture_log_snapshot

        (hermes_home / "logs" / "desktop.log").unlink()

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert snap.full_text is None
        assert "not on this host" in snap.tail_text
        assert "Hermes Desktop" in snap.tail_text
        # The reader needs the path to collect by hand on the client machine.
        assert str(hermes_home / "logs" / "desktop.log") in snap.tail_text

    def test_present_client_log_is_captured_normally(self, hermes_home):
        """A local backend still reads desktop.log — the note is only for a miss."""
        from hermes_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert "backend spawned" in snap.tail_text
        assert "not on this host" not in snap.tail_text

    def test_empty_client_log_is_empty_not_absent(self, hermes_home):
        """An empty file means the app ran and logged nothing — a different fact."""
        from hermes_cli.debug import _capture_log_snapshot

        (hermes_home / "logs" / "desktop.log").write_text("")

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert snap.tail_text == "(file empty)"

    def test_report_carries_the_note_for_a_remote_backend(self, hermes_home):
        """The uploaded report — what people paste into support — must explain it."""
        from hermes_cli.debug import collect_debug_report

        (hermes_home / "logs" / "desktop.log").unlink()

        report = collect_debug_report(log_lines=10, dump_text="dump\n")
        assert "--- desktop.log" in report
        assert "not on this host" in report




# ---------------------------------------------------------------------------
# Capture log redaction (force=True applies regardless of HERMES_REDACT_SECRETS)
# ---------------------------------------------------------------------------

# A vendor-prefixed token used across redaction tests. Long enough to clear
# the redactor's `floor` parameter so it actually masks rather than fully blanks.
_REDACT_FIXTURE_TOKEN = "sk-proj-A1B2C3D4E5F6G7H8I9J0aA"


class TestCaptureLogSnapshotRedaction:
    """Pin upload-time redaction at the _capture_log_snapshot boundary."""

    @pytest.fixture
    def hermes_home_with_secret(self, tmp_path, monkeypatch):
        """Isolated HERMES_HOME whose agent.log contains a vendor-prefixed token."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Baseline fixture: no explicit env-var opinion. With the post-#17691
        # default of ON, the default-path tests below exercise the
        # secure-default behaviour. The `force=True` regression test
        # setenvs to "false" inline to prove force=True works even when
        # the runtime flag is disabled.
        monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)

        logs_dir = home / "logs"
        logs_dir.mkdir()
        (logs_dir / "agent.log").write_text(
            f"2026-04-12 17:00:00 INFO config: api_key={_REDACT_FIXTURE_TOKEN} loaded\n"
        )
        (logs_dir / "errors.log").write_text("")
        (logs_dir / "gateway.log").write_text("")
        return home

    def test_default_redacts_tail_and_full_text(self, hermes_home_with_secret):
        from hermes_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("agent", tail_lines=10)

        # Both views the upload uses must be sanitized.
        assert _REDACT_FIXTURE_TOKEN not in snap.tail_text
        assert snap.full_text is not None
        assert _REDACT_FIXTURE_TOKEN not in snap.full_text

    def test_redact_false_passes_through(self, hermes_home_with_secret):
        from hermes_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("agent", tail_lines=10, redact=False)

        # Original token survives when the caller opts out.
        assert _REDACT_FIXTURE_TOKEN in snap.tail_text
        assert _REDACT_FIXTURE_TOKEN in (snap.full_text or "")

    def test_force_true_works_when_redaction_disabled(
        self, hermes_home_with_secret, monkeypatch
    ):
        """Regression test: redact_sensitive_text short-circuits without force=True.

        If a future refactor drops `force=True` from `_redact_log_text`, this
        test fails immediately. Without `force=True`, the redactor returns the
        input unchanged when HERMES_REDACT_SECRETS=false, and the share-time
        redaction feature ships silently broken for users who opted out of
        runtime redaction (e.g. developers working on the redactor itself).
        """

        # Force the runtime flag off so we're exercising the force=True path,
        # not the default-on path.
        monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")

        from hermes_cli.debug import _capture_log_snapshot

        assert os.environ.get("HERMES_REDACT_SECRETS", "") == "false"

        snap = _capture_log_snapshot("agent", tail_lines=10)

        assert _REDACT_FIXTURE_TOKEN not in snap.tail_text
        assert snap.full_text is not None
        assert _REDACT_FIXTURE_TOKEN not in snap.full_text

    def test_default_redacts_email_addresses_for_public_share(
        self, hermes_home_with_secret
    ):
        from hermes_cli.debug import _capture_log_snapshot

        log_path = hermes_home_with_secret / "logs" / "agent.log"
        log_path.write_text(
            "2026-04-12 17:00:00 INFO gateway.run: "
            "inbound message: platform=bluebubbles "
            "user=person@example.com chat=iMessage;-;person@example.com msg='hello'\n"
        )

        snap = _capture_log_snapshot("agent", tail_lines=10)

        assert "person@example.com" not in snap.tail_text
        assert "[REDACTED_EMAIL]" in snap.tail_text
        assert snap.full_text is not None
        assert "person@example.com" not in snap.full_text

    def test_no_redact_preserves_email_addresses(self, hermes_home_with_secret):
        from hermes_cli.debug import _capture_log_snapshot

        log_path = hermes_home_with_secret / "logs" / "agent.log"
        log_path.write_text(
            "2026-04-12 17:00:00 INFO gateway.run: "
            "inbound message: platform=bluebubbles "
            "user=person@example.com chat=iMessage;-;person@example.com msg='hello'\n"
        )

        snap = _capture_log_snapshot("agent", tail_lines=10, redact=False)

        assert "person@example.com" in snap.tail_text
        assert "person@example.com" in (snap.full_text or "")

    def test_capture_default_log_snapshots_threads_redact(
        self, hermes_home_with_secret
    ):
        from hermes_cli.debug import _capture_default_log_snapshots

        snaps = _capture_default_log_snapshots(50)

        # Default threads redact=True to all three captured logs.
        assert _REDACT_FIXTURE_TOKEN not in snaps["agent"].tail_text
        assert _REDACT_FIXTURE_TOKEN not in (snaps["agent"].full_text or "")

    def test_capture_default_log_snapshots_no_redact_passes_through(
        self, hermes_home_with_secret
    ):
        from hermes_cli.debug import _capture_default_log_snapshots

        snaps = _capture_default_log_snapshots(50, redact=False)

        assert _REDACT_FIXTURE_TOKEN in snaps["agent"].tail_text
        assert _REDACT_FIXTURE_TOKEN in (snaps["agent"].full_text or "")


# ---------------------------------------------------------------------------
# Debug report collection
# ---------------------------------------------------------------------------

class TestCollectDebugReport:
    """Test the debug report builder."""

    def test_report_includes_dump_output(self, hermes_home):
        from hermes_cli.debug import collect_debug_report

        with patch("hermes_cli.dump.run_dump") as mock_dump:
            mock_dump.side_effect = lambda args: print(
                "--- hermes dump ---\nversion: 0.8.0\n--- end dump ---"
            )
            report = collect_debug_report(log_lines=50)

        assert "--- hermes dump ---" in report
        assert "version: 0.8.0" in report


# ---------------------------------------------------------------------------
# CLI entry point — run_debug_share
# ---------------------------------------------------------------------------

class TestRunDebugShare:
    """Test the run_debug_share CLI handler."""

    def test_share_sweeps_expired_pastes(self, hermes_home, capsys):
        """Slash-command path still sweeps legacy pending paste deletes (a
        pre-existing customer may have pastes uploaded by an older build
        pending cleanup), but never uploads a new one — this product never
        ships client logs off the VM."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)) as mock_sweep, \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(args)

        mock_sweep.assert_called_once()
        mock_upload.assert_not_called()
        assert "does not upload" in capsys.readouterr().out



    def test_share_uploads_five_pastes(self, hermes_home, capsys):
        """Default share never uploads; it prints report + agent.log +
        gateway.log + gui.log + desktop.log locally instead, each still
        self-contained with the dump header (mirroring the historical
        upload-bound paste content, minus the network trip)."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False

        with patch("hermes_cli.dump.run_dump") as mock_dump, \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            mock_dump.side_effect = lambda a: print("--- hermes dump ---\nversion: test\n--- end dump ---")
            run_debug_share(args)

        out = capsys.readouterr().out
        # Never uploads — everything below is printed to the terminal instead.
        mock_upload.assert_not_called()
        assert "paste.rs" not in out

        assert "agent.log" in out
        assert "gateway.log" in out
        assert "gui.log" in out
        assert "desktop.log" in out

        # Each full log section should still carry the dump header, so it
        # remains self-contained even though it's printed, not pasted.
        assert out.count("--- hermes dump ---") == 5  # report + 4 full logs
        assert "--- full agent.log ---" in out
        assert "--- full gateway.log ---" in out
        assert "--- full gui.log ---" in out
        assert "--- full desktop.log ---" in out




# ---------------------------------------------------------------------------
# Share-time redaction wiring + visible banner
# ---------------------------------------------------------------------------

class TestRunDebugShareRedaction:
    """End-to-end: --no-redact flag, banner injection, default behavior."""

    @pytest.fixture
    def hermes_home_with_secret(self, tmp_path, monkeypatch):
        """Isolated HERMES_HOME whose agent.log contains a vendor-prefixed token."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)

        logs_dir = home / "logs"
        logs_dir.mkdir()
        (logs_dir / "agent.log").write_text(
            f"2026-04-12 17:00:00 INFO config: api_key={_REDACT_FIXTURE_TOKEN} loaded\n"
        )
        (logs_dir / "errors.log").write_text("")
        (logs_dir / "gateway.log").write_text(
            f"2026-04-12 17:00:01 INFO gateway.run: token {_REDACT_FIXTURE_TOKEN}\n"
        )
        return home

    def test_default_share_redacts_uploaded_content(
        self, hermes_home_with_secret, capsys
    ):
        """Nothing is uploaded (never has network access to leak), and the
        report + full-log text printed locally still has the raw token
        scrubbed — redaction protects the printed output too, since a
        customer may paste it into a support channel themselves."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False
        args.no_redact = False

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(args)

        mock_upload.assert_not_called()
        out = capsys.readouterr().out
        assert _REDACT_FIXTURE_TOKEN not in out, (
            "raw token leaked into printed output"
        )

    def test_default_share_includes_redaction_banner(
        self, hermes_home_with_secret, capsys
    ):
        """The locally-printed report and full logs carry the visible
        redaction banner (report once, plus once per full log section)."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False
        args.no_redact = False

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(args)

        mock_upload.assert_not_called()
        out = capsys.readouterr().out
        # Report + agent.log + gateway.log full sections each carry the banner.
        assert out.count("redacted at collection time") >= 2, (
            "redaction banner missing from printed output"
        )

    def test_no_redact_flag_disables_redaction_and_banner(
        self, hermes_home_with_secret, capsys
    ):
        """--no-redact preserves original log content and omits the banner
        in the locally-printed output (still no upload happens)."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False
        args.no_redact = True

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(args)

        mock_upload.assert_not_called()
        out = capsys.readouterr().out
        # The raw token now appears in the printed output.
        assert _REDACT_FIXTURE_TOKEN in out, (
            "expected raw token in --no-redact printed output"
        )
        # No banner anywhere when redaction is disabled.
        assert "redacted at collection time" not in out, (
            "banner present with --no-redact"
        )


# ---------------------------------------------------------------------------
# run_debug router
# ---------------------------------------------------------------------------

class TestRunDebug:
    def test_no_subcommand_shows_usage(self, capsys):
        from hermes_cli.debug import run_debug

        args = MagicMock()
        args.debug_command = None

        run_debug(args)

        out = capsys.readouterr().out
        assert "hermes debug" in out
        assert "share" in out
        assert "delete" in out

    def test_share_subcommand_routes(self, hermes_home):
        from hermes_cli.debug import run_debug

        args = MagicMock()
        args.debug_command = "share"
        args.lines = 200
        args.expire = 7
        args.local = True
        args.nous = False

        with patch("hermes_cli.dump.run_dump"):
            run_debug(args)


# ---------------------------------------------------------------------------
# Argparse integration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Delete / auto-delete
# ---------------------------------------------------------------------------

class TestExtractPasteId:
    def test_paste_rs_url(self):
        from hermes_cli.debug import _extract_paste_id
        assert _extract_paste_id("https://paste.rs/abc123") == "abc123"


    def test_empty_returns_none(self):
        from hermes_cli.debug import _extract_paste_id
        assert _extract_paste_id("") is None


class TestDeletePaste:
    def test_delete_sends_delete_request(self):
        from hermes_cli.debug import delete_paste

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("hermes_cli.debug.urllib.request.urlopen",
                    return_value=mock_resp) as mock_open:
            result = delete_paste("https://paste.rs/abc123")

        assert result is True
        req = mock_open.call_args[0][0]
        assert req.method == "DELETE"
        assert "paste.rs/abc123" in req.full_url


class TestScheduleAutoDelete:
    """``_schedule_auto_delete`` used to spawn a detached Python subprocess
    per call (one per paste URL batch).  Those subprocesses slept 6 hours
    and accumulated forever under repeated use — 15+ orphaned interpreters
    were observed in production.

    The new implementation is stateless: it records pending deletions to
    ``~/.hermes/pastes/pending.json`` and lets ``_sweep_expired_pastes``
    handle the DELETE requests synchronously on the next ``hermes debug``
    invocation.
    """


    def test_records_pending_to_json(self, hermes_home):
        """Scheduled URLs are persisted to pending.json with expiration."""
        from hermes_cli.debug import _schedule_auto_delete, _pending_file
        import json

        _schedule_auto_delete(
            ["https://paste.rs/abc", "https://paste.rs/def"],
            delay_seconds=10,
        )

        pending_path = _pending_file()
        assert pending_path.exists()

        entries = json.loads(pending_path.read_text())
        assert len(entries) == 2
        urls = {e["url"] for e in entries}
        assert urls == {"https://paste.rs/abc", "https://paste.rs/def"}

        # expire_at is ~now + delay_seconds
        import time
        for e in entries:
            assert e["expire_at"] > time.time()
            assert e["expire_at"] <= time.time() + 15



    def test_dedupes_same_url(self, hermes_home):
        """Same URL recorded twice → one entry with the later expire_at."""
        from hermes_cli.debug import _schedule_auto_delete, _load_pending

        _schedule_auto_delete(["https://paste.rs/dup"], delay_seconds=10)
        _schedule_auto_delete(["https://paste.rs/dup"], delay_seconds=100)

        entries = _load_pending()
        assert len(entries) == 1
        assert entries[0]["url"] == "https://paste.rs/dup"


class TestSweepExpiredPastes:
    """Test the opportunistic sweep that replaces the sleeping subprocess."""


    def test_sweep_deletes_expired_entries(self, hermes_home):
        from hermes_cli.debug import (
            _sweep_expired_pastes,
            _save_pending,
            _load_pending,
        )
        import time

        # Seed pending.json with one expired + one future entry
        _save_pending([
            {"url": "https://paste.rs/expired", "expire_at": time.time() - 100},
            {"url": "https://paste.rs/future", "expire_at": time.time() + 3600},
        ])

        delete_calls = []

        def fake_delete(url):
            delete_calls.append(url)
            return True

        with patch("hermes_cli.debug.delete_paste", side_effect=fake_delete):
            deleted, remaining = _sweep_expired_pastes()

        assert delete_calls == ["https://paste.rs/expired"]
        assert deleted == 1
        assert remaining == 1

        entries = _load_pending()
        urls = {e["url"] for e in entries}
        assert urls == {"https://paste.rs/future"}

    def test_sweep_leaves_future_entries_alone(self, hermes_home):
        from hermes_cli.debug import _sweep_expired_pastes, _save_pending
        import time

        _save_pending([
            {"url": "https://paste.rs/future1", "expire_at": time.time() + 3600},
            {"url": "https://paste.rs/future2", "expire_at": time.time() + 7200},
        ])

        with patch("hermes_cli.debug.delete_paste") as mock_delete:
            deleted, remaining = _sweep_expired_pastes()

        mock_delete.assert_not_called()
        assert deleted == 0
        assert remaining == 2

    def test_sweep_survives_network_failure(self, hermes_home):
        """Failed DELETEs stay in pending.json until the 24h grace window."""
        from hermes_cli.debug import (
            _sweep_expired_pastes,
            _save_pending,
            _load_pending,
        )
        import time

        _save_pending([
            {"url": "https://paste.rs/flaky", "expire_at": time.time() - 100},
        ])

        with patch(
            "hermes_cli.debug.delete_paste",
            side_effect=Exception("network down"),
        ):
            deleted, remaining = _sweep_expired_pastes()

        # Failure within 24h grace → kept for retry
        assert deleted == 0
        assert remaining == 1
        assert len(_load_pending()) == 1


class TestRunDebugSweepsOnInvocation:
    """``run_debug`` must sweep expired pastes on every invocation."""

    def test_run_debug_calls_sweep(self, hermes_home):
        from hermes_cli.debug import run_debug

        args = MagicMock()
        args.debug_command = None  # default → prints help

        with patch("hermes_cli.debug._sweep_expired_pastes") as mock_sweep:
            run_debug(args)

        mock_sweep.assert_called_once()


class TestSaveReportLocally:
    """``_save_report_locally`` is the on-disk stand-in for what used to be
    a paste-service URL. It holds plaintext conversation content, so it
    must be written with restrictive permissions."""

    def test_writes_report_content(self, hermes_home):
        from hermes_cli.debug import _save_report_locally

        path = _save_report_locally("some report text")
        assert path.read_text(encoding="utf-8") == "some report text"
        assert path.parent.name == "debug-reports"

    def test_file_is_owner_only(self, hermes_home):
        import stat

        from hermes_cli.debug import _save_report_locally

        path = _save_report_locally("secret conversation content")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


class TestRunDebugDelete:

    def test_handles_delete_failure(self, capsys):
        from hermes_cli.debug import run_debug_delete

        args = MagicMock()
        args.urls = ["https://paste.rs/abc"]

        with patch("hermes_cli.debug.delete_paste",
                    side_effect=Exception("network error")):
            run_debug_delete(args)

        out = capsys.readouterr().out
        assert "Could not delete" in out


class TestShareIncludesAutoDelete:
    """Verify that run_debug_share schedules auto-deletion and prints TTL."""


    def test_share_shows_privacy_notice(self, hermes_home, capsys):
        """Default share no longer warns about a PUBLIC paste service —
        there's no upload to warn about. It states plainly that the report
        stays local instead, and never reaches the (mocked) uploader."""
        from hermes_cli.debug import run_debug_share

        args = MagicMock()
        args.lines = 50
        args.expire = 7
        args.local = False
        args.nous = False

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload, \
             patch("hermes_cli.debug._schedule_auto_delete"):
            run_debug_share(args)

        mock_upload.assert_not_called()
        out = capsys.readouterr().out
        assert "does not upload" in out
        assert "PUBLIC paste service" not in out


# ---------------------------------------------------------------------------
# build_debug_share — structured core, still exercised directly here
# ---------------------------------------------------------------------------


class TestBuildDebugShare:
    """The paste-upload core: still present and still correct in isolation,
    but no longer reachable from either the CLI (``run_debug_share`` always
    takes the local-only branch now) or the dashboard endpoint
    (``POST /api/ops/debug-share`` now calls ``collect_share_bundle``
    directly and returns its content instead of uploading). Kept and tested
    as a unit — see ``docs/product/specs/2026-08-13-trix-agent-persona-design.md``
    for why it's disabled rather than deleted.
    """



    def test_redaction_keeps_secrets_out_of_payload(self, hermes_home):
        from hermes_cli.debug import build_debug_share

        secret = "sk-proj-SUPERSECRETtoken1234567890"
        (hermes_home / "logs" / "agent.log").write_text(
            f"line one\nauthorization token={secret}\nline three\n"
        )

        uploaded = []

        def _upload(content, expiry_days=7):
            uploaded.append(content)
            return "https://paste.rs/x"

        with patch("hermes_cli.dump.run_dump"), patch(
            "hermes_cli.debug.upload_to_pastebin", side_effect=_upload
        ), patch("hermes_cli.debug._schedule_auto_delete"):
            result = build_debug_share(log_lines=50, redact=True)

        assert result.redacted is True
        joined = "\n".join(uploaded)
        assert secret not in joined, "secret leaked into upload payload"

    def test_optional_log_failure_is_collected_not_raised(self, hermes_home):
        from hermes_cli.debug import build_debug_share

        count = [0]

        def _upload(content, expiry_days=7):
            count[0] += 1
            # First call (the required Report) succeeds; a later one fails.
            if count[0] == 2:
                raise RuntimeError("paste service hiccup")
            return f"https://paste.rs/p{count[0]}"

        with patch("hermes_cli.dump.run_dump"), patch(
            "hermes_cli.debug.upload_to_pastebin", side_effect=_upload
        ), patch("hermes_cli.debug._schedule_auto_delete"):
            result = build_debug_share(log_lines=50, redact=True)

        assert "Report" in result.urls
        assert len(result.failures) == 1
        assert "paste service hiccup" in result.failures[0]



# ---------------------------------------------------------------------------
# Shared bundle collection + Nous-S3 path
# ---------------------------------------------------------------------------

class TestCollectShareBundle:

    def test_no_redact_omits_banner(self, hermes_home):
        from hermes_cli.debug import collect_share_bundle

        with patch("hermes_cli.dump.run_dump"):
            bundle = collect_share_bundle(log_lines=50, redact=False)

        assert "redacted at collection time" not in bundle["report"]

    def test_redaction_keeps_secrets_out(self, hermes_home):
        from hermes_cli.debug import collect_share_bundle

        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
        (hermes_home / "logs" / "agent.log").write_text(
            f"line one\nOPENAI_API_KEY={secret}\nline three\n"
        )
        with patch("hermes_cli.dump.run_dump"):
            redacted = collect_share_bundle(log_lines=50, redact=True)
            unredacted = collect_share_bundle(log_lines=50, redact=False)

        # Sanity: without redaction the secret is present in the bundle.
        assert secret in "\n".join(unredacted.values())
        # With redaction it must be scrubbed everywhere.
        assert secret not in "\n".join(redacted.values())




class TestBuildNousBundle:
    def test_envelope_shape_and_gzip(self, hermes_home):
        import gzip
        import json as _json

        from hermes_cli.debug import build_nous_bundle

        files = {"report": "hello", "agent.log": "log line"}
        blob = build_nous_bundle(files, redact=True)

        # It's gzip — magic bytes.
        assert blob[:2] == b"\x1f\x8b"
        envelope = _json.loads(gzip.decompress(blob).decode())
        assert envelope["format"] == "hermes-debug-share/1"
        assert envelope["redacted"] is True
        assert envelope["files"] == files
        assert "created" in envelope

    def test_redacted_false_recorded(self):
        import gzip
        import json as _json

        from hermes_cli.debug import build_nous_bundle

        blob = build_nous_bundle({"report": "x"}, redact=False)
        envelope = _json.loads(gzip.decompress(blob).decode())
        assert envelope["redacted"] is False


class TestRunDebugShareNous:
    def _args(self, **over):
        class _A:
            lines = 50
            expire = 7
            local = False
            nous = True
            no_redact = False
            yes = True

        a = _A()
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def test_nous_does_not_reach_nous(self, hermes_home, capsys):
        """`--nous` is now inert: it is silently downgraded to the local
        path, never calls ``share_to_nous``, and never prints a Nous view
        URL — even though a successful result is available to return."""
        from hermes_cli.debug import run_debug_share

        res = {
            "id": "id-1",
            "viewUrl": "https://support.example.com/diagnostics/id-1",
            "expiresAt": "2026-06-20T00:00:00Z",
        }
        with patch("hermes_cli.dump.run_dump"), patch(
            "hermes_cli.diagnostics_upload.share_to_nous", return_value=res
        ) as share:
            run_debug_share(self._args())

        share.assert_not_called()
        out = capsys.readouterr().out
        assert "Nous-INTERNAL" not in out
        assert "support.example.com" not in out
        assert "does not upload" in out

    def test_nous_failure_suggests_local(self, hermes_home, capsys):
        """Even when the (mocked) Nous backend is broken, `--nous` never
        gets near it: the local path returns the printed report instead of
        raising, because the upload attempt itself never happens."""
        from hermes_cli.debug import run_debug_share

        with patch("hermes_cli.dump.run_dump"), patch(
            "hermes_cli.diagnostics_upload.share_to_nous",
            side_effect=RuntimeError("service down"),
        ) as share:
            run_debug_share(self._args())  # must not raise / SystemExit

        share.assert_not_called()
        out = capsys.readouterr().out
        assert "Nous upload failed" not in out
        assert "does not upload" in out

    def test_nous_does_not_touch_pastebin(self, hermes_home, capsys):
        """`--nous` touches neither the Nous backend nor the public paste
        service — both upload paths are disabled by product decision."""
        from hermes_cli.debug import run_debug_share

        res = {"id": "id-1", "viewUrl": "https://v"}
        with patch("hermes_cli.dump.run_dump"), patch(
            "hermes_cli.diagnostics_upload.share_to_nous", return_value=res
        ) as share, patch("hermes_cli.debug.upload_to_pastebin") as paste:
            run_debug_share(self._args())
        paste.assert_not_called()
        share.assert_not_called()
        assert "does not upload" in capsys.readouterr().out


class TestDebugHelpTextHonesty:
    """Every user-visible description of `hermes debug` / `/debug` must
    describe what the command actually does now (collect + print locally,
    never upload) rather than the pre-privacy-fix upstream copy. This class
    exists to catch a regression brought in by a future upstream merge that
    reintroduces the old "upload"-promising strings.
    """

    # Phrases that would only appear if the old, now-false claims came back.
    # Checked case-insensitively. Negated phrasing like "no upload" /
    # "never uploaded" / "performs no upload" is fine and deliberately not
    # on this list.
    _BANNED_PHRASES = [
        "upload debug report",
        "upload a debug report",
        "upload logs and system info",
        "paste service and get a shareable url",
        "paste service and print",
        "upload to nous-internal storage",
        "upload immediately",
        "upload debug report for support",
        "before upload",
        "upload the debug bundle",
    ]

    def _assert_no_banned_phrase(self, text: str, where: str):
        lowered = text.lower()
        for phrase in self._BANNED_PHRASES:
            assert phrase not in lowered, (
                f"banned upload-promising phrase {phrase!r} found in {where}"
            )

    def _debug_and_share_help(self):
        import argparse

        from hermes_cli.subcommands.debug import build_debug_parser

        parser = argparse.ArgumentParser(prog="hermes")
        sub = parser.add_subparsers(dest="command")
        build_debug_parser(sub, cmd_debug=lambda a: None)

        debug_parser = sub.choices["debug"]
        share_action = next(
            action for action in debug_parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        share_parser = share_action.choices["share"]
        return debug_parser.format_help(), share_parser.format_help()

    def test_real_argparse_help_never_promises_upload(self):
        """`hermes debug --help` / `hermes debug share --help` — the actual
        help argparse prints, not the fallback text `run_debug` prints when
        called with no subcommand."""
        debug_help, share_help = self._debug_and_share_help()
        self._assert_no_banned_phrase(debug_help, "`hermes debug --help`")
        self._assert_no_banned_phrase(share_help, "`hermes debug share --help`")

    def test_module_docstring_never_promises_upload(self):
        import hermes_cli.debug as dbg

        self._assert_no_banned_phrase(
            dbg.__doc__ or "", "hermes_cli/debug.py module docstring"
        )

    def test_run_debug_fallback_help_never_promises_upload(self, capsys):
        from hermes_cli.debug import run_debug

        args = MagicMock()
        args.debug_command = None
        run_debug(args)

        self._assert_no_banned_phrase(
            capsys.readouterr().out, "`hermes debug` fallback help text"
        )

    def test_top_level_help_epilog_never_promises_upload(self):
        import hermes_cli._parser as _parser_mod

        self._assert_no_banned_phrase(
            _parser_mod._EPILOGUE, "top-level `hermes --help` epilog"
        )

    def test_slash_debug_docstring_never_promises_upload(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        self._assert_no_banned_phrase(
            CLICommandsMixin._handle_debug_command.__doc__ or "",
            "CLICommandsMixin._handle_debug_command docstring",
        )


class TestDebugSlashCommand:
    """`/debug [nous|local]` parsing in the CLI/TUI handler.

    The classic CLI and the TUI slash worker both dispatch through
    ``HermesCLI.process_command`` → ``_handle_debug_command(cmd_original)``,
    which parses an optional destination word and builds the args namespace
    handed to ``run_debug_share``.
    """

    def _handler(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        class _Stub(CLICommandsMixin):
            pass

        return _Stub()._handle_debug_command

    def _captured(self, cmd_original):
        captured = {}

        def _fake_run(args):
            captured.update(vars(args))

        with patch("hermes_cli.debug.run_debug_share", _fake_run):
            self._handler()(cmd_original)
        return captured

    def test_bare_debug_defaults_to_paste(self):
        c = self._captured("/debug")
        assert c["nous"] is False and c["local"] is False
        assert c["lines"] == 200 and c["expire"] == 7
        # The slash command IS the consent action → skip the [y/N] prompt
        # (input() would hang inside prompt_toolkit's event loop).
        assert c["yes"] is True


    def test_word_parsing_is_case_insensitive(self):
        c = self._captured("/debug NOUS")
        assert c["nous"] is True


    def test_no_arg_default_keyword(self):
        # Calling with no cmd_original (legacy callers) must still work.
        c = self._captured("")
        assert c["nous"] is False and c["local"] is False


class TestShareConsentGate:
    """`hermes debug share` never uploads, so the old --yes consent gate for
    non-interactive sessions no longer has anything to guard: the command
    now behaves like --local unconditionally, with or without --yes.

    Uses SimpleNamespace rather than MagicMock so ``args.yes`` is a real
    ``False`` — a MagicMock auto-provides a truthy ``.yes``, which would
    have silently bypassed the old gate this class used to test.
    """

    def _args(self, **over):
        from types import SimpleNamespace

        base = dict(lines=50, expire=7, local=False, nous=False,
                    no_redact=False, yes=False)
        base.update(over)
        return SimpleNamespace(**base)




    def test_non_interactive_never_uploads_regardless_of_yes(
        self, hermes_home, capsys, monkeypatch
    ):
        """No TTY + no --yes → still just prints the report locally. There
        is no upload left to gate, so this no longer exits(1)."""
        from hermes_cli.debug import run_debug_share

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(self._args())  # must not raise SystemExit

        mock_upload.assert_not_called()
        out = capsys.readouterr().out
        assert "does not upload" in out


    def test_local_never_prompts(self, hermes_home, capsys, monkeypatch):
        """--local renders to stdout and must not prompt or upload."""
        from hermes_cli.debug import run_debug_share

        def _boom(_):
            raise AssertionError("input() must not be called for --local")

        monkeypatch.setattr("builtins.input", _boom)

        with patch("hermes_cli.dump.run_dump"), \
             patch("hermes_cli.debug.upload_to_pastebin") as mock_upload:
            run_debug_share(self._args(local=True))

        mock_upload.assert_not_called()
        assert "Aborted" not in capsys.readouterr().out

