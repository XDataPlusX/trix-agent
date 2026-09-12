"""Contract tests for hermes_cli/setup_wizard/state.py (spec 8, §14).

Covers the two-slot credential model, per-IP + global lockout, and the
"successful verify never writes state.json" cache-friendliness contract.
Assertions are behavioral invariants (see the docstring of each test),
never snapshots of specific stored values.
"""
import json
import time


def _load(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState
    return WizardState


# ---------------------------------------------------------------------------
# §14.1 — completion does not extinguish the primary slot or the wizard.
# ---------------------------------------------------------------------------


def test_primary_survives_completion(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "correct horse battery staple")
    st.mark_completed()

    reloaded = WizardState.load()
    assert reloaded.is_completed() is True
    # completed no longer gates access — the wizard stays open, and the
    # permanent credentials keep working.
    assert reloaded.is_open() is True
    assert reloaded.verify("trix-abc123", "correct horse battery staple", "203.0.113.1") is True


def test_only_disabled_flag_closes_the_wizard(tmp_path, monkeypatch):
    """completed=True must not close the gate; only disabled=True does."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "pw-primary-000000")
    st.mark_completed()
    assert WizardState.load().is_open() is True

    st.set_disabled(True)
    assert WizardState.load().is_open() is False
    assert WizardState.load().is_disabled() is True
    # Disabled blocks login outright, even with correct credentials.
    assert WizardState.load().verify("trix-abc123", "pw-primary-000000", "203.0.113.1") is False

    st.set_disabled(False)
    assert WizardState.load().is_open() is True
    assert WizardState.load().verify("trix-abc123", "pw-primary-000000", "203.0.113.1") is True


# ---------------------------------------------------------------------------
# §14.2 / §14.7 — temporary slot never clobbers primary; primary keeps
# working immediately after (and after) temporary is issued/expires.
# ---------------------------------------------------------------------------


def test_temporary_does_not_clobber_primary(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "primary-password-000")
    st.issue_temporary("temp-password-000", ttl_seconds=3600)

    # The very next request on primary's credentials still succeeds —
    # issuing an emergency password must not disturb it.
    assert WizardState.load().verify("trix-abc123", "primary-password-000", "203.0.113.1") is True
    # The temporary slot works too, with any login value.
    assert WizardState.load().verify("whatever-login", "temp-password-000", "203.0.113.2") is True


def test_primary_keeps_working_after_temporary_expires(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "primary-password-111")
    st.issue_temporary("temp-password-111", ttl_seconds=1)

    # Force the temporary slot's TTL into the past instead of sleeping.
    fresh = WizardState.load()
    fresh._data["temporary"]["expires_at"] = time.time() - 10
    fresh._save()

    reloaded = WizardState.load()
    assert reloaded.verify("anyone", "temp-password-111", "203.0.113.3") is False
    assert reloaded.verify("trix-abc123", "primary-password-111", "203.0.113.3") is True


def test_temporary_does_not_burn_out_on_first_use(tmp_path, monkeypatch):
    """Basic auth resends credentials on every request — a temporary
    password must keep validating across repeated requests until its TTL,
    not die after the first successful check."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_temporary("temp-password-222", ttl_seconds=3600)

    for _ in range(5):
        assert WizardState.load().verify("anyone", "temp-password-222", "203.0.113.4") is True


# ---------------------------------------------------------------------------
# §14.3 — per-IP lockout isolation.
# ---------------------------------------------------------------------------


def test_lockout_is_per_ip_not_global(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")

    attacker_ip = "203.0.113.10"
    for _ in range(5):
        assert WizardState.load().verify("trix-abc123", "wrong", attacker_ip) is False
    assert WizardState.load().retry_after_seconds(attacker_ip) > 0

    # A different IP, with correct credentials, is unaffected.
    victim_ip = "198.51.100.20"
    assert WizardState.load().retry_after_seconds(victim_ip) == 0
    assert WizardState.load().verify("trix-abc123", "the-real-password", victim_ip) is True


def test_ipv6_addresses_share_a_slash_64_bucket(tmp_path, monkeypatch):
    """A single IPv6-enabled host controls its whole /64 — lockout must be
    keyed on the /64, not the full address, or rotating addresses inside
    the same prefix would dodge the 5-attempt budget."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")

    base = "2001:db8:1234:5678"
    for i in range(5):
        ip = f"{base}::{i}"
        assert WizardState.load().verify("trix-abc123", "wrong", ip) is False

    # A fresh address in the same /64 is already locked out.
    same_prefix_ip = f"{base}::dead:beef"
    assert WizardState.load().retry_after_seconds(same_prefix_ip) > 0

    # A different /64 entirely is unaffected.
    other_prefix_ip = "2001:db8:ffff:0000::1"
    assert WizardState.load().retry_after_seconds(other_prefix_ip) == 0


def test_ip_lockout_escalates_and_caps_at_15_minutes(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")
    ip = "203.0.113.50"

    for _ in range(5):
        WizardState.load().verify("trix-abc123", "wrong", ip)
    assert WizardState.load().retry_after_seconds(ip) >= 60

    # Fast-forward past the current lockout and directly land at the
    # failure count where the uncapped exponential formula would exceed
    # the 15-minute (900s) ceiling, to prove the cap holds.
    fresh = WizardState.load()
    fresh._data["failures_by_ip"][ip]["n"] = 8  # next failure -> n=9, over=4
    fresh._data["failures_by_ip"][ip]["locked_until"] = 0
    fresh._save()
    assert WizardState.load().verify("trix-abc123", "wrong", ip) is False
    assert WizardState.load().retry_after_seconds(ip) == 900


# ---------------------------------------------------------------------------
# §14.4 — the global circuit breaker never exceeds 60 seconds and never
# fully closes login.
# ---------------------------------------------------------------------------


def test_global_breaker_never_exceeds_60_seconds(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")

    # Drive more than 50 failures within the trailing hour, spread across
    # many distinct IPs so no single IP's own lockout is what's observed.
    for i in range(11):
        ip = f"203.0.113.{i + 1}"
        for _ in range(5):
            WizardState.load().verify("trix-abc123", "wrong", ip)

    # An IP that has never failed anything still gets slowed down by the
    # global breaker — but never for more than 60 seconds.
    innocent_ip = "198.51.100.99"
    wait = WizardState.load().retry_after_seconds(innocent_ip)
    assert 0 < wait <= 60

    # Even correct credentials from the innocent IP are throttled while
    # the breaker is tripped...
    assert WizardState.load().verify("trix-abc123", "the-real-password", innocent_ip) is False

    # ...but the breaker is a brake, not a gate: once its window elapses,
    # login works again without any special reset.
    fresh = WizardState.load()
    fresh._data["global_locked_until"] = time.time() - 1
    fresh._save()
    assert WizardState.load().verify("trix-abc123", "the-real-password", innocent_ip) is True


def test_global_breaker_sustained_trickle_does_not_lock_clean_ip_forever(tmp_path, monkeypatch):
    """Review finding 3 (blocker): §8.1 says the global breaker "полностью
    закрыть вход глобально нельзя никогда". The naive implementation
    re-arms `global_locked_until = now + 60` on *every* failure for as
    long as the trailing-hour window still holds more than the threshold
    — so once an initial burst trips it, a sustained trickle of roughly
    one failure a minute (trivial with a routed IPv6 /56: thousands of
    fresh /64 buckets, each good for 5 free attempts) keeps re-arming the
    breaker for as long as the attack continues, walling out a clean IP
    with the *correct* password indefinitely.

    Reproduces the chain, not a single read: an initial burst crosses the
    threshold once (expected — the breaker should trip), then a trickle
    of single failures spaced past the 60s window (so each one is
    actually processed, not swallowed by the still-active lock) continues
    for a while. A clean IP must get free again between arms, not stay
    locked for the whole trickle.
    """
    import hermes_cli.setup_wizard.state as state_mod

    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")

    clock = [1_000_000.0]
    monkeypatch.setattr(state_mod.time, "time", lambda: clock[0])

    innocent_ip = "198.51.100.99"

    # Initial burst, well over the 50/hour threshold, from many distinct
    # attacker buckets — trips the breaker once, as intended.
    for i in range(60):
        ip = f"2001:db8:{i:x}::1"
        WizardState.load().verify("trix-abc123", "wrong", ip)
    assert WizardState.load().retry_after_seconds(innocent_ip) > 0

    # Move past that single 60s window.
    clock[0] += 61
    assert WizardState.load().retry_after_seconds(innocent_ip) == 0

    # The attacker keeps sending one failure roughly every 65s (each from
    # a fresh /64 bucket, so none of these individually trip the per-IP
    # lockout) for a long stretch. None of these, on their own, cross the
    # 50-per-hour threshold from a freshly-armed state.
    locked_ticks = 0
    for i in range(60, 90):
        ip = f"2001:db8:{i:x}::1"
        clock[0] += 65
        WizardState.load().verify("trix-abc123", "wrong", ip)
        if WizardState.load().retry_after_seconds(innocent_ip) > 0:
            locked_ticks += 1

    # The bug made every single one of these ticks locked (perpetual
    # re-arming); the fix must leave the clean IP free for the trickle.
    assert locked_ticks == 0
    assert (
        WizardState.load().verify("trix-abc123", "the-real-password", innocent_ip) is True
    )


def test_global_breaker_caps_even_a_corrupted_larger_value(tmp_path, monkeypatch):
    """Defense in depth: a hand-edited/corrupted global_locked_until far in
    the future must still read back as at most 60 seconds away."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st._data["global_locked_until"] = time.time() + 999999
    st._save()
    assert WizardState.load().retry_after_seconds("203.0.113.1") <= 60


# ---------------------------------------------------------------------------
# §14.8 — state.json permissions survive every mutation.
# ---------------------------------------------------------------------------


def test_state_file_stays_0600_through_mutations(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    from hermes_cli.setup_wizard.state import state_path

    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")
    assert oct(state_path().stat().st_mode)[-3:] == "600"

    st.issue_temporary("temp-pw", ttl_seconds=600)
    assert oct(state_path().stat().st_mode)[-3:] == "600"

    for _ in range(3):
        WizardState.load().verify("trix-abc123", "wrong", "203.0.113.77")
    assert oct(state_path().stat().st_mode)[-3:] == "600"

    WizardState.load().set_disabled(True)
    assert oct(state_path().stat().st_mode)[-3:] == "600"


# ---------------------------------------------------------------------------
# §14.15 — a successful verification never writes state.json.
# ---------------------------------------------------------------------------


def test_successful_verify_does_not_write_state_json_when_ip_is_clean(tmp_path, monkeypatch):
    """§14.15, refined by owner ruling 2026-08-25: the invariant is that a
    successful login must not *cost* a write, not that state can never
    change on success. An IP with no prior failures (the common case,
    including a page's status polling under HTTP Basic — §8.3.2) has
    nothing to reset, so this must stay free."""
    WizardState = _load(monkeypatch, tmp_path)
    from hermes_cli.setup_wizard.state import state_path

    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")
    before_mtime_ns = state_path().stat().st_mtime_ns

    save_calls = []
    orig_save = WizardState._save

    def spy_save(self):
        save_calls.append(1)
        orig_save(self)

    monkeypatch.setattr(WizardState, "_save", spy_save)

    assert WizardState.load().verify("trix-abc123", "the-real-password", "203.0.113.5") is True
    assert save_calls == []
    assert state_path().stat().st_mtime_ns == before_mtime_ns

    # A failed verification, in contrast, does mutate the counters and
    # therefore does write.
    assert WizardState.load().verify("trix-abc123", "wrong", "203.0.113.5") is False
    assert save_calls == [1]


def test_successful_verify_resets_ip_failure_count_exactly_once(tmp_path, monkeypatch):
    """Review finding 6, refined per owner ruling 2026-08-25: a successful
    login writes state.json if and only if there is something to reset —
    this IP's failure counter is nonzero. Otherwise a client who mistypes
    a few times, then logs in correctly, carries a stale count into an
    unrelated typo hours or days later and gets locked out for no recent
    reason. The reset costs exactly one write, right when a run of
    failures ends in a success; the very next success from the same
    (now clean) IP is back to costing nothing, and a fresh run of
    failures afterward starts the free-attempt budget from zero again."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")
    ip = "203.0.113.6"

    # Four failures — under the 5-free-attempt budget, so not locked yet.
    for _ in range(4):
        assert WizardState.load().verify("trix-abc123", "wrong", ip) is False
    assert WizardState.load().retry_after_seconds(ip) == 0

    save_calls = []
    orig_save = WizardState._save

    def spy_save(self):
        save_calls.append(1)
        orig_save(self)

    monkeypatch.setattr(WizardState, "_save", spy_save)

    # The successful login after that run of failures DOES write — exactly
    # once — to clear the stale counter.
    assert WizardState.load().verify("trix-abc123", "the-real-password", ip) is True
    assert save_calls == [1]

    # A second, immediately-following success from the same now-clean IP
    # costs nothing again — the reset itself doesn't keep re-triggering.
    save_calls.clear()
    assert WizardState.load().verify("trix-abc123", "the-real-password", ip) is True
    assert save_calls == []

    # And the reset is real, not cosmetic: a fresh run of 4 failures
    # "the next day" starts from zero — it takes a 5th failure to lock,
    # the same as any IP that never failed before, not the 1st.
    for _ in range(4):
        assert WizardState.load().verify("trix-abc123", "wrong", ip) is False
    assert WizardState.load().retry_after_seconds(ip) == 0


# ---------------------------------------------------------------------------
# §9.6 — the pre-spec-8 flat state.json shape must not crash the reader.
# ---------------------------------------------------------------------------


def test_old_flat_format_does_not_crash(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    from hermes_cli.setup_wizard.state import state_path

    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(
        json.dumps(
            {
                "algo": "scrypt",
                "salt": "aa" * 16,
                "hash": "deadbeef",
                "failures": 2,
                "locked_until": 0,
                "expires_at": None,
                "completed": True,
            }
        ),
        encoding="utf-8",
    )

    st = WizardState.load()
    assert st.is_open() is True  # no `disabled` key in the old shape
    assert st.is_completed() is True
    assert st.verify("admin", "anything", "203.0.113.6") is False
    assert st.retry_after_seconds("203.0.113.6") == 0

    # And it must still be mutable afterwards (issuing credentials over an
    # old-shaped file works and doesn't need a migration step).
    st.issue_primary("trix-newlogin", "brand-new-password")
    assert WizardState.load().verify("trix-newlogin", "brand-new-password", "203.0.113.6") is True


def test_missing_state_file_reads_closed_credentials_but_open_gate(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    assert st.is_open() is True  # never disabled -> open by default
    assert st.is_completed() is False
    assert st.verify("anyone", "anything", "203.0.113.7") is False


# ---------------------------------------------------------------------------
# Corruption must still fail closed (carried over from the pre-spec-8 suite).
# ---------------------------------------------------------------------------


def test_corrupt_numeric_fields_fail_closed(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    from hermes_cli.setup_wizard.state import state_path

    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(
        json.dumps(
            {
                "primary": {"login": "trix-abc", "password_hash": "scrypt$16384$8$1$AA==$BB=="},
                "failures_by_ip": {"203.0.113.8": {"n": "many", "locked_until": "soon"}},
                "global_locked_until": "later",
            }
        ),
        encoding="utf-8",
    )
    st = WizardState.load()
    assert st.retry_after_seconds("203.0.113.8") == 0
    assert st.verify("trix-abc", "wrong-anyway", "203.0.113.8") is False


def test_wrong_login_and_wrong_password_both_just_fail(tmp_path, monkeypatch):
    """§14.5 — state.py doesn't need to special-case which part was wrong;
    both simply return False, giving the caller a uniform signal to build
    an identical HTTP response on."""
    WizardState = _load(monkeypatch, tmp_path)
    st = WizardState.load()
    st.issue_primary("trix-abc123", "the-real-password")

    assert WizardState.load().verify("wrong-login", "the-real-password", "203.0.113.9") is False
    assert WizardState.load().verify("trix-abc123", "wrong-password", "203.0.113.9") is False


# ---------------------------------------------------------------------------
# Hash format (§13.1) — reuse the dashboard basic-auth plugin's format.
# ---------------------------------------------------------------------------


def test_password_stored_as_shared_scrypt_format_never_plaintext(tmp_path, monkeypatch):
    WizardState = _load(monkeypatch, tmp_path)
    from hermes_cli.setup_wizard.state import state_path

    st = WizardState.load()
    st.issue_primary("trix-abc123", "super-secret-value-xyz")
    st.issue_temporary("another-secret-value-abc", ttl_seconds=600)

    raw = state_path().read_text(encoding="utf-8")
    assert "super-secret-value-xyz" not in raw
    assert "another-secret-value-abc" not in raw
    data = json.loads(raw)
    assert data["primary"]["password_hash"].startswith("scrypt$")
    assert data["temporary"]["password_hash"].startswith("scrypt$")
