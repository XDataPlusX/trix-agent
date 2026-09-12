"""Deterministic core of the support bot (spec 15,
``docs/product/PROMPT-spec15-support-page.md``).

Lives here, not spread across ``hermes_cli/setup_wizard/*`` or
``hermes_cli/doctor.py``, for the same reason as ``trix_status.py`` and
``trix_doctor_verdict.py``: our product logic in our own module, one-line
imports at the call sites, upstream files untouched.

**The single hard constraint this module exists to satisfy.** Per the
spec's "Единственное жёсткое ограничение": the support bot has no
terminal, ever — the wizard process runs as the ``user`` account, which
has host ``NOPASSWD:ALL`` and ``docker`` group membership, i.e. it is
root on the host. Giving a model that reads free client text a shell in
that process is the one decision in this whole feature that cannot be
undone once real clients exist. The fix is structural, not a prompt: a
**closed registry** (``SUPPORT_ACTIONS`` below) of fixed-verb actions,
each bound to a zero-argument callable at *module import time*. There is
no parameter slot for a package name, a path, or a command fragment to
flow through — not "the model is told not to", but "there is nowhere for
it to go". ``SupportAction.__post_init__`` enforces this at import time
(a handler with any parameter fails the whole module's import), and
``tests/hermes_cli/test_trix_support.py`` re-asserts it as a standing
test so a future edit that quietly adds a parameter is caught twice.

**What this module does NOT do.** No model, no chat loop, no HTTP route.
Those are the next task (spec 15's "Экраны и поток" / "/support" command)
and are explicitly out of scope here — see the delegating brief. This
module only provides the two primitives that layer will need: the
registry, and :func:`run_support_pass` (checks → fix-where-possible →
recheck → structured verdict).

**Why the two doctor actions shell out to the CLI instead of importing
`run_doctor()`/`run_doctor_with_verdict()` directly.** The brief calls
out ``hermes_cli/setup_wizard/app.py``'s existing mistake: it bounds a
600s install hook with a thread/future timeout, and on timeout the
underlying subprocess keeps running — the wrapper only gives up
*waiting*, it does not kill anything. A Python-level timeout around an
in-process call has the exact same problem: a hung call inside
``doctor.py`` (roughly 100 individual checks, not all of which are
guaranteed to carry their own bound) cannot be killed from outside a
thread. Every OTHER check/fix in this module calls a function that
already enforces its own real timeout at the actual I/O boundary
(``httpx.Client(timeout=...)`` in ``validate.py``, ``subprocess.run(...,
timeout=...)`` in ``docker_preflight.py``/``trix_setup_service_check.py``/
``gateway_ctl.py``) — for those, the extra ``_execute()`` wrapper below is
pure defense in depth. For the doctor pass specifically, there is no such
guarantee, so it runs the same way ``gateway_ctl.py`` already runs
``hermes gateway restart`` — as a real OS subprocess, via
``python -m hermes_cli.main doctor --json --exit-code [--fix]`` (the
same ``--json``/``--exit-code``/``--fix`` flags ``trix_doctor_verdict.py``
already wires into ``cmd_doctor``) — bounded by ``subprocess.run(...,
timeout=...)``, which genuinely terminates the child process on timeout.
Nothing about doctor's own ~100 checks is reimplemented; this only picks
a boundary that can actually be killed.

**Why `gateway_restart` is a consequence, not a ritual (corrected
2026-09-03).** An earlier version of this module restarted the gateway
unconditionally on every pass, reasoning that no check in the *delegated*
list could detect "gateway process not running". That was a bug, caught
in review: a client whose bot is working fine and who presses the button
out of curiosity would have their live conversation cut for nothing —
against the owner's own principle that the interface never does what it
has not verified. The real gap was that the delegated check list had
silently dropped the spec's own item 5 ("состояние шлюза: запущен, когда
перезапускался, отвечает ли бот",
``docs/product/PROMPT-spec15-support-page.md``, «Закрытый список
действий») during scoping. The fix restores that check
(:func:`_check_gateway_state` — ``systemctl --user is-active`` on the
gateway's own unit, same mechanism and same silence contract as
``trix_setup_service_check.check_trix_setup_service``) and wires
``gateway_restart`` as its fix, through the same generic ``FIX_FOR_CHECK``
chain :func:`_check_doctor_no_fix` already uses: restart is attempted, and
its own genuinely independent recheck (a fresh ``systemctl --user
is-active`` after the restart, not the fix's own claimed success) runs,
*only* when ``gateway_state`` itself reports the gateway is not active. A
machine that is already healthy never has its gateway touched.

**Why only two checks get an automatic fix-and-recheck chain.**
``FIX_FOR_CHECK`` maps ``doctor_no_fix → doctor_fix`` and
``gateway_state → gateway_restart`` — the only two pairs in the closed
list where the paired fix is both fast/non-disruptive enough to run
inside every automatic pass AND a genuine "two sides of the same coin"
with its check (``hermes doctor --fix`` vs. ``hermes doctor``; restarting
the unit vs. observing its state). ``ensure_tool``, ``sandbox_image``, and
``disk_cleanup`` are now IMPLEMENTED (see below) but deliberately NOT
added to ``FIX_FOR_CHECK``: unlike a doctor pass or a restart, they cross
real, possibly slow network boundaries (up to 900s / 600s respectively —
see their own docstrings) — silently folding either into every
``run_support_pass()`` a client's "бот не отвечает" triggers would turn a
diagnostic sweep meant to finish quickly into one that can run for
tens of minutes on a failing check, the same class of over-eager
unconditional behavior ``gateway_restart``'s own correction above (and
review) already caught once. They stay directly callable through
``SUPPORT_ACTIONS`` — exactly the shape the future model-driven layer
(module docstring: "No model, no chat loop... those are the next task")
is expected to dispatch by NAME once it exists, not through this file's
own automatic pass. ``config_edit`` remains UNIMPLEMENTED — not a gap,
an explicit owner decision (see the comment on its registry entry).

**No fourth trigger for `gateway_restart` — `config_edit` was declined.**
An earlier draft of this docstring reserved a note here for "``config_edit``
will eventually need to restart the gateway too, once implemented." The
owner decided (2026-09-03) not to implement ``config_edit`` at all — see
its registry entry's comment for the rationale. There is therefore no
third trigger to design a hook for; if a future owner ruling reopens
``config_edit``, the gateway-restart-chaining question above still
applies and can be revisited then, but nothing is built speculatively for
it now.

**Client vs. internal reporting (spec §3, owner rulings).** The client
never sees a check name, a step count, or a log line — only one of three
fixed Russian sentences from :func:`build_client_report`, chosen purely
from the structural verdict. Everything else (every raw check payload,
every internal error string, every timing) goes to
:func:`write_internal_report`, appended as JSON to a file under
``HERMES_HOME`` — the only telemetry channel this product has, per the
brief ("мониторинга у продукта нет"). :func:`record_feedback` appends the
client's "помогло?" answer to the same file, correlated by
``SupportPassResult.run_id``, for a future ``/debug`` reader to pick up.

**No Hermes/Nous, no jargon, Russian only.** Every string a client can
see in this module is reviewed for this by hand; none of them name a
tool, a check, or the upstream project.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from hermes_constants import get_hermes_home

# The one escalation address the whole feature is allowed to name (spec
# "решения владельца" п.5: "Ни почты, ни формы, ни телефона в интерфейсе").
SUPPORT_ESCALATION_CONTACT = "@Trix_Agent_Support_Bot"

_DOCTOR_NO_FIX_TIMEOUT = 180.0
_DOCTOR_FIX_TIMEOUT = 300.0
# gateway_ctl.restart_gateway: up to two ``_RESTART_TIMEOUT`` (120s each,
# restart then start-fallback) subprocess calls, plus wait_bot_alive's own
# default 90s poll — the outer bound must exceed that sum, or a call that
# is legitimately still working looks like a hang.
_GATEWAY_RESTART_TIMEOUT = 400.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Zero-argument check wrappers. Every one of these reads its own inputs
# (tokens, keys, proxy) from the current process's config/.env — never from
# a caller-supplied argument — which is what keeps every handler's own
# signature at zero parameters (see the module docstring's security note).
# ---------------------------------------------------------------------------


def _active_provider_env_var() -> str | None:
    """The env var backing the currently configured model provider, if any.

    Small, local re-derivation of the same lookup
    ``hermes_cli/setup_wizard/app.py``'s private ``_current_provider_env_var``
    performs — not imported from there (leading-underscore, and this task
    does not embed into ``setup_wizard/*`` at all), but the same two
    public building blocks: ``model.provider`` from config, and
    ``providers.get_provider_profile(...).env_vars[0]``.
    """
    from hermes_cli.config import load_config
    from providers import get_provider_profile

    cfg = load_config() or {}
    model_cfg = cfg.get("model")
    provider_name = ""
    if isinstance(model_cfg, dict):
        provider_name = (model_cfg.get("provider") or "").strip()
    if not provider_name:
        return None
    try:
        profile = get_provider_profile(provider_name)
    except Exception:
        return None
    if profile is None or not profile.env_vars:
        return None
    return profile.env_vars[0]


def _check_telegram_token() -> dict:
    """Spec check 1: live ``getMe`` via ``validate.check_telegram_token``."""
    from hermes_cli.config import get_env_value
    from hermes_cli.setup_wizard.validate import check_telegram_token

    token = get_env_value("TELEGRAM_BOT_TOKEN") or ""
    proxy = get_env_value("TELEGRAM_PROXY")
    if not token:
        return {"ok": False, "error": "Токен Телеграм-бота не настроен."}
    return check_telegram_token(token, proxy)


def _check_provider_key() -> dict:
    """Spec check 2: live provider-key probe via ``validate.check_provider_key``."""
    from hermes_cli.config import get_env_value
    from hermes_cli.setup_wizard.validate import check_provider_key

    proxy = get_env_value("TELEGRAM_PROXY")
    env_var = _active_provider_env_var()
    if not env_var:
        # Mirrors check_provider_key's own "no live probe defined" contract —
        # nothing configured is not itself a failure of THIS check.
        return {"ok": True, "checked": False, "message": "Провайдер не настроен."}
    value = get_env_value(env_var) or ""
    if not value:
        return {"ok": False, "checked": False, "message": "Ключ провайдера не задан."}
    return check_provider_key(env_var, value, proxy)


def _check_proxy_syntax() -> dict:
    """Spec check 4: network-free proxy syntax check."""
    from hermes_cli.config import get_env_value
    from hermes_cli.setup_wizard.validate import check_proxy_syntax

    return check_proxy_syntax(get_env_value("TELEGRAM_PROXY"))


def _check_network_proxy() -> dict:
    """Spec check 3: reachability with "виноват прокси / виновата сеть" blame.

    ``blame`` is internal-only detail (never surfaced to a client — see
    :func:`build_client_report`): ``"proxy_syntax"`` when the proxy string
    itself is malformed, ``"proxy"`` when a configured proxy blocks
    everything routed through it while direct-only targets still answer,
    ``"network"`` when even the direct-only targets are unreachable, and
    ``None`` when the check is fully green.
    """
    from hermes_cli.config import get_env_value
    from hermes_cli.setup_wizard.validate import check_reachability

    proxy = get_env_value("TELEGRAM_PROXY")
    result = check_reachability(proxy)
    proxy_invalid = bool(result.get("proxy_invalid"))
    telegram_ok = bool(result.get("telegram"))
    direct = result.get("direct") or {}
    direct_any_ok = any(direct.values())
    ok = telegram_ok and not proxy_invalid

    if proxy_invalid:
        blame = "proxy_syntax"
    elif ok:
        blame = None
    elif proxy and direct_any_ok:
        blame = "proxy"
    else:
        blame = "network"

    return {**result, "ok": ok, "blame": blame}


def _check_sandbox() -> dict:
    """Spec check 5: ``docker_preflight.check_docker_backend``."""
    from hermes_cli.docker_preflight import check_docker_backend

    return check_docker_backend().to_dict()


def _check_browser() -> dict:
    """Spec check 6: ``browser_preflight.check_chromium_backend``."""
    from hermes_cli.browser_preflight import check_chromium_backend

    return check_chromium_backend().to_dict()


def _check_search() -> dict:
    """Spec check 7: ``search_preflight.check_search_backend``.

    Раньше звала ``check_ddgs_backend()`` — та проверяет только
    импортируемость пакета ``ddgs`` и не смотрит на
    ``web.search_backend``, так что клиент на SearXNG/Brave получал
    вердикт про чужой поисковик (разбор 2026-09-04). Проход поддержки
    обязан проверять бэкенд, который клиент реально выбрал, живым
    запросом — это и делает ``check_search_backend()``.
    ``check_ddgs_backend()`` остаётся как есть для ``scripts/install.sh``
    (там вопрос буквально "встал ли пакет ddgs только что").
    """
    from hermes_cli.search_preflight import check_search_backend

    return check_search_backend().to_dict()


def _check_wizard_service() -> dict:
    """Spec check 8: the wizard's own rescue-door unit
    (``trix_setup_service_check.check_trix_setup_service``).

    That function's own contract: silent (no ``issues`` entries) both when
    everything is fine AND when the check legitimately does not apply on
    this machine (not Linux, s6 container, unit never provisioned) — an
    empty ``issues`` list is "ok" either way, exactly matching its own
    documented "quiet-success" posture.
    """
    from hermes_cli.trix_setup_service_check import check_trix_setup_service

    issues: list = []
    check_trix_setup_service(issues)
    return {"ok": not issues, "issues": list(issues)}


_GATEWAY_STATE_SYSTEMCTL_TIMEOUT = 10.0


def _run_gateway_systemctl_is_active(unit_name: str) -> subprocess.CompletedProcess:
    """The one real OS boundary ``_check_gateway_state`` crosses — its own
    function so tests can replace it with a fake ``CompletedProcess``
    instead of spawning a real ``systemctl``."""
    return subprocess.run(
        ["systemctl", "--user", "is-active", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GATEWAY_STATE_SYSTEMCTL_TIMEOUT,
        check=False,
    )


def _check_gateway_state() -> dict:
    """Spec check 5 (original closed list — restored 2026-09-03; the
    delegated task's own check list had dropped it during scoping, see the
    module docstring): is the gateway currently up, via ``systemctl --user
    is-active`` on the gateway's own unit.

    Same mechanism, and the same silence contract, as
    ``trix_setup_service_check.check_trix_setup_service`` (its own
    docstring): reports ``ok=True`` (no opinion, ``applicable=False``)
    exactly where that check does — not Linux, an s6-supervised container,
    or the unit simply not present (a machine never provisioned as a Trix
    client VM, or a developer box) — because unit absence alone cannot
    distinguish "no gateway service by design" from "provisioning silently
    failed", and a ``systemctl`` that could not even be asked (missing
    binary, no user D-Bus session, timeout) is treated the same way: not
    proof the gateway is down.
    """
    from hermes_cli.gateway import get_systemd_unit_path, is_linux
    from hermes_cli.service_manager import detect_service_manager

    if not is_linux():
        return {"ok": True, "applicable": False, "reason": "not_linux"}
    if detect_service_manager() == "s6":
        return {"ok": True, "applicable": False, "reason": "s6_container"}

    unit_path = get_systemd_unit_path()
    if not unit_path.exists():
        return {"ok": True, "applicable": False, "reason": "unit_absent"}

    try:
        proc = _run_gateway_systemctl_is_active(unit_path.stem)
    except (OSError, subprocess.SubprocessError):
        return {"ok": True, "applicable": False, "reason": "systemctl_unavailable"}

    state = proc.stdout.strip()
    active = state == "active"
    return {"ok": active, "applicable": True, "active": active, "raw_state": state}


def _run_doctor_subprocess(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The one real OS boundary the two doctor actions cross — kept as its
    own function so tests can replace it with a fake ``CompletedProcess``
    instead of actually spawning ``hermes doctor`` (slow, and touches the
    real machine's packages/tools).

    ``encoding``/``errors`` заданы явно, как у трёх соседних вызовов и как
    требует правило репозитория: без них декодирование идёт в системной
    кодировке и посторонний байт в выводе доктора роняет починку
    ``UnicodeDecodeError``-ом вместо того, чтобы вернуть результат. У
    клиента этот вывод собирается из чужих инструментов, так что содержимое
    мы не контролируем."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_doctor_cli(*, fix: bool, timeout: float) -> dict:
    """Invoke ``hermes doctor --json --exit-code [--fix]`` as a subprocess.

    Same pattern ``gateway_ctl._run_gateway_cli`` already uses for the same
    reason (module docstring): calling the CLI out-of-process means this
    module never diverges from what ``hermes doctor`` actually does, and a
    hung check inside doctor's ~100-check sweep is killed by
    ``subprocess.run``'s own timeout instead of merely abandoned.
    """
    cmd = [sys.executable, "-m", "hermes_cli.main", "doctor", "--json", "--exit-code"]
    if fix:
        cmd.append("--fix")

    try:
        proc = _run_doctor_subprocess(cmd, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Доктор не уложился в {timeout:g} с."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"Не удалось запустить доктора: {exc}"}

    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "error": "Доктор вернул нечитаемый результат.",
            "returncode": proc.returncode,
        }
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Доктор вернул неожиданный формат результата."}

    # Belt and suspenders: verdict_json()'s own "ok" and doctor_exit_code()'s
    # process exit code are computed from the same DoctorRunResult and
    # should always agree — this only guards against the two ever drifting.
    payload["ok"] = bool(payload.get("ok")) and proc.returncode == 0
    return payload


def _check_doctor_no_fix() -> dict:
    """Spec check 9: full doctor run, no ``--fix``."""
    return _run_doctor_cli(fix=False, timeout=_DOCTOR_NO_FIX_TIMEOUT)


def _fix_doctor() -> dict:
    """Spec fix 11: full doctor run with ``--fix``."""
    return _run_doctor_cli(fix=True, timeout=_DOCTOR_FIX_TIMEOUT)


def _fix_gateway_restart() -> dict:
    """Spec fix 16: ``gateway_ctl.restart_gateway`` + ``wait_bot_alive``.

    Applied only as the fix side of :func:`_check_gateway_state` (see
    ``FIX_FOR_CHECK`` and the module docstring) — never unconditionally. A
    healthy gateway is never restarted just because a client pressed the
    button.
    """
    from hermes_cli.config import get_env_value
    from hermes_cli.setup_wizard.gateway_ctl import restart_gateway, wait_bot_alive

    token = get_env_value("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        return {"ok": False, "error": "Токен бота не настроен — перезапуск не поможет."}
    proxy = get_env_value("TELEGRAM_PROXY")

    restart_result = restart_gateway()
    if not restart_result.get("ok"):
        return {"ok": False, "error": restart_result.get("message")}

    return wait_bot_alive(
        token,
        proxy,
        pre_pid=restart_result.get("pre_pid"),
        pre_platform_stamp=restart_result.get("pre_platform_stamp"),
    )


# ``install.sh --ensure node,browser`` internally bounds its two network
# steps at ``NODE_DEPS_TIMEOUT`` (600s default) each via its own
# ``run_with_timeout`` — but the THIRD step this same invocation can take
# (``install_node``'s direct ``curl`` fetch of a Node tarball, reached only
# when Node itself is missing or too old) has no internal bound of any kind
# in that script. 900s is a deliberate single real ceiling for the whole
# call: generous enough that one legitimately slow network step (the
# realistic case — Node is normally already provisioned, so only the
# Chromium/agent-browser fetch actually runs) completes on a modest VPS
# link, while still being a genuine ``subprocess.run(timeout=...)`` kill —
# not a ``ThreadPoolExecutor`` future that merely stops waiting while the
# child keeps running (the wizard's own historical bug this module's
# docstring already warns against repeating).
_ENSURE_TOOL_TIMEOUT = 900.0


def _run_ensure_tool_subprocess(cmd: list[str], env: dict, timeout: float) -> subprocess.CompletedProcess:
    """The one real OS boundary ``_fix_ensure_tool`` crosses — its own
    function so tests can fake it instead of actually spawning
    ``install.sh`` (real network, real npm, genuinely minutes on a real
    machine)."""
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env, check=False,
    )


def _fix_ensure_tool() -> dict:
    """Spec fix 12: догрузить недостающий инструмент — the ONE concrete
    target this zero-argument action can ever mean is the browser layer
    (``STATUS_cloudinit.md`` §4 gap 2: ``install.sh`` now runs without
    ``--skip-browser``, but the client config template still pins
    ``browser.backend: "off"`` — local Chromium on top of whatever the
    installer left behind — and on the audited machine the installer left
    nothing behind).

    Reuses ``scripts/install.sh``'s OWN ``--ensure`` mechanism (``node,
    browser`` — the "browser" case internally checks/installs node first,
    same as ``hermes_cli/dep_ensure.py``'s ``ensure_dependency("browser")``
    already does) rather than re-deriving dependency detection or download
    logic here. The install-script LOCATOR is reused too
    (``dep_ensure._find_install_script`` — leading-underscore, but this is
    NOT the same situation the module docstring calls out for
    ``setup_wizard/app.py``'s private helper: that one was UI-layer and
    wizard-specific; this one is ``dep_ensure.py``'s general-purpose
    bundled-wheel-vs-git-checkout script resolver, already exercised by
    its own tests, and re-deriving that resolution here would be exactly
    the duplication CLAUDE.md's contribution rubric warns against).

    Invoked as our own real ``subprocess.run(..., timeout=...)`` — never a
    ``ThreadPoolExecutor`` future wrapped around an in-process call, and
    never ``dep_ensure.ensure_dependency`` itself (that helper's own
    ``subprocess.run(cmd, env=run_env)`` call has NO timeout at all) — so a
    genuinely wedged download is actually killed, matching every other
    long-running action in this module.

    Why this target needs no apt/root guard, unlike a future ripgrep/ffmpeg
    target might: both steps ``--ensure node,browser`` can reach on the
    success path — ``install_node``'s direct curl+tar fetch, and
    ``ensure_browser``'s ``npm install -g --prefix $HERMES_HOME/node`` of
    ``agent-browser`` + the bundled ``camofox`` Chromium — write only into
    the agent user's OWN ``$HERMES_HOME/node`` directory (writable by that
    user since the 2026-09-03 install-recipe fix) and never shell to
    ``apt``/``sudo``. The only ``sudo`` text inside ``ensure_browser`` is an
    inert printed hint shown AFTER a failed Chromium fetch, never executed.
    A hypothetical future target that needs ``install_system_packages``
    (which does use ``sudo``) would need its own non-interactive-sudo
    guard — out of scope for the one target implemented here.

    Independently verifies success via ``check_chromium_backend()`` — the
    exact ``check_fn`` the browser toolset's schema itself gates on —
    after the subprocess returns, rather than trusting ``install.sh``'s own
    exit code: ``ensure_mode`` has no ``exit $?`` of its own, and
    ``ensure_browser()``'s last line is an unconditional ``return 0`` even
    when the Chromium fetch failed. Same "a subprocess with no return
    value" problem ``tools_view.run_tool_install``'s own docstring already
    documents for ``_run_post_setup`` — same fix (independent before/after
    readiness probe) applied here instead of trusting the exit code.

    NOTE (``STATUS_cloudinit.md``, "Найдено ревью 2026-09-03"): a RUNNING
    gateway process caches Chromium presence at its own startup, so this
    action succeeding does not by itself make ``browser_*`` tools appear in
    a live session's schema — restarting the gateway to pick that up is
    deliberately NOT chained here (out of scope for this task; the module
    docstring's "future third trigger" note already earmarks the analogous
    chain-a-fix-to-``gateway_restart`` decision for ``config_edit``, which
    this codebase's owner has separately decided NOT to implement — see the
    comment on that action's registry entry below).
    """
    from hermes_cli.browser_preflight import check_chromium_backend
    from hermes_cli.dep_ensure import _find_install_script
    from tools.environments.local import hermes_subprocess_env

    if check_chromium_backend().ok:
        return {"ok": True, "already": True, "message": "Браузер уже настроен и готов к работе."}

    script, shell = _find_install_script()
    if script is None or shell != "bash":
        # Windows (install.ps1) is not a real target for this product —
        # Trix Agent client machines are Linux VPS instances
        # (docs/product/deployment-requirements.md) — so a missing bash
        # installer here means the checkout itself is broken, not that a
        # PowerShell path needs wiring.
        return {"ok": False, "error": "Файл установщика не найден на этой машине."}

    run_env = hermes_subprocess_env(inherit_credentials=False)
    run_env["IS_INTERACTIVE"] = "false"

    try:
        proc = _run_ensure_tool_subprocess(
            ["bash", str(script), "--ensure", "node,browser"], run_env, _ENSURE_TOOL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Установка не уложилась в {_ENSURE_TOOL_TIMEOUT:g} с."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"Не удалось запустить установщик: {exc}"}

    if check_chromium_backend().ok:
        return {"ok": True, "already": False, "message": "Инструмент установлен."}

    tail = (proc.stderr or proc.stdout or "").strip()
    return {
        "ok": False,
        "error": tail[-500:] if tail else "Установщик не смог настроить инструмент.",
    }


# ``docker image inspect`` is a local daemon query — no network — so a
# short bound is enough; matches the reasoning behind
# ``docker_preflight._DEFAULT_TIMEOUT_SECONDS`` (a healthy daemon answers
# in well under a second).
_SANDBOX_IMAGE_INSPECT_TIMEOUT = 10.0
# STATUS_cloudinit.md gap 3: the sandbox image is ~1 GB and today gets
# pulled implicitly by the FIRST ``docker run`` a client's command
# triggers, bounded at only 120s — too short for a real cold pull on
# anything but a fast link, which is exactly the "проверить сразу и
# заранее" behavior this action exists to provide instead. 600s (10
# minutes) is a deliberate, wider ceiling chosen for a proactive/manual
# pull specifically: comfortable for ~1 GB even on a modest VPS uplink,
# while still a genuine kill switch — a pull that hasn't finished in 10
# minutes on a supposedly-provisioned client VM is a real network problem
# worth reporting honestly, not something to wait out indefinitely.
_SANDBOX_IMAGE_PULL_TIMEOUT = 600.0


def _run_sandbox_docker_subprocess(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The one real OS boundary ``_fix_sandbox_image`` crosses — its own
    function so tests can fake it instead of actually pulling a ~1 GB
    image over the network."""
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )


def _sandbox_image_name() -> str:
    """The sandbox image name, from the CLIENT'S OWN config
    (``terminal.docker_image``) — never a literal, per the brief: a client
    who (through a mechanism outside this module's scope) ends up on a
    different image must have THAT image checked and pulled, not whatever
    string happened to be hardcoded here at write time.
    """
    from hermes_cli.config import load_config
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    cfg = load_config() or {}
    terminal_cfg = cfg.get("terminal")
    image = ""
    if isinstance(terminal_cfg, dict):
        image = (terminal_cfg.get("docker_image") or "").strip()
    if image:
        return image
    # load_config() deep-merges DEFAULT_CONFIG, so this fallback is only
    # defense in depth against a malformed/partial config on disk — not
    # the normal path.
    return DEFAULT_CONFIG["terminal"]["docker_image"]


def _sandbox_image_present(docker_exe: str, image: str) -> bool:
    try:
        proc = _run_sandbox_docker_subprocess(
            [docker_exe, "image", "inspect", image], _SANDBOX_IMAGE_INSPECT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _fix_sandbox_image() -> dict:
    """Spec fix 13: дотянуть образ песочницы и пересоздать контейнер.

    Idempotent by design (brief's own requirement): checks
    ``docker image inspect`` BEFORE pulling anything, and reports "already
    there, did nothing" rather than re-pulling — matching every other
    idempotent check/fix pairing in this module (e.g. a healthy gateway is
    never restarted).

    "Пересоздать контейнер" does not mean writing new container-lifecycle
    logic here: ``tools/environments/docker.py``'s ``reap_orphan_containers``
    already exists for exactly this shape of cleanup (removes only
    ``status=exited`` hermes-tagged containers — a RUNNING container,
    which might belong to a sibling in-flight command, is never touched by
    its own contract). Called with ``max_age_seconds=0`` so any stale
    exited container left over from before the image existed is dropped
    immediately; the next command a client sends then creates a fresh
    container against the image this action just pulled, with zero new
    container-management code written here.

    No ``profile_filter`` is passed: this VM's whole point is one client's
    one sandbox, and the support pass itself runs under its own profile
    (module docstring, spec's "Отдельный профиль ведёт свою базу") — filtering
    by ITS profile name would filter out exactly the client-facing
    containers this action exists to unblock. Reaping every profile's
    stale exited containers on this machine is the correct scope here.
    """
    from tools.environments.docker import find_docker, reap_orphan_containers

    docker_exe = find_docker()
    if not docker_exe:
        return {
            "ok": False,
            "error": "Docker не найден — без него загружать образ песочницы некуда.",
        }

    image = _sandbox_image_name()

    if _sandbox_image_present(docker_exe, image):
        return {"ok": True, "already": True, "image": image, "message": "Образ песочницы уже на месте."}

    try:
        proc = _run_sandbox_docker_subprocess(
            [docker_exe, "pull", image], _SANDBOX_IMAGE_PULL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Загрузка образа не уложилась в {_SANDBOX_IMAGE_PULL_TIMEOUT:g} с."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"Не удалось запустить docker pull: {exc}"}

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": tail[-500:] if tail else "docker pull завершился с ошибкой."}

    # Independent recheck, same pattern as every other fix in this module —
    # never trust the subprocess's own exit code alone.
    if not _sandbox_image_present(docker_exe, image):
        return {"ok": False, "error": "После docker pull образ всё равно не найден локально."}

    reap_orphan_containers(max_age_seconds=0)

    return {"ok": True, "already": False, "image": image, "message": "Образ песочницы загружен."}


# Docker-prune bound (120s) comes from trix_disk.docker_prune's own
# subprocess.run(timeout=120) call; file removal itself is local
# filesystem work with no network I/O (trix_disk's own docstring: "уборка
# идёт секунды"). 180s gives that local work a comfortable margin on top
# of the docker-prune bound without inventing a second, redundant timeout
# mechanism around a call that already enforces its own.
_DISK_CLEANUP_TIMEOUT = 180.0


def _fix_disk_cleanup() -> dict:
    """Spec fix 15: убрать служебное с диска — mechanism from spec 10
    (``hermes_cli/trix_disk.py``), called here and NOT reimplemented, per
    the brief ("Механизм уже написан, ты его только вызываешь").

    Delegates entirely to ``trix_disk.clean(get_hermes_home(),
    docker_prune=trix_disk.docker_prune)`` — the SAME entry point
    ``gateway/slash_commands.py``'s own ``/disk clean`` handler calls. The
    client-file and workspace protections (``protected_paths`` /
    ``_touches_protected`` / ``_stays_inside_home`` / ``_refusal``, each
    re-checked immediately before every individual removal inside
    ``_clean_once``) are entirely that module's own, already covered by
    its own mutation-tested suite, and are neither re-derived nor weakened
    here — this handler adds zero removal logic of its own, only a call.

    ``docker_prune`` is passed explicitly (the default is ``None``, i.e.
    "skip Docker entirely") so a build-up of dangling images gets reclaimed
    too, via ``trix_disk``'s OWN safe prune invocation (``DOCKER_PRUNE_ARGV``
    — deliberately no ``-a``, so the sandbox image ``sandbox_image`` just
    spent a real network pull fetching is never evicted by this same pass).

    ``ok`` mirrors ``format_clean_result``'s own notion of success/failure
    (its docstring: a partial success that freed real space does not get
    the "escalate to the owner" framing that a total failure does) rather
    than inventing a second, different definition here: true when
    something was actually freed, OR nothing needed cleaning at all
    (no errors, nothing removed — an idempotent no-op, not a failure);
    false only when there WERE errors and NOTHING was freed by any of them.
    """
    from hermes_cli.trix_disk import clean, docker_prune, format_clean_result

    result = clean(get_hermes_home(), docker_prune=docker_prune)
    ok = result.freed_bytes > 0 or not result.errors
    return {
        "ok": ok,
        "freed_bytes": result.freed_bytes,
        "removed_labels": list(result.removed_labels),
        "errors": list(result.errors),
        "message": format_clean_result(result),
    }


# ---------------------------------------------------------------------------
# The closed registry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportAction:
    """One entry of the closed action list (module docstring).

    ``handler`` is a zero-argument callable bound at import time — nothing
    a client's free text or a future model tool call supplies can become a
    parameter to it, because ``__post_init__`` refuses to construct a
    ``SupportAction`` whose handler takes any parameter at all.
    ``handler is None`` exactly when ``implemented`` is False: the action
    is named in the closed list (so it is visible and cannot quietly grow
    a parallel mechanism later) but has no code behind it in this task.
    """

    action_id: str
    label_ru: str
    kind: Literal["check", "fix"]
    implemented: bool
    handler: Callable[[], dict] | None = None
    timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if self.implemented and self.handler is None:
            raise ValueError(f"{self.action_id}: implemented action needs a handler")
        if not self.implemented and self.handler is not None:
            raise ValueError(f"{self.action_id}: unimplemented action must not have a handler")
        if self.handler is not None:
            import inspect

            params = inspect.signature(self.handler).parameters
            if params:
                raise TypeError(
                    f"{self.action_id}: handler must take zero parameters, got "
                    f"{list(params)} — support actions never accept caller-supplied input"
                )


SUPPORT_ACTIONS: dict[str, SupportAction] = {
    action.action_id: action
    for action in (
        SupportAction(
            "telegram_token", "Проверка токена Телеграм-бота", "check", True,
            _check_telegram_token, 15.0,
        ),
        SupportAction(
            "provider_key", "Проверка ключа провайдера модели", "check", True,
            _check_provider_key, 15.0,
        ),
        SupportAction(
            "proxy_syntax", "Проверка формата прокси", "check", True,
            _check_proxy_syntax, 5.0,
        ),
        SupportAction(
            "network_proxy", "Проверка сети и прокси", "check", True,
            _check_network_proxy, 15.0,
        ),
        SupportAction(
            "sandbox", "Проверка песочницы для команд", "check", True,
            _check_sandbox, 10.0,
        ),
        SupportAction(
            "browser", "Проверка браузера", "check", True,
            _check_browser, 15.0,
        ),
        SupportAction(
            "search", "Проверка веб-поиска", "check", True,
            _check_search, 15.0,
        ),
        SupportAction(
            "wizard_service", "Проверка службы аварийного доступа", "check", True,
            _check_wizard_service, 30.0,
        ),
        SupportAction(
            "gateway_state", "Проверка состояния шлюза", "check", True,
            _check_gateway_state, 15.0,
        ),
        SupportAction(
            "doctor_no_fix", "Полная проверка без починки", "check", True,
            _check_doctor_no_fix, _DOCTOR_NO_FIX_TIMEOUT + 10.0,
        ),
        SupportAction(
            "doctor_fix", "Полная проверка с автоматической починкой", "fix", True,
            _fix_doctor, _DOCTOR_FIX_TIMEOUT + 10.0,
        ),
        SupportAction(
            "gateway_restart", "Перезапуск шлюза с ожиданием, что бот ожил", "fix", True,
            _fix_gateway_restart, _GATEWAY_RESTART_TIMEOUT,
        ),
        SupportAction(
            "ensure_tool",
            "Догрузить недостающий инструмент (например, браузер)",
            "fix", True,
            _fix_ensure_tool, _ENSURE_TOOL_TIMEOUT,
        ),
        SupportAction(
            "sandbox_image",
            "Дотянуть образ песочницы и пересоздать контейнер",
            "fix", True,
            _fix_sandbox_image, _SANDBOX_IMAGE_PULL_TIMEOUT + _SANDBOX_IMAGE_INSPECT_TIMEOUT * 2 + 30.0,
        ),
        # NOT a leftover placeholder — an explicit OWNER DECISION (2026-09-03):
        # config_edit will not be built. Rationale (owner's own words): a
        # client's config.yaml is usually fine — what actually breaks is
        # narrower things around it (connectivity, the sandbox image,
        # missing tools), so a config-editing fix would treat a rare case
        # while adding real risk (a key whitelist, value validation, an
        # irreversible write to the client's own file). Left in the closed
        # list with ``handler=None`` on purpose, per the module's own
        # "visible but not wired" convention for a not-yet-implemented
        # action — do NOT wire this up later without a fresh owner ruling
        # reopening it; this comment is the record that it was considered
        # and declined, not skipped.
        SupportAction(
            "config_edit",
            "Изменить значение в конфиге из белого списка ключей",
            "fix", False,
        ),
        SupportAction(
            "disk_cleanup",
            "Убрать служебные файлы с диска",
            "fix", True,
            _fix_disk_cleanup, _DISK_CLEANUP_TIMEOUT,
        ),
    )
}

# The checks, run in this order every pass. check_proxy_syntax runs before
# check_reachability on purpose — same reasoning as validate.py's own module
# docstring: a malformed proxy should be blamed on the proxy field, not
# misread as "the network/Telegram is unreachable". gateway_state runs
# right before doctor_no_fix, after every narrower check, since restarting
# the gateway (its own fix — see FIX_FOR_CHECK) is the most disruptive
# implemented action in this pass and should only ever be reached once
# everything cheaper has already been observed.
CHECK_ORDER: tuple[str, ...] = (
    "telegram_token",
    "provider_key",
    "proxy_syntax",
    "network_proxy",
    "sandbox",
    "browser",
    "search",
    "wizard_service",
    "gateway_state",
    "doctor_no_fix",
)

# check_id -> fix action_id: the only two pairs in the closed list where a
# failing check has an IMPLEMENTED fix that is its literal other half (see
# the module docstring for why the narrower checks have none yet, and for
# the future config_edit trigger this deliberately does not build ahead
# of a real caller). A check with no entry here is reported as-is on
# failure — "было плохо, не почини­ли" — never auto-repaired.
FIX_FOR_CHECK: dict[str, str] = {
    "doctor_no_fix": "doctor_fix",
    "gateway_state": "gateway_restart",
}


# ---------------------------------------------------------------------------
# Execution: one bounded, isolated action call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRunResult:
    """The outcome of running exactly one action once."""

    action_id: str
    ok: bool
    error: str | None
    detail: dict
    started_at: str
    finished_at: str
    duration_s: float


def _execute(action_id: str, fn: Callable[[], dict], timeout: float) -> ActionRunResult:
    """Run ``fn()`` isolated from every other action's failure or hang.

    A raised exception or a ``TimeoutError`` here becomes this action's own
    ``ok=False`` result — it never propagates out and never aborts the rest
    of the pass (acceptance criterion 2 in spirit: one action's blast
    radius is itself). The thread-pool bound is defense in depth for
    actions that already enforce their own real I/O timeout (every check
    except the two doctor actions — see the module docstring); it does
    NOT by itself kill a hung in-process call, which is exactly why the
    one call in this module without its own internal bound (doctor) goes
    through a real subprocess instead of relying on this wrapper alone.
    """
    started = _now_iso()
    t0 = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn)
        try:
            raw = future.result(timeout=timeout)
        except _FutureTimeoutError:
            raw = {"ok": False, "error": f"Действие не уложилось в отведённое время ({timeout:g} с)."}
        except Exception as exc:  # noqa: BLE001 — isolate any action's own failure
            raw = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        executor.shutdown(wait=False)
    finished = _now_iso()
    duration = time.monotonic() - t0

    if not isinstance(raw, dict):
        raw = {"ok": False, "error": f"Неожиданный тип результата: {type(raw).__name__}"}

    ok = bool(raw.get("ok"))
    error = None if ok else raw.get("error")
    return ActionRunResult(
        action_id=action_id,
        ok=ok,
        error=error,
        detail=raw,
        started_at=started,
        finished_at=finished,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# The pass: checks, fix-where-possible, recheck, structural verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckOutcome:
    """One check's result for this pass, plus its fix/recheck if attempted."""

    check_id: str
    initial: ActionRunResult
    fix: ActionRunResult | None
    recheck: ActionRunResult | None
    outcome: Literal["good", "fixed", "not_fixed"]


def _classify_check_outcome(
    initial: ActionRunResult,
    fix: ActionRunResult | None,
    recheck: ActionRunResult | None,
) -> Literal["good", "fixed", "not_fixed"]:
    """The three-way verdict the brief asks for.

    ``"fixed"`` requires ``recheck is not None and recheck.ok`` — never
    ``fix.ok`` alone. A fix reporting success is that fix's own opinion of
    itself; only a fresh run of the check afterward counts as proof. This
    is what makes "почини­ли" structurally impossible to report without an
    actual successful recheck.
    """
    if initial.ok:
        return "good"
    if recheck is not None and recheck.ok:
        return "fixed"
    return "not_fixed"


@dataclass(frozen=True)
class SupportPassResult:
    """The full structural result of one support pass.

    No standalone ``gateway_restart`` field: a restart is only ever the
    fix side of the ``gateway_state`` check now (module docstring,
    ``FIX_FOR_CHECK``), so its result — when one was even attempted — lives
    entirely inside that check's own ``CheckOutcome.fix``/``.recheck``.
    """

    run_id: str
    started_at: str
    finished_at: str
    checks: tuple[CheckOutcome, ...]
    ok: bool


def run_support_pass() -> SupportPassResult:
    """Run every check; for the two pairs named in ``FIX_FOR_CHECK``, apply
    the fix and recheck ONLY when the check actually failed; return the
    full structural result.

    A healthy machine never has ``gateway_restart`` (or ``doctor_fix``)
    invoked at all — see the module docstring for why the earlier
    unconditional restart was wrong and how this replaces it.
    """
    run_id = uuid.uuid4().hex
    started = _now_iso()
    checks: list[CheckOutcome] = []

    for check_id in CHECK_ORDER:
        action = SUPPORT_ACTIONS[check_id]
        initial = _execute(check_id, action.handler, action.timeout_s)  # type: ignore[arg-type]

        fix_result: ActionRunResult | None = None
        recheck_result: ActionRunResult | None = None
        fix_id = FIX_FOR_CHECK.get(check_id)
        if not initial.ok and fix_id is not None:
            fix_action = SUPPORT_ACTIONS[fix_id]
            fix_result = _execute(fix_id, fix_action.handler, fix_action.timeout_s)  # type: ignore[arg-type]
            # The recheck re-invokes the SAME check function, fresh — never
            # the fix's own reported success — so "почини­ли" can never be
            # reported without a genuinely independent re-observation.
            recheck_result = _execute(check_id, action.handler, action.timeout_s)  # type: ignore[arg-type]

        checks.append(
            CheckOutcome(
                check_id=check_id,
                initial=initial,
                fix=fix_result,
                recheck=recheck_result,
                outcome=_classify_check_outcome(initial, fix_result, recheck_result),
            )
        )

    finished = _now_iso()
    ok = all(c.outcome != "not_fixed" for c in checks)
    return SupportPassResult(
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        checks=tuple(checks),
        ok=ok,
    )


# ---------------------------------------------------------------------------
# Client-facing report — spec §3 "Клиенту — только результат".
# ---------------------------------------------------------------------------

_MSG_CLIENT_ALL_GOOD = "Проверка завершена: неполадок не найдено, бот должен отвечать как обычно."
_MSG_CLIENT_FIXED = "Проверка завершена: обнаруженная неполадка устранена, бот снова должен отвечать."
_MSG_CLIENT_NOT_FIXED = (
    "Проверка завершена: часть неполадок исправить самостоятельно не удалось. "
    f"Если бот всё ещё не отвечает, напишите в поддержку: {SUPPORT_ESCALATION_CONTACT}."
)


def build_client_report(result: SupportPassResult) -> str:
    """The only text a client ever sees for a support pass.

    No check id, no tool name, no log line, no step-by-step account of
    what ran — one of exactly three fixed Russian sentences, chosen only
    from the structural verdict. This is a hardline (owner ruling, spec
    §3), not a style choice: see the module docstring.
    """
    if result.ok:
        if any(c.outcome == "fixed" for c in result.checks):
            return _MSG_CLIENT_FIXED
        return _MSG_CLIENT_ALL_GOOD
    return _MSG_CLIENT_NOT_FIXED


# ---------------------------------------------------------------------------
# Internal report + feedback — spec §3 "Нам — полностью".
# ---------------------------------------------------------------------------


def _support_log_path() -> Path:
    return get_hermes_home() / "support" / "runs.jsonl"


def _action_result_to_dict(result: ActionRunResult) -> dict:
    return {
        "action_id": result.action_id,
        "ok": result.ok,
        "error": result.error,
        "detail": result.detail,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_s": result.duration_s,
    }


def write_internal_report(result: SupportPassResult) -> str:
    """Append the full pass — every check, every raw detail, every internal
    error string — to our own log under ``HERMES_HOME``. Never trimmed for
    a client; this file is never shown to one. Returns ``result.run_id`` so
    the caller can later correlate a "помогло?" answer with this exact run
    via :func:`record_feedback`.
    """
    path = _support_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "run",
        "run_id": result.run_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "ok": result.ok,
        "checks": [
            {
                "check_id": c.check_id,
                "outcome": c.outcome,
                "initial": _action_result_to_dict(c.initial),
                "fix": _action_result_to_dict(c.fix) if c.fix is not None else None,
                "recheck": _action_result_to_dict(c.recheck) if c.recheck is not None else None,
            }
            for c in result.checks
        ],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return result.run_id


def record_feedback(run_id: str, helped: bool, note: str | None = None) -> None:
    """Append the client's "помогло?" answer to the same log, correlated by
    ``run_id``. This is the only telemetry channel the product has — no
    monitoring exists otherwise — so the only job here is: never lose the
    answer.
    """
    path = _support_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "feedback",
        "run_id": run_id,
        "helped": bool(helped),
        "note": note,
        "recorded_at": _now_iso(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
