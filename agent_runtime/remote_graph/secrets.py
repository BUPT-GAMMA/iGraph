"""Deployment-key encryption; absent/bad keys disable only credential use.

An explicit key always wins and is never replaced, even when invalid, so
existing ciphertext is not silently orphaned. Without one, a key file in the
persistent data volume is created on first use and reused afterwards.
"""
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from agent_runtime.execution_store import ControlError


def _cipher(key: str) -> Fernet | None:
    try:
        return Fernet(key.encode('ascii')) if key else None
    except (ValueError, UnicodeError):
        return None


def load_or_create_key(path: Path) -> str:
    """Return the persisted key, creating it atomically for concurrent callers."""
    try:
        return path.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(Fernet.generate_key().decode('ascii'))
            output.flush()
            os.fsync(output.fileno())
        # link() never overwrites: a concurrent winner's key is kept and read.
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return path.read_text(encoding='ascii').strip()


class SecretBox:
    def __init__(self, key: str, key_file: Path | None = None):
        self._cipher = _cipher(key)
        self._key_file = None if key else key_file

    def _require_cipher(self) -> Fernet:
        if self._cipher is None and self._key_file is not None:
            try:
                self._cipher = _cipher(load_or_create_key(self._key_file))
            except (OSError, UnicodeError):
                self._cipher = None
        if self._cipher is None:
            raise ControlError('REMOTE_GRAPH_SECRET_UNAVAILABLE', 503)
        return self._cipher

    def encrypt(self, value: str) -> str:
        return self._require_cipher().encrypt(value.encode('utf-8')).decode('ascii')

    def decrypt(self, value: str) -> str:
        try:
            return self._require_cipher().decrypt(value.encode('ascii')).decode('utf-8')
        except (InvalidToken, ValueError, UnicodeError):
            raise ControlError('REMOTE_GRAPH_SECRET_UNAVAILABLE', 503) from None
