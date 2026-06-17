from __future__ import annotations

import os
from pathlib import Path

from backend.agent.memory.scope import DEFAULT_SCOPE, MemoryScope


def igraph_home() -> Path:
    root = (
        os.getenv("IGRAPH_HOME", "").strip()
        or os.getenv("GRAPHAGENT_HOME", "").strip()
    )
    if root:
        return Path(root).expanduser()
    return Path.home() / ".igraph"


class MemoryPathResolver:
    def __init__(self, root: str | Path | None = None) -> None:
        env_root = (
            os.getenv("IGRAPH_MEMORY_ROOT", "").strip()
            or os.getenv("GRAPHAGENT_MEMORY_ROOT", "").strip()
        )
        self._root = (
            Path(root or env_root).expanduser()
            if (root or env_root)
            else igraph_home() / "memory"
        )

    @property
    def root(self) -> Path:
        return self._root

    def scope_dir(self, scope: MemoryScope | None = None) -> Path:
        s = scope or DEFAULT_SCOPE
        workspace = s.workspace_id or "default_workspace"
        user = s.user_id or "default_user"
        return self._root / workspace / user

    def profile_path(self, scope: MemoryScope | None = None) -> Path:
        return self.scope_dir(scope) / "profile.json"

    def project_memory_path(self, scope: MemoryScope | None = None) -> Path:
        s = scope or DEFAULT_SCOPE
        project = s.project_id or "default_project"
        return self.scope_dir(s) / "projects" / project / "memory.json"

    def dataset_memory_path(self, scope: MemoryScope | None = None) -> Path:
        s = scope or DEFAULT_SCOPE
        dataset = s.dataset_id or s.graph_fingerprint or "default_dataset"
        return self.scope_dir(s) / "datasets" / dataset / "memory.json"

    def playbooks_path(self, scope: MemoryScope | None = None) -> Path:
        return self.scope_dir(scope) / "playbooks.json"

    def events_path(self, scope: MemoryScope | None = None) -> Path:
        return self.scope_dir(scope) / "events.jsonl"
