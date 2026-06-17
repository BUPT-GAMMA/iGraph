from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def _default_config_path() -> Path:
    home = (
        os.getenv("IGRAPH_HOME", "").strip()
        or os.getenv("GRAPHAGENT_HOME", "").strip()
    )
    if home:
        return Path(home).expanduser() / "config.yaml"
    return Path.home() / ".igraph" / "config.yaml"


_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@lru_cache(maxsize=2)
def _load_config_file(path: str) -> dict[str, str]:
    """Parse ``skills.config`` section from a minimal YAML config file.

    Only flat key-value pairs under ``skills.config:`` are supported.
    Nested dicts or lists are ignored.  Returns an empty dict on any error.

    Example config.yaml::

        skills:
          config:
            ml.default_epochs: 200
            ml.device: cuda
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    in_skills = False
    in_config = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            continue
        if indent == 0:
            in_skills = stripped.startswith("skills:")
            in_config = False
            continue
        if in_skills and indent == 2 and stripped.startswith("config:"):
            in_config = True
            continue
        if in_config and indent >= 4 and ":" in stripped:
            key, _, value = stripped.partition(":")
            k = key.strip()
            v = value.strip().strip("'\"")
            if k and v and not value.strip().startswith("{") and not value.strip().startswith("["):
                result[k] = v
    return result


def resolve_skill_config(
    config_keys: tuple[str, ...],
    config_path: str = "",
) -> dict[str, str]:
    """Look up each key in *config_keys* from the config file.

    Missing keys map to empty string so templates render cleanly even without
    a config file.

    Note: ``_load_config_file`` returns a cached dict — we only *read* it here
    via ``dict.get``, never mutate it, so the cache is safe.
    """
    if not config_keys:
        return {}
    path = config_path or str(_default_config_path())
    file_cfg = _load_config_file(path)
    # Build a new dict so callers cannot inadvertently mutate the LRU-cached object
    return {k: file_cfg.get(k, "") for k in config_keys}


def substitute_vars(body: str, config: dict[str, str]) -> str:
    """Replace ``${key}`` placeholders in *body* with values from *config*."""
    if not config or not body:
        return body

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return config.get(key, match.group(0))

    return _VAR_RE.sub(_replace, body)
