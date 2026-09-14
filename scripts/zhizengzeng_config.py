"""Read an existing Codex profile without copying credentials into commands.

This helper intentionally accepts only the statically quoted ``codex`` block
used by the server-side ``run_simple_eval.sh``.  It never evaluates shell text
and never logs the API key.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


_CODEX_BLOCK_START = re.compile(
    r'^\s*elif\s+\[\s*"\$AGENT"\s*=\s*"codex"\s*\]\s*;\s*then\s*$',
    re.MULTILINE,
)
_BLOCK_END = re.compile(r"^\s*(?:elif\b|else\b|fi\b)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ZhizengzengConfig:
    api_key: str
    base_url: str


def _quoted_assignment(block: str, name: str) -> str:
    pattern = re.compile(
        rf'''^\s*{re.escape(name)}\s*=\s*(?P<quote>["'])(?P<value>[^\r\n]*?)(?P=quote)\s*(?:#.*)?$''',
        re.MULTILINE,
    )
    match = pattern.search(block)
    if match is None or not match.group("value").strip():
        raise ValueError(f"Codex provider profile is missing {name}")
    return match.group("value").strip()


def load_codex_zhizengzeng_config(path: str | Path) -> ZhizengzengConfig:
    """Load the quoted API URL/key from an explicitly selected profile.

    Any endpoint must use HTTPS without embedded credentials. Deployments may
    additionally set ``IGRAPH_PROVIDER_ALLOWED_HOSTS`` to a comma-separated
    hostname allowlist. No provider URL or credential is compiled into the
    benchmark package.
    """

    profile_path = Path(path).expanduser().resolve()
    if not profile_path.is_file():
        raise FileNotFoundError(f"Codex provider profile does not exist: {profile_path}")
    if profile_path.stat().st_size > 1024 * 1024:
        raise ValueError("Codex provider profile is unexpectedly large")
    text = profile_path.read_text(encoding="utf-8")
    start = _CODEX_BLOCK_START.search(text)
    if start is None:
        raise ValueError("Codex provider profile has no static codex block")
    tail = text[start.end() :]
    end = _BLOCK_END.search(tail)
    block = tail[: end.start()] if end is not None else tail

    base_url = _quoted_assignment(block, "API_URL").rstrip("/")
    api_key = _quoted_assignment(block, "API_KEY")
    parsed = urlsplit(base_url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Codex provider profile does not contain a valid HTTPS endpoint")
    allowed_hosts = {
        value.strip().casefold().rstrip(".")
        for value in os.environ.get("IGRAPH_PROVIDER_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }
    if allowed_hosts and hostname.rstrip(".") not in allowed_hosts:
        raise ValueError("Codex provider hostname is not in IGRAPH_PROVIDER_ALLOWED_HOSTS")
    if any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise ValueError("Codex provider API key contains invalid characters")
    return ZhizengzengConfig(api_key=api_key, base_url=base_url)


__all__ = ["ZhizengzengConfig", "load_codex_zhizengzeng_config"]
