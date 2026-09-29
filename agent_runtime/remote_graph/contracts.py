"""Public DTOs are deliberately separate from resolved credentials."""
from typing import Any, Literal

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


RemoteGraphKind = Literal['neo4j', 'falkordb']


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=120)
    kind: RemoteGraphKind = 'neo4j'
    uri: str = Field(min_length=1, max_length=2048)
    database: str = Field(min_length=1, max_length=256)
    username: str = Field(default='', max_length=256)
    password: SecretStr | None = None

    @field_validator('uri')
    @classmethod
    def valid_uri(cls, value, info):
        try:
            uri = urlsplit(value)
            schemes = ({'redis', 'rediss'} if info.data.get('kind') == 'falkordb'
                       else {'bolt', 'bolt+s', 'neo4j', 'neo4j+s'})
            valid = (uri.scheme in schemes
                     and uri.hostname and not uri.username and not uri.password
                     and '@' not in uri.netloc and not uri.path and not uri.query
                     and not uri.fragment and uri.port != 0
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Invalid remote graph URI')
        return value

    @model_validator(mode='after')
    def valid_username(self):
        if self.kind == 'neo4j' and not self.username:
            raise ValueError('Neo4j username is required')
        return self


class PublicConnection(BaseModel):
    connection_id: str
    revision: int
    name: str
    kind: RemoteGraphKind = 'neo4j'
    uri: str
    database: str
    username: str
    has_password: bool


class RemoteBinding(BaseModel):
    connection_id: str
    revision: int
    name: str
    kind: RemoteGraphKind = 'neo4j'
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
    artifact: dict[str, Any] | None = None
    export_complete: bool = False
    delivery_error: str | None = None
    consistency: str | None = None
    error_code: str | None = None
    error_message: str | None = None
