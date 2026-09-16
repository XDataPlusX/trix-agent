"""Yuanbao's cron-delivery header/footer stripping must be language-independent.

``cron/scheduler.py`` wraps a job's output in a localized header/footer
(``gateway.platforms.base.format_cron_wrap``) before delivery, and
``MessageSender.strip_cron_wrapper`` (this module's target) removes it again
for Yuanbao's cleaner platform-native rendering.

Regression coverage for the bug where the wrapper was hardcoded to English
literals: once the wrapper text was localized (e.g. to Russian), the
hardcoded ``content.startswith("Cronjob Response: ")`` / footer-prefix match
silently stopped firing, so Yuanbao users got the raw wrapped text (English
header/footer glued around a Russian body) instead of the clean output.

``MessageSender.strip_cron_wrapper`` now delegates to
``gateway.platforms.base.strip_cron_wrap``, which recognizes the wrapper by
reading the *localized* template's static prefix (for the active/explicit
language, plus the English baseline) rather than a hardcoded literal -- so it
keeps working no matter which language ``format_cron_wrap`` built the
message in.
"""
from gateway.platforms.base import format_cron_wrap
from gateway.platforms.yuanbao import MessageSender


RAW_OUTPUT = "☕ Good morning! Time for coffee."


def test_strips_english_wrapper():
    wrapped = format_cron_wrap("coffee-reminder", "23bb0683365e", RAW_OUTPUT, lang="en")
    assert MessageSender.strip_cron_wrapper(wrapped) == RAW_OUTPUT


def test_strips_russian_wrapper():
    """The exact bug report: a Russian-wrapped message must still be
    recognized and stripped, not just the English one."""
    wrapped = format_cron_wrap("coffee-reminder", "23bb0683365e", RAW_OUTPUT, lang="ru")
    assert wrapped != RAW_OUTPUT
    assert "Плановая задача" in wrapped  # "Плановая задача" sanity check the fixture is actually localized
    assert MessageSender.strip_cron_wrapper(wrapped) == RAW_OUTPUT


def test_plain_content_is_untouched():
    """Content that never went through format_cron_wrap must pass straight
    through -- no false-positive stripping of ordinary agent output."""
    plain = "Just a normal reply, no cron wrapper here."
    assert MessageSender.strip_cron_wrapper(plain) == plain


def test_content_missing_divider_is_untouched():
    """A header-looking line with no divider/footer must not be mistaken
    for a wrapped delivery (guards against over-eager prefix matching)."""
    almost = "📅 Плановая задача: coffee-reminder\nJust some unrelated text, no divider at all."
    assert MessageSender.strip_cron_wrapper(almost) == almost
