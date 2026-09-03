"""Fixtures shared across hermes_cli tests (kanban + setup-wizard)."""

from __future__ import annotations

import pytest


_TEST_LOGIN = "trix-testlogin01"
_TEST_PASSWORD = "Tr1xTestPassw0rd0000000000000"


@pytest.fixture
def wizard_app(tmp_path, monkeypatch):
    """A fresh setup-wizard ``FastAPI`` app object + a freshly issued
    ``primary`` login/password (spec 8, §4.2/§8.3).

    The **canonical** construction — ``app_env`` (below) just wraps this in
    a ``TestClient``. Returns the raw app object (not a client) so a caller
    that needs to register an extra route before it's exercised has
    somewhere to hook in without duplicating the "set HERMES_HOME, issue a
    password, create_app()" boilerplate a second time.

    Returns ``(app, (login, password))`` — the plaintext pair a caller
    needs to build an ``Authorization: Basic`` header or hand to
    ``httpx``'s ``auth=`` param; there is no session token any more (HTTP
    Basic resends credentials on every request instead — see
    ``app.py``'s ``_BasicAuthMiddleware``).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary(_TEST_LOGIN, _TEST_PASSWORD)
    from hermes_cli.setup_wizard.app import create_app

    # _run_submit (spec 15) now runs trix_support.run_support_pass() once
    # more at the end of a successful "Готово" -- the SAME live pass
    # /api/support/run drives (network/Telegram/provider-key probes, a
    # doctor subprocess, up to a real 15s-per-check timeout). Left
    # unmocked, every existing "successful submit" test in this suite
    # would transitively make live calls and could take up to minutes
    # per test. Default it to an instant, healthy no-op result here, the
    # same way test_setup_wizard_support_view.py already mocks it
    # per-test for /api/support/run -- a test that wants to exercise the
    # real pass (or a specific verdict) re-monkeypatches
    # ``trix_support.run_support_pass`` itself; the last ``setattr`` for a
    # given test wins, and monkeypatch un-does both at teardown regardless
    # of ordering.
    from hermes_cli import trix_support

    monkeypatch.setattr(
        trix_support,
        "run_support_pass",
        lambda: trix_support.SupportPassResult(
            run_id="test-default-support-pass",
            started_at="t0",
            finished_at="t1",
            checks=(),
            ok=True,
        ),
    )

    return create_app(), (_TEST_LOGIN, _TEST_PASSWORD)


@pytest.fixture
def app_env(wizard_app):
    """``wizard_app``, wrapped in a ``TestClient`` — **not** authenticated.

    Shared across ``test_setup_wizard_app_*.py`` files so each doesn't
    hand-roll its own copy. The client carries no ``Authorization`` header
    of its own; a test that wants an authenticated client uses
    ``logged_in`` instead, or sets ``client.auth = login_password`` itself
    (e.g. to exercise wrong-credential/lockout behavior).
    """
    app, login_password = wizard_app
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="https://testserver"), login_password


@pytest.fixture
def logged_in(app_env):
    """``app_env``'s client, with valid HTTP Basic credentials already
    attached (spec 8, §8.3) — every request this client makes carries
    ``Authorization: Basic ...`` for the ``primary`` slot's login/password,
    the same way a real browser resends it on every request once the user
    has entered it once. There is no login route to call first any more.
    """
    client, (login, password) = app_env
    client.auth = (login, password)
    return client


@pytest.fixture
def logged_in_with_saved_env(tmp_path, monkeypatch):
    """A logged-in client whose ``HERMES_HOME`` already has saved secrets.

    Simulates the wizard's return mode (spec §11/§12.4): the client already
    ran through the wizard once and ``TELEGRAM_BOT_TOKEN`` is on disk. The
    ``primary`` slot (spec 8, §4.2) is permanent — it does not need
    reissuing for a return visit.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure

    save_env_value_secure("TELEGRAM_BOT_TOKEN", "123:abc")

    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary(_TEST_LOGIN, _TEST_PASSWORD)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (_TEST_LOGIN, _TEST_PASSWORD)
    return client


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
