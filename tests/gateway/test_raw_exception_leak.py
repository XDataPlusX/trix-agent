"""Client-command-surface plan Task 8: raw exceptions must not reach the
client's chat.

Two call sites in the plan's own file list:

- ``gateway/slash_commands.py`` (``/setup``, ``_handle_setup_wizard_
  command``): a systemctl/subprocess exception used to be truncated to 200
  chars and dropped straight into the client-facing reply via
  ``t("trix.setup_wizard.failed", error=...)``. Covered by
  ``tests/gateway/test_setup_wizard_command.py`` (an existing, non-`_new_`
  file per the plan's own File list — extended in place rather than
  duplicated here).
- ``hermes_cli/model_switch.py`` (credential-resolution failure inside
  ``switch_model()``'s no-explicit-user-provider branch): the exception was
  interpolated into ``ModelSwitchResult.error_message`` with an f-string
  that never called ``t()`` at all, so it stayed English even under Russian
  UI language, and it isn't truncated the way ``/setup``'s used to be.

Both handlers are shared by the CLI and the gateway; the gateway is where a
non-technical client actually reads the reply (``gateway/slash_commands.py``
wraps ``ModelSwitchResult.error_message`` in
``t("gateway.model.error_prefix", error=...)`` for both the typed ``/model``
path and the interactive picker path), so this file drives the shared
pipeline function directly rather than re-testing gateway plumbing that
``tests/gateway/test_model_command_*.py`` already covers.
"""

from __future__ import annotations

from unittest.mock import patch


class TestModelSwitchCredentialFailureDoesNotLeakRawException:
    def _switch_with_failing_credentials(self, exc: Exception):
        import hermes_cli.runtime_provider as runtime_provider
        from hermes_cli.model_switch import switch_model

        # "anthropic" (not "openai"): bare "openai" is an ALIAS that routes
        # through the openrouter aggregator (hermes_cli.providers.ALIASES),
        # so it trips an earlier, unrelated guard ("routes through
        # OpenRouter, which has no credentials configured") before ever
        # reaching resolve_runtime_provider() -- which would make this test
        # pass for the wrong reason (never exercising the except branch
        # Task 8 fixed at all). "anthropic" is not in that alias table, so
        # it reaches the branch cleanly.
        with patch.object(runtime_provider, "resolve_runtime_provider", side_effect=exc):
            return switch_model(
                raw_input="claude-x",
                current_provider="anthropic",
                current_model="claude-3",
                explicit_provider="anthropic",
            )

    def test_result_is_a_failure_with_a_message(self):
        result = self._switch_with_failing_credentials(RuntimeError("boom"))
        assert result.success is False
        assert result.error_message

    def test_raw_exception_text_does_not_reach_error_message(self):
        distinctive = "keyring locked: /home/user/.local/share/keyrings/login.keyring"
        result = self._switch_with_failing_credentials(RuntimeError(distinctive))
        assert distinctive not in result.error_message
        assert "keyring locked" not in result.error_message
        assert "/home/user/.local/share/keyrings" not in result.error_message

    def test_exception_class_name_does_not_reach_error_message_either(self):
        """A stringified exception can also carry its class name / repr
        (e.g. via logging %r or a bare raise) -- not just .args[0]. Uses a
        custom exception type so this can't pass by accident of picking a
        builtin name the copy happens to also use."""

        class _WeirdKeyringBackendError(Exception):
            pass

        result = self._switch_with_failing_credentials(
            _WeirdKeyringBackendError("some backend-specific detail")
        )
        assert "_WeirdKeyringBackendError" not in result.error_message
        assert "WeirdKeyringBackendError" not in result.error_message
        assert "some backend-specific detail" not in result.error_message

    def test_error_message_still_names_which_provider_failed(self):
        """The reply degrades to something short and localized -- but it
        should still be actionable, not a generic 'something went wrong'
        that gives the client nothing to act on."""
        result = self._switch_with_failing_credentials(RuntimeError("boom"))
        assert "anthropic" in result.error_message.lower()

    def test_full_exception_is_logged_for_support(self, caplog):
        import logging

        distinctive = "keyring locked: /home/user/.local/share/keyrings/login.keyring"
        with caplog.at_level(logging.WARNING, logger="hermes_cli.model_switch"):
            self._switch_with_failing_credentials(RuntimeError(distinctive))

        assert any(distinctive in record.getMessage() for record in caplog.records), (
            "the untruncated exception must still reach the log -- support's "
            "only view into what actually failed"
        )
