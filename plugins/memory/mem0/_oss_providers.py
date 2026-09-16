"""OSS provider definitions for LLM, embedder, and vector store."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Локальная база Qdrant — состояние ОДНОГО профиля, и место ей внутри его
# корня. Раньше путь был зашит строкой ``~/.hermes/mem0_qdrant``, вычислен
# на импорте и потому не знал ни про ``HERMES_HOME``, ни про containment:
# в Docker-развёртывании база молча уезжала из смонтированного тома, а два
# contained-профиля на одной машине делили одно хранилище.
_QDRANT_DIR_NAME = "mem0_qdrant"


def qdrant_default_path() -> Path:
    """Каталог локальной базы Qdrant для ТЕКУЩЕГО профиля.

    Функция, а не константа: корень профиля известен в момент вызова, а не
    в момент импорта модуля.
    """
    try:
        from hermes_constants import get_profile_root

        return get_profile_root() / _QDRANT_DIR_NAME
    except Exception:
        # Последний рубеж, если движок недоступен как библиотека.
        return Path(os.path.expanduser("~/.hermes")) / _QDRANT_DIR_NAME


def vector_default_config(provider_id: str) -> dict[str, Any]:
    """Дефолтная конфигурация векторного хранилища, посчитанная сейчас.

    Единственная точка, через которую эти дефолты положено брать: путь к
    базе зависит от профиля и не может жить в таблице-константе.
    """
    config = dict(VECTOR_PROVIDERS[provider_id].get("default_config") or {})
    if provider_id == "qdrant":
        config["path"] = str(qdrant_default_path())
    return config


LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "needs_key": True,
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-5-mini",
        "base_url_key": "openai_base_url",
    },
    "ollama": {
        "label": "Ollama (local)",
        "needs_key": False,
        "default_model": "llama3.1:8b",
        "default_url": "http://localhost:11434",
        "base_url_key": "ollama_base_url",
        "pip_dep": "ollama",
    },
}

EMBEDDER_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "needs_key": True,
        "env_var": "OPENAI_API_KEY",
        "default_model": "text-embedding-3-small",
        "base_url_key": "openai_base_url",
        "dims": 1536,
    },
    "ollama": {
        "label": "Ollama (local)",
        "needs_key": False,
        "default_model": "nomic-embed-text",
        "default_url": "http://localhost:11434",
        "base_url_key": "ollama_base_url",
        "dims": 768,
        "pip_dep": "ollama",
    },
}

VECTOR_PROVIDERS: dict[str, dict[str, Any]] = {
    "qdrant": {
        "label": "Qdrant",
        # Путь сюда НЕ кладётся: он зависит от профиля и считается в
        # :func:`vector_default_config`. Строка в таблице была бы четвёртым
        # местом, знающим, где живут данные, и единственным неверным.
        "default_config": {},
        "pip_dep": "qdrant-client",
    },
    "pgvector": {
        "label": "PGVector",
        "default_config": {"host": "localhost", "port": 5432, "user": os.getenv("USER", "postgres"), "dbname": "postgres"},
        "pip_dep": "psycopg2-binary",
    },
}

KNOWN_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
}


def validate_oss_config(oss_config: dict) -> list[str]:
    """Validate an OSS config dict. Returns list of error strings (empty = valid)."""
    errors: list[str] = []

    for section, registry in [("llm", LLM_PROVIDERS), ("embedder", EMBEDDER_PROVIDERS),
                               ("vector_store", VECTOR_PROVIDERS)]:
        block = oss_config.get(section)
        if not block or not isinstance(block, dict):
            errors.append(f"Missing required section: {section}")
            continue
        provider_id = block.get("provider", "")
        if provider_id not in registry:
            valid = ", ".join(registry.keys())
            errors.append(f"Unknown {section} provider '{provider_id}'. Valid: {valid}")

    vs = oss_config.get("vector_store", {})
    if vs.get("provider") == "pgvector":
        cfg = vs.get("config", {})
        if not cfg.get("user"):
            errors.append("PGVector requires 'user' in vector_store.config")

    return errors
