"""Tests for ``hermes_cli.diagnostics_upload`` — the Nous-S3 upload client.

All network I/O is mocked at ``urllib.request.urlopen``; no real requests
are made.
"""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def _resp(*, status=200, body=b""):
    """Build a context-manager mock mimicking ``urllib.request.urlopen``."""
    m = MagicMock()
    m.status = status
    m.getcode.return_value = status
    m.read.return_value = body
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


# ---------------------------------------------------------------------------
# request_upload_url
# ---------------------------------------------------------------------------

class TestRequestUploadUrl:
    def test_happy_path_posts_json_and_returns_dict(self):
        from hermes_cli.diagnostics_upload import request_upload_url

        payload = {
            "success": True,
            "id": "abc-123",
            "uploadUrl": "https://bucket.s3.amazonaws.com/uploads/abc-123.json.gz?sig",
            "viewUrl": "https://support.example.com/diagnostics/abc-123",
            "uploadExpiresInSeconds": 900,
        }
        resp = _resp(status=200, body=json.dumps(payload).encode())

        with patch(
            "hermes_cli.diagnostics_upload.urllib.request.urlopen",
            return_value=resp,
        ) as urlopen:
            result = request_upload_url(content_type="application/gzip", size_bytes=512)

        assert result == payload

        # The request object passed to urlopen carries our JSON body + headers.
        req = urlopen.call_args[0][0]
        assert req.method == "POST"
        assert req.full_url.endswith("/api/diagnostics/upload-url")
        sent = json.loads(req.data.decode())
        assert sent["contentType"] == "application/gzip"
        assert sent["sizeBytes"] == 512
        # urllib lower-cases header keys.
        assert req.headers["Content-type"] == "application/json"

    def test_non_2xx_raises(self):
        from hermes_cli.diagnostics_upload import request_upload_url

        resp = _resp(status=500, body=b"boom")
        with patch(
            "hermes_cli.diagnostics_upload.urllib.request.urlopen",
            return_value=resp,
        ):
            with pytest.raises(RuntimeError):
                request_upload_url()


    def test_base_url_env_override(self, monkeypatch):
        # NAS_BASE is read at import time; re-import the module under the
        # patched env to confirm the override is honoured.
        import importlib

        monkeypatch.setenv("HERMES_DIAGNOSTICS_BASE_URL", "https://staging.example.com")
        import hermes_cli.diagnostics_upload as mod

        mod = importlib.reload(mod)
        try:
            assert mod.NAS_BASE == "https://staging.example.com"
            resp = _resp(
                status=200,
                body=json.dumps({"uploadUrl": "u", "id": "i", "viewUrl": "v"}).encode(),
            )
            with patch(
                "hermes_cli.diagnostics_upload.urllib.request.urlopen",
                return_value=resp,
            ) as urlopen:
                mod.request_upload_url()
            req = urlopen.call_args[0][0]
            assert req.full_url == "https://staging.example.com/api/diagnostics/upload-url"
        finally:
            monkeypatch.delenv("HERMES_DIAGNOSTICS_BASE_URL", raising=False)
            importlib.reload(mod)


# ---------------------------------------------------------------------------
# put_bundle
# ---------------------------------------------------------------------------

class TestPutBundle:
    def test_put_sends_exact_body_and_content_type(self):
        from hermes_cli.diagnostics_upload import put_bundle

        data = b"\x1f\x8b\x08gzipped-bytes"
        resp = _resp(status=200, body=b"")

        with patch(
            "hermes_cli.diagnostics_upload.urllib.request.urlopen",
            return_value=resp,
        ) as urlopen:
            put_bundle("https://bucket.s3.amazonaws.com/uploads/x.json.gz?sig", data)

        req = urlopen.call_args[0][0]
        assert req.method == "PUT"
        # PUT body must be the bundle bytes, unchanged.
        assert req.data == data
        assert req.headers["Content-type"] == "application/gzip"



    def test_http_error_propagates(self):
        from hermes_cli.diagnostics_upload import put_bundle

        err = urllib.error.HTTPError("https://u", 500, "err", {}, io.BytesIO(b""))
        with patch(
            "hermes_cli.diagnostics_upload.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(urllib.error.HTTPError):
                put_bundle("https://u", b"data")


# ---------------------------------------------------------------------------
# share_to_nous — disabled at the network boundary
# ---------------------------------------------------------------------------
#
# share_to_nous used to orchestrate request_upload_url() + put_bundle() to
# push a gzipped debug bundle to Nous-internal S3. Trix Agent never uploads
# customer logs anywhere, and the only thing that used to stop this path was
# a guard 500 lines away in hermes_cli.debug.run_debug_share (--nous forced
# off before any network I/O). A merge conflict resolved the wrong way in
# that one function -- and this repo absorbs upstream merges routinely --
# would have silently re-enabled it with nothing else in the way. The
# function itself now raises immediately, so the guarantee holds regardless
# of how run_debug_share is merged.


class TestShareToNous:
    def test_raises_immediately_with_no_mocks_installed(self):
        """The baseline case: calling share_to_nous at all is refused."""
        from hermes_cli import diagnostics_upload as mod

        with pytest.raises(RuntimeError, match="never uploads"):
            mod.share_to_nous(b"data")

    def test_never_uploads_even_when_the_backend_would_succeed(self):
        """Regression guard for the exact failure mode the review called
        out: even if request_upload_url()/put_bundle() are mocked to
        succeed (i.e. the network *would* accept the upload), share_to_nous
        must still refuse and must never reach either of them. This is what
        the old test_orchestrates_request_then_put covered when the upload
        path was live; now it proves the path stays dead."""
        from hermes_cli import diagnostics_upload as mod

        info = {
            "id": "id-9",
            "uploadUrl": "https://bucket/uploads/id-9.json.gz?sig",
            "viewUrl": "https://support/diagnostics/id-9",
            "expiresAt": "2026-06-20T00:00:00Z",
        }
        blob = b"gzipped-bundle"

        with patch.object(mod, "request_upload_url", return_value=info) as req, \
             patch.object(mod, "put_bundle") as put:
            with pytest.raises(RuntimeError, match="never uploads"):
                mod.share_to_nous(blob)

        req.assert_not_called()
        put.assert_not_called()
