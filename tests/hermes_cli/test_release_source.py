"""Адрес источника кода — одно значение, а не литерал в шести файлах."""

import pytest

from hermes_cli.release_source import (
    RELEASE_ARCHIVE_URL,
    RELEASE_BRANCH,
    RELEASE_REPO_HTTPS,
    RELEASE_REPO_SSH,
    RELEASE_TAG_URL_BASE,
    canonical_remote,
    is_release_remote,
)


def test_every_url_points_at_the_same_repository():
    """Инвариант: все производные URL описывают один и тот же репозиторий."""
    canonical = canonical_remote(RELEASE_REPO_HTTPS)
    assert canonical
    assert canonical_remote(RELEASE_REPO_SSH) == canonical
    assert canonical in RELEASE_ARCHIVE_URL.replace("https://", "")
    assert canonical in RELEASE_TAG_URL_BASE.replace("https://", "")


def test_archive_url_targets_the_release_branch():
    assert RELEASE_ARCHIVE_URL.endswith(f"/{RELEASE_BRANCH}.zip")


def test_no_url_mentions_upstream():
    """Ни один адрес не ведёт к репозиторию Nous Research."""
    for url in (
        RELEASE_REPO_HTTPS,
        RELEASE_REPO_SSH,
        RELEASE_ARCHIVE_URL,
        RELEASE_TAG_URL_BASE,
    ):
        assert "nousresearch" not in url.lower()


@pytest.mark.parametrize(
    "remote",
    [
        RELEASE_REPO_HTTPS,
        RELEASE_REPO_SSH,
        RELEASE_REPO_HTTPS.removesuffix(".git"),
        RELEASE_REPO_HTTPS.removesuffix(".git") + "/",
        RELEASE_REPO_SSH.removesuffix(".git"),
        "HTTPS://github.com/xdataplusx/trix-agent.git",
        "GIT@GITHUB.COM:xdataplusx/trix-agent.git",
    ],
)
def test_is_release_remote_accepts_every_equivalent_form(remote):
    """git пишет origin в нескольких формах — все они наш репозиторий."""
    assert is_release_remote(remote) is True


@pytest.mark.parametrize(
    "remote",
    [
        None,
        "",
        "https://github.com/NousResearch/hermes-agent.git",
        "git@github.com:NousResearch/hermes-agent.git",
        "https://github.com/someone/else.git",
        "HTTPS://github.com/NousResearch/hermes-agent.git",
        "GIT@GITHUB.COM:NousResearch/hermes-agent.git",
    ],
)
def test_is_release_remote_rejects_anything_else(remote):
    assert is_release_remote(remote) is False
