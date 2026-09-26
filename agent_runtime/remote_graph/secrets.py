"""Deployment-key encryption; absent/bad keys disable only credential use."""
from cryptography.fernet import Fernet, InvalidToken

from agent_runtime.execution_store import ControlError


class SecretBox:
    def __init__(self, key: str):
        try:
            self._cipher = Fernet(key.encode('ascii')) if key else None
        except (ValueError, UnicodeError):
            self._cipher = None

    def _require_cipher(self) -> Fernet:
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
