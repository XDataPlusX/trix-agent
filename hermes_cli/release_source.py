"""Откуда этот продукт берёт свой код.

Единственный источник правды об адресе релизного репозитория и имени
релизной ветки. Всё, что устанавливает или обновляет продукт, импортирует
отсюда, а не хранит собственный литерал: раньше адрес был размазан по
шести файлам, и любой из них мог тихо вернуться к upstream при мёрже.

Инвариант, который нельзя потерять: remote ``upstream`` ->
NousResearch/hermes-agent существует только в рабочем репозитории
XDataPlus. На клиентской машине его нет, и ни один путь кода его не
создаёт. См. спеку установки и обновления, раздел «Инвариант».
"""

RELEASE_REPO_OWNER = "xdataplusx"
RELEASE_REPO_NAME = "trix-agent"

RELEASE_REPO_HTTPS = (
    f"https://github.com/{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}.git"
)
RELEASE_REPO_SSH = f"git@github.com:{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}.git"

# Единственная ветка, которую видит клиент. Установка клонирует её,
# обновление тянет её, другие ветки недостижимы штатным путём.
RELEASE_BRANCH = "release"

RELEASE_TAG_PREFIX = "trix-v"

RELEASE_ARCHIVE_URL = (
    f"https://github.com/{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}"
    f"/archive/refs/heads/{RELEASE_BRANCH}.zip"
)
RELEASE_TAG_URL_BASE = (
    f"https://github.com/{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}/releases/tag"
)

# Raw-content base for files fetched straight off the release branch --
# no docs site, no API, no auth, just the file as committed. Anything that
# used to point at Nous's hosted docs mirror or the upstream
# raw.githubusercontent.com/NousResearch/... address derives its
# replacement from this, rather than a fresh literal. First consumer: the
# model catalog manifest at ``assets/api/model-catalog.json``
# (hermes_cli/model_catalog.py).
RELEASE_RAW_BASE_URL = (
    f"https://raw.githubusercontent.com/{RELEASE_REPO_OWNER}/{RELEASE_REPO_NAME}"
    f"/{RELEASE_BRANCH}"
)


def canonical_remote(url: str | None) -> str:
    """Свести remote-URL к ``host/owner/repo`` в нижнем регистре.

    git хранит один и тот же репозиторий в нескольких формах (ssh,
    https, с ``.git`` и без, с хвостовым слэшем). Сравнение сырых строк
    даёт ложное «это чужой репозиторий» на ровном месте.
    """
    if not url:
        return ""
    value = url.strip().lower()
    if value.startswith("git@github.com:"):
        value = "github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@"):
        value = value[len("ssh://git@"):]
    else:
        for prefix in ("https://", "http://"):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    return value


_RELEASE_CANONICAL = canonical_remote(RELEASE_REPO_HTTPS)


def is_release_remote(url: str | None) -> bool:
    """True, если URL указывает на релизный репозиторий XDataPlus."""
    return bool(url) and canonical_remote(url) == _RELEASE_CANONICAL


def resolve_local_release_tag(repo_dir: str, timeout: float = 3) -> str | None:
    """Return the nearest local git tag matching ``RELEASE_TAG_PREFIX*``, or None.

    stdlib-only (``subprocess`` only) on purpose: this is the single
    implementation shared by both ``hermes_cli.banner`` (full version label,
    with caching + release URL) and ``hermes_cli._startup_fast`` (the
    pre-import-wall ``--version`` path, which must stay off the heavy import
    wall). One implementation means the two can't drift the way the fast
    path's version string used to (see ``_startup_fast`` module docstring).

    Scoped to ``RELEASE_TAG_PREFIX`` so an upstream-style tag left over from
    the fork's shared git history (e.g. ``v2026.8.3``) is never reported as
    this product's own release. Returns None on any failure — not a git
    checkout, no matching tag, no ``git`` binary — so callers fall back to
    an honest "no known release" label instead of fabricating a version.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", f"{RELEASE_TAG_PREFIX}*"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(repo_dir),
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    tag = (result.stdout or "").strip()
    if not tag or not tag.startswith(RELEASE_TAG_PREFIX):
        return None
    return tag
