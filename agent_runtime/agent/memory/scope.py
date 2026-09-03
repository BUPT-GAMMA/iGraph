from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryScope:
    """Identifies a memory namespace.

    For now the system only supports ``default_user``, but this dataclass
    makes future multi-user / multi-project expansion a type-safe no-op.
    """

    user_id: str = "default_user"
    workspace_id: str | None = None
    project_id: str | None = None
    dataset_id: str | None = None
    session_id: str | None = None
    graph_fingerprint: str | None = None


DEFAULT_SCOPE = MemoryScope()
