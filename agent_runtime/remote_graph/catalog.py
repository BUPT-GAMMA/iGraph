"""Owner-scoped connection persistence using execution-store transactions."""
from uuid import uuid4

from sqlalchemy import Column, Integer, JSON, String, Table, Text, select, text

from agent_runtime.execution_store import ControlError, metadata
from .contracts import ConnectionWrite, PublicConnection, RemoteBinding, ResolvedConnection


connections = Table(
    'igraph_remote_graph_connections', metadata,
    Column('user_id', String, primary_key=True),
    Column('connection_id', String, primary_key=True),
    Column('revision', Integer, nullable=False),
    Column('name', String, nullable=False),
    Column('kind', String, nullable=False, server_default=text("'neo4j'")),
    Column('uri', String, nullable=False),
    Column('database', String, nullable=False),
    Column('username', String, nullable=False),
    Column('password_ciphertext', Text, nullable=False),
)

selections = Table(
    'igraph_remote_graph_selections', metadata,
    Column('user_id', String, primary_key=True),
    Column('session_id', String, primary_key=True),
    Column('binding', JSON, nullable=False),
)


class RemoteGraphCatalog:
    def __init__(self, store, secret_box):
        self.store, self.secret_box = store, secret_box

    def _row(self, conn, uid, connection_id):
        row = conn.execute(select(connections).where(
            connections.c.user_id == uid,
            connections.c.connection_id == connection_id,
        )).mappings().first()
        if row is None:
            raise ControlError('REMOTE_GRAPH_NOT_FOUND', 404)
        return row

    @staticmethod
    def _public(row) -> PublicConnection:
        return PublicConnection(**{key: row[key] for key in (
            'connection_id', 'revision', 'name', 'kind', 'uri', 'database', 'username'
        )}, has_password=bool(row['password_ciphertext']))

    @staticmethod
    def _revision(row, revision):
        if row['revision'] != revision:
            raise ControlError('REMOTE_GRAPH_CHANGED', 409)

    def list(self, uid) -> list[PublicConnection]:
        with self.store.transaction(uid, '__remote_graphs__') as conn:
            return [self._public(row) for row in conn.execute(
                select(connections).where(connections.c.user_id == uid)
                .order_by(connections.c.name, connections.c.connection_id)
            ).mappings()]

    def save(self, uid, body: ConnectionWrite) -> PublicConnection:
        with self.store.transaction(uid, '__remote_graphs__') as conn:
            old = self._row(conn, uid, body.connection_id) if body.connection_id else None
            if old is not None:
                self._revision(old, body.revision)
                if old['kind'] != body.kind:
                    raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
            password = body.password.get_secret_value() if body.password is not None else ''
            if not password and old is None:
                raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
            # Even a blank-password edit checks the deployment key before reuse.
            if not password:
                password = self.secret_box.decrypt(old['password_ciphertext'])
            row = dict(user_id=uid, connection_id=body.connection_id or str(uuid4()),
                       revision=old['revision'] + 1 if old is not None else 1,
                       name=body.name, kind=body.kind, uri=body.uri, database=body.database,
                       username=body.username,
                       password_ciphertext=self.secret_box.encrypt(password))
            if old is None:
                conn.execute(connections.insert().values(**row))
            else:
                changed = conn.execute(connections.update().where(
                    connections.c.user_id == uid,
                    connections.c.connection_id == body.connection_id,
                    connections.c.revision == body.revision,
                ).values(**row))
                if changed.rowcount != 1:
                    raise ControlError('REMOTE_GRAPH_CHANGED', 409)
            return self._public(row)

    def delete(self, uid, connection_id, revision) -> None:
        with self.store.transaction(uid, '__remote_graphs__') as conn:
            self._revision(self._row(conn, uid, connection_id), revision)
            deleted = conn.execute(connections.delete().where(
                connections.c.user_id == uid,
                connections.c.connection_id == connection_id,
                connections.c.revision == revision,
            ))
            if deleted.rowcount != 1:
                raise ControlError('REMOTE_GRAPH_CHANGED', 409)

    def resolve(self, uid, binding: RemoteBinding) -> ResolvedConnection:
        with self.store.transaction(uid, '__remote_graphs__') as conn:
            row = self._row(conn, uid, binding.connection_id)
            self._revision(row, binding.revision)
            if row['kind'] != binding.kind:
                raise ControlError('REMOTE_GRAPH_CHANGED', 409)
            return ResolvedConnection(public=self._public(row),
                                      password=self.secret_box.decrypt(row['password_ciphertext']))

    def resolve_write(self, uid, body: ConnectionWrite) -> ResolvedConnection:
        """Resolve an unsaved form without persisting it or disclosing its secret."""
        password = body.password.get_secret_value() if body.password else ''
        if body.connection_id:
            with self.store.transaction(uid, '__remote_graphs__') as conn:
                old = self._row(conn, uid, body.connection_id)
                self._revision(old, body.revision)
                if old['kind'] != body.kind:
                    raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
                if not password:
                    password = self.secret_box.decrypt(old['password_ciphertext'])
        if not password:
            raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
        return ResolvedConnection(public=PublicConnection(
            connection_id=body.connection_id or 'unsaved', revision=body.revision or 1,
            name=body.name, uri=body.uri, database=body.database, username=body.username,
            kind=body.kind, has_password=True), password=password)

    def selection_form(self, uid, connection_id) -> ConnectionWrite:
        with self.store.transaction(uid, '__remote_graphs__') as conn:
            public = self._public(self._row(conn, uid, connection_id))
            return ConnectionWrite(**public.model_dump(exclude={'has_password'}))

    def select(self, uid, sid, connection_id, *, revision=None) -> RemoteBinding | None:
        with self.store.transaction(uid, sid) as conn:
            binding = None
            if connection_id is not None:
                row = self._row(conn, uid, connection_id)
                if revision is not None:
                    self._revision(row, revision)
                binding = RemoteBinding(**self._public(row).model_dump())
            condition = (selections.c.user_id == uid, selections.c.session_id == sid)
            conn.execute(selections.delete().where(*condition))
            if binding:
                conn.execute(selections.insert().values(user_id=uid, session_id=sid,
                                                        binding=binding.model_dump()))
            return binding

    def freeze(self, conn, uid, sid) -> RemoteBinding | None:
        condition = (selections.c.user_id == uid, selections.c.session_id == sid)
        saved = conn.execute(select(selections.c.binding).where(*condition)).scalar_one_or_none()
        if saved is None:
            return None
        try:
            current = RemoteBinding(**self._public(self._row(conn, uid, saved['connection_id'])).model_dump())
        except ControlError as exc:
            if exc.code != 'REMOTE_GRAPH_NOT_FOUND':
                raise
            return RemoteBinding(**saved)
        conn.execute(selections.update().where(*condition).values(binding=current.model_dump()))
        return current

    def selection(self, uid, sid) -> dict:
        with self.store.transaction(uid, sid) as conn:
            binding = self.freeze(conn, uid, sid)
            reason = None
            if binding:
                try:
                    self._row(conn, uid, binding.connection_id)
                except ControlError as exc:
                    reason = exc.code
            return {'remote_graph': binding.model_dump() if binding else None,
                    'remote_graph_unavailable_reason': reason}
