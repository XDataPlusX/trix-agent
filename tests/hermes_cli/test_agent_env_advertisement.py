"""Tests for the AI_AGENT / HERMES_AGENT harness-attribution env vars.

Port of earendil-works/pi#7493: entry points advertise the agent harness to
child processes via the cross-agent ``AI_AGENT`` standard plus a
Hermes-specific marker, without clobbering an outer harness.

The AI_AGENT value must equal Hermes' id in the public agent-harness
registry (``hermes-agent`` in huggingface.js ``agent-harnesses.ts``) —
standard-var matching there is exact, so any other value is attributed to
"unknown".

Upstream ALSO re-exported both vars inside every wrapped shell command
(``BaseEnvironment._wrap_command``) so the marker would reach REMOTE
backends, whose exec environment does not inherit the Hermes process env.
Спека 18 removed that re-export: in a sandbox it puts the literal string
``hermes-agent`` where the model reads it with a bare ``env`` and concludes
it is Hermes. The host-process advertisement below is unchanged, so the
local backend still gets both vars by ordinary inheritance.
"""

import os
import subprocess

from hermes_cli.main import _advertise_agent_env

# Registry id — must stay in sync with huggingface.js agent-harnesses.ts.
HARNESS_ID = "hermes-agent"


class TestAdvertiseAgentEnv:
    def test_sets_both_vars_when_unset(self, monkeypatch):
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("HERMES_AGENT", raising=False)
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == HARNESS_ID
        assert os.environ["HERMES_AGENT"] == "true"

    def test_does_not_clobber_outer_harness(self, monkeypatch):
        monkeypatch.setenv("AI_AGENT", "pi")
        monkeypatch.delenv("HERMES_AGENT", raising=False)
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == "pi"
        assert os.environ["HERMES_AGENT"] == "true"

    def test_idempotent(self, monkeypatch):
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("HERMES_AGENT", raising=False)
        _advertise_agent_env()
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == HARNESS_ID
        assert os.environ["HERMES_AGENT"] == "true"


class TestWrapCommandDoesNotLeakHarnessIntoSandbox:
    """Спека 18: the wrapped command must not carry the upstream name.

    Found by a live scan inside the sandbox on VM 31.29.151.3 AFTER the
    base rename: ``ls -a /root`` was clean, but ``env`` still answered
    ``AI_AGENT=hermes-agent`` / ``HERMES_AGENT=true``. Asserts over the
    string the function actually builds — never over source text.
    """

    def _wrap(self, command: str) -> str:
        from tools.environments.local import LocalEnvironment

        env = LocalEnvironment.__new__(LocalEnvironment)
        env._snapshot_ready = False
        env._session_id = "testsession0"
        env._cwd_marker = "__TRIX_CWD_testsession0__"
        env._snapshot_path = "/tmp/trix-snap-testsession0.sh"
        env._snapshot_passthrough_names = set()
        return env._wrap_command(command, "/tmp")

    def test_wrapped_command_does_not_export_harness_vars(self):
        wrapped = self._wrap("true")
        assert "AI_AGENT=" not in wrapped
        assert "HERMES_AGENT=" not in wrapped

    def test_wrapped_command_carries_no_upstream_name_at_all(self):
        """Не только эти две переменные — вообще ни одного упоминания."""
        wrapped = self._wrap("echo payload-sentinel")
        assert HARNESS_ID not in wrapped
        assert "hermes" not in wrapped.lower()
        assert "payload-sentinel" in wrapped

    def test_sandbox_shell_sees_no_harness_vars(self):
        """Прогон через настоящий bash: переменных нет в среде команды."""
        wrapped = self._wrap('echo "AI=[$AI_AGENT] HERMES=[$HERMES_AGENT]"')
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("AI_AGENT", "HERMES_AGENT")}
        out = subprocess.run(
            ["bash", "-c", wrapped], capture_output=True, text=True,
            env=clean_env, timeout=30,
        )
        assert "AI=[] HERMES=[]" in out.stdout, out.stdout

    def test_outer_harness_value_still_reaches_the_command(self):
        """Экспорт убран, но унаследованное значение не теряется."""
        wrapped = self._wrap('echo "AI=[$AI_AGENT]"')
        env = dict(os.environ)
        env["AI_AGENT"] = "outer-harness"
        out = subprocess.run(
            ["bash", "-c", wrapped], capture_output=True, text=True,
            env=env, timeout=30,
        )
        assert "AI=[outer-harness]" in out.stdout, out.stdout
