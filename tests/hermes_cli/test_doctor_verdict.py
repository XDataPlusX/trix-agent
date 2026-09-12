"""Behavioral contract tests for ``hermes_cli.trix_doctor_verdict``.

``hermes doctor`` always exited 0 — the only diagnostic result was human
text. These tests pin the CONTRACT the new ``--json``/``--exit-code``
flags must satisfy, not any specific current message or count:

- a verdict is "ok" exactly when nothing remains unresolved;
- ``run_doctor_with_verdict`` is a byte-for-byte passthrough (return
  ``None``, call the real doctor exactly once) when neither flag is set —
  this is the backward-compatibility guarantee the task requires;
- ``--json`` keeps stdout parseable (only the JSON verdict lands there)
  without discarding the human report (it still prints, on stderr).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.trix_doctor_verdict import (
    DoctorRunResult,
    doctor_exit_code,
    run_doctor_with_verdict,
    verdict_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDoctorRunResultContract:
    def test_ok_iff_nothing_remains(self):
        assert DoctorRunResult(issues=[], manual_issues=[], fixed_count=0).ok
        assert not DoctorRunResult(issues=["x"], manual_issues=[], fixed_count=0).ok
        assert not DoctorRunResult(issues=[], manual_issues=["y"], fixed_count=0).ok

    def test_remaining_issues_is_the_union_in_order(self):
        result = DoctorRunResult(issues=["a", "b"], manual_issues=["c"], fixed_count=3)
        assert result.remaining_issues == ["a", "b", "c"]

    def test_fixed_count_does_not_affect_ok(self):
        # Fixing something and having nothing left over are different
        # facts — a run that fixed 5 issues and has none remaining is
        # still "ok"; fixed_count alone must never flip the verdict.
        assert DoctorRunResult(issues=[], manual_issues=[], fixed_count=5).ok


class TestVerdictJson:
    def test_ok_result_serializes_as_ok(self):
        result = DoctorRunResult(issues=[], manual_issues=[], fixed_count=2)
        payload = json.loads(verdict_json(result))
        assert payload["ok"] is True
        assert payload["verdict"] == "ok"
        assert payload["remaining_issues"] == []
        assert payload["fixed_count"] == 2

    def test_unresolved_result_serializes_as_needs_attention(self):
        result = DoctorRunResult(issues=["broken thing"], manual_issues=["needs a human"], fixed_count=0)
        payload = json.loads(verdict_json(result))
        assert payload["ok"] is False
        assert payload["verdict"] == "needs_attention"
        assert set(payload["remaining_issues"]) == {"broken thing", "needs a human"}

    def test_output_is_valid_json_and_nothing_else(self):
        # A consumer does result = subprocess.run(...).stdout; json.loads(result).
        # Any leading/trailing prose breaks that.
        result = DoctorRunResult(issues=["x"], manual_issues=[], fixed_count=0)
        text = verdict_json(result)
        json.loads(text)  # must not raise


class TestDoctorExitCode:
    def test_zero_iff_ok(self):
        assert doctor_exit_code(DoctorRunResult(issues=[], manual_issues=[], fixed_count=0)) == 0

    def test_nonzero_when_anything_remains(self):
        assert doctor_exit_code(DoctorRunResult(issues=["x"], manual_issues=[], fixed_count=0)) != 0
        assert doctor_exit_code(DoctorRunResult(issues=[], manual_issues=["y"], fixed_count=0)) != 0


class TestRunDoctorWithVerdictBackwardCompat:
    """Without --json/--exit-code, this must be indistinguishable from the
    old ``cmd_doctor`` body: ``run_doctor(args)``, ignore the return,
    process exits 0."""

    def test_no_flags_calls_doctor_once_and_returns_none(self):
        calls = []

        def fake_run_doctor(args):
            calls.append(args)
            print("human report line")  # noqa: T201 — simulates real doctor output
            return DoctorRunResult(issues=["unresolved thing"], manual_issues=[], fixed_count=0)

        args = SimpleNamespace(fix=False, ack=None, json=False, exit_code=False)
        rc = run_doctor_with_verdict(fake_run_doctor, args)

        assert rc is None
        assert calls == [args]

    def test_missing_flag_attributes_default_to_old_behavior(self):
        """The pre-existing SimpleNamespace(fix=False, ack=None) shape
        (no .json/.exit_code at all) must not raise and must behave like
        the flags were off."""
        calls = []

        def fake_run_doctor(args):
            calls.append(args)
            return DoctorRunResult(issues=["unresolved thing"], manual_issues=[], fixed_count=0)

        args = SimpleNamespace(fix=False, ack=None)
        rc = run_doctor_with_verdict(fake_run_doctor, args)

        assert rc is None
        assert len(calls) == 1

    def test_no_flags_ignores_a_none_result_too(self):
        # Mirrors the --ack success path, which returns None.
        rc = run_doctor_with_verdict(lambda args: None, SimpleNamespace(json=False, exit_code=False))
        assert rc is None


class TestRunDoctorWithVerdictExitCode:
    def test_exit_code_flag_propagates_unresolved_state(self):
        def fake_run_doctor(args):
            return DoctorRunResult(issues=["still broken"], manual_issues=[], fixed_count=1)

        rc = run_doctor_with_verdict(fake_run_doctor, SimpleNamespace(json=False, exit_code=True))
        assert isinstance(rc, int) and rc != 0

    def test_exit_code_flag_is_zero_when_all_clear(self):
        def fake_run_doctor(args):
            return DoctorRunResult(issues=[], manual_issues=[], fixed_count=0)

        rc = run_doctor_with_verdict(fake_run_doctor, SimpleNamespace(json=False, exit_code=True))
        assert rc == 0

    def test_ack_style_none_result_does_not_force_nonzero(self):
        rc = run_doctor_with_verdict(lambda args: None, SimpleNamespace(json=False, exit_code=True))
        assert rc is None


class TestRunDoctorWithVerdictJson:
    def test_json_flag_keeps_stdout_parseable_and_moves_human_text_to_stderr(self, capsys):
        def fake_run_doctor(args):
            print("a human line an install script must not have to parse around")
            return DoctorRunResult(issues=["x"], manual_issues=[], fixed_count=0)

        run_doctor_with_verdict(fake_run_doctor, SimpleNamespace(json=True, exit_code=False))

        captured = capsys.readouterr()
        # stdout must be exactly the verdict JSON — nothing else.
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        # The human report was not discarded — it went to stderr instead.
        assert "human line" in captured.err

    def test_json_alone_does_not_force_a_nonzero_return(self):
        def fake_run_doctor(args):
            return DoctorRunResult(issues=["x"], manual_issues=[], fixed_count=0)

        rc = run_doctor_with_verdict(fake_run_doctor, SimpleNamespace(json=True, exit_code=False))
        assert rc is None

    def test_json_and_exit_code_together(self, capsys):
        def fake_run_doctor(args):
            return DoctorRunResult(issues=[], manual_issues=["y"], fixed_count=4)

        rc = run_doctor_with_verdict(fake_run_doctor, SimpleNamespace(json=True, exit_code=True))

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["fixed_count"] == 4
        assert payload["remaining_issues"] == ["y"]
        assert isinstance(rc, int) and rc != 0

    def test_json_with_none_result_prints_nothing_and_does_not_crash(self, capsys):
        rc = run_doctor_with_verdict(lambda args: None, SimpleNamespace(json=True, exit_code=True))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert rc is None


class TestDoctorCliRealPath:
    """Exercises the ACTUAL wire a user/script goes through: argparse ->
    `hermes_cli.subcommands.doctor.build_doctor_parser` -> `cmd_doctor` ->
    `hermes_cli.main`'s `rc = args.func(args); if isinstance(rc, int) and
    rc != 0: sys.exit(rc)` -> the real OS process exit code.

    Every other test in this file calls ``run_doctor_with_verdict`` (or
    ``cmd_doctor``) directly, in-process. That proves the helper's own
    logic is correct but proves nothing about whether ``cmd_doctor`` in
    ``hermes_cli/main.py`` is actually still wired to it — a revert of
    ``cmd_doctor`` back to the old ``run_doctor(args)`` body leaves every
    other test in this file green (they never touch ``cmd_doctor`` at
    all), while a real invocation of ``hermes doctor --exit-code`` would
    silently go back to always exiting 0. Real subprocess, real
    ``python -m hermes_cli.main`` entrypoint, real argument parsing --
    not a reimplementation of the dispatch and not a mock of any of it.

    A freshly created, empty ``HERMES_HOME`` is guaranteed to report at
    least one unresolved issue (no ``.env``, no API key configured --
    see the "Configuration Files" section of ``run_doctor()``) without
    ``--fix`` ever being passed, so ``ok`` is deterministically ``False``
    here. Mirrors the same fresh-HERMES_HOME assumption already relied on
    by ``tests/hermes_cli/test_doctor_env_seed.py``'s subprocess tests.
    """

    def _run(self, hermes_home: Path, *extra_args: str) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ)
        env["HERMES_HOME"] = str(hermes_home)
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "doctor", *extra_args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_plain_doctor_still_exits_zero(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        result = self._run(hermes_home)

        assert result.returncode == 0, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_exit_code_flag_makes_the_real_process_exit_nonzero(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        result = self._run(hermes_home, "--exit-code")

        assert result.returncode != 0, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_json_flag_puts_only_the_verdict_on_stdout(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        result = self._run(hermes_home, "--json")

        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["verdict"] == "needs_attention"
        assert payload["remaining_issues"]
        assert result.returncode == 0  # --json alone does not force a nonzero exit

    def test_json_and_exit_code_together_through_the_real_cli(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        result = self._run(hermes_home, "--json", "--exit-code")

        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Замечания к поставке не должны отправлять клиента в поддержку
# ---------------------------------------------------------------------------


class TestAdvisoriesNeverReachTheClientVerdict:
    """Найдено клиентом на живой машине 2026-09-04.

    Клиент прошёл настройку целиком, бот ответил — и в конце получил
    «Проверка завершена: часть неполадок исправить самостоятельно не
    удалось… напишите в поддержку». Единственным содержимым «неполадок»
    были три строки `npm audit` про сборочный инструментарий (esbuild/vite
    в рабочих областях `web` и `ui-tui`, которые клиенту вообще не
    поставляются как интерфейс).

    Цепочка была такая: `doctor` кладёт их в `issues` → `ok` становится
    False → `doctor_no_fix` в проходе поддержки падает → его починка
    (`doctor --fix`) не умеет чинить npm-советы → перепроверка падает →
    вердикт `not_fixed` → та самая фраза. И так на КАЖДОЙ машине, потому
    что советы приходят из пакетов, которые мы же и поставляем.
    """

    def _result(self, **kw):
        from hermes_cli.trix_doctor_verdict import DoctorRunResult

        return DoctorRunResult(**kw)

    def test_advisories_alone_leave_the_machine_ok(self):
        result = self._result(advisories=["web workspace has 4 npm vulnerabilities"])
        assert result.ok is True
        assert result.remaining_issues == []

    def test_advisories_do_not_flip_the_process_exit_code(self):
        from hermes_cli.trix_doctor_verdict import doctor_exit_code

        assert doctor_exit_code(self._result(advisories=["x has 1 npm vulnerability"])) == 0

    def test_a_real_issue_still_flips_everything(self):
        """Обратная сторона: «не считать советы» не должно превратиться в
        «не считать ничего»."""
        from hermes_cli.trix_doctor_verdict import doctor_exit_code

        result = self._result(
            issues=["Run 'hermes setup' to configure API keys"],
            advisories=["x has 1 npm vulnerability"],
        )
        assert result.ok is False
        assert doctor_exit_code(result) == 1

    def test_advisories_are_reported_not_hidden(self):
        """Вынести из вердикта — не то же самое, что спрятать: потребитель
        (страница поддержки, установщик) обязан их видеть."""
        import json

        from hermes_cli.trix_doctor_verdict import verdict_json

        payload = json.loads(verdict_json(self._result(advisories=["web workspace has 4 npm vulnerabilities"])))
        assert payload["advisories"] == ["web workspace has 4 npm vulnerabilities"]
        assert payload["verdict"] == "ok"

    def test_the_client_sentence_is_the_all_good_one(self):
        """Сквозная проверка того самого предложения, которое прочитал
        клиент, — от вердикта доктора до текста прохода поддержки."""
        from hermes_cli import trix_support

        doctor_payload = {"ok": True, "remaining_issues": [], "advisories": ["x has 1 npm vulnerability"]}

        def fake_doctor(*, fix, timeout):
            return dict(doctor_payload)

        original = trix_support._run_doctor_cli
        trix_support._run_doctor_cli = fake_doctor
        try:
            assert trix_support._check_doctor_no_fix()["ok"] is True
        finally:
            trix_support._run_doctor_cli = original
