"""Public DTOs are deliberately separate from resolved credentials."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=120)
    uri: str = Field(min_length=1, max_length=2048)
    database: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr | None = None


class PublicConnection(BaseModel):
    connection_id: str
    revision: int
    name: str
    kind: Literal['neo4j'] = 'neo4j'
    uri: str
    database: str
    username: str
    has_password: bool


class RemoteBinding(BaseModel):
    connection_id: str
    revision: int
    name: str
    kind: Literal['neo4j'] = 'neo4j'
    database: str


class ResolvedConnection(BaseModel):
    public: PublicConnection
    password: SecretStr = Field(exclude=True, repr=False)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=65536)
    parameters: dict[str, Any] = Field(default_factory=dict)
    save_result: bool = False


class QueryResult(BaseModel):
    success: bool
    query_id: str
    source: RemoteBinding
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    returned_rows: int = 0
    truncated: bool = False
    preview_truncated: bool = False
    elapsed_ms: int = 0
    artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
