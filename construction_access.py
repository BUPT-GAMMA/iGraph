"""Wire contract for the BFF-owned construction dataset boundary."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCOPE_HEADER = "X-iGraph-Construction-Scope"
DATASET_TOOLS = frozenset({"get_graph_construction_status", "get_constructed_graph",
    "load_constructed_graph", "preview_graph_optimization", "commit_graph_optimization"})


def scope_headers(project_id: str | None) -> dict[str, str]:
    """Call only with an already verified project (None explicitly means personal)."""
    return {SCOPE_HEADER: f"project:{project_id}" if project_id else "personal"}


class AccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    scope: str | None = Field(default=None, min_length=1, max_length=256)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    cleanup: bool = False


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset_id: str
    source_path: str
    source_filename: str


class AccessGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str
    scope: str
    dataset_ids: list[str]
    source: SourceRecord | None = None
