"""User-owned datasets, optionally scoped to a project."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ProjectDatasetRecord:
    dataset_id: str
    project_id: str | None
    user_id: str
    name: str
    original_filename: str
    storage_path: str
    file_type: str
    source_kind: str = "upload"
    source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "ready"
    created_at: float = 0.0
    updated_at: float = 0.0
    description: str = ""


class ProjectDatasetDirectory:
    """Atomic JSON metadata repository with project-scoped source storage."""

    def __init__(self, metadata_path: Path, storage_root: Path) -> None:
        self.metadata_path = metadata_path
        self.storage_root = storage_root
        self._lock = threading.RLock()

    @staticmethod
    def _safe_filename(filename: str) -> str:
        basename = Path(str(filename or "dataset.bin")).name.strip() or "dataset.bin"
        safe = _SAFE_FILENAME.sub("_", basename).strip("._")
        return safe[:180] or "dataset.bin"

    def _load(self) -> dict[str, ProjectDatasetRecord]:
        if not self.metadata_path.is_file():
            return {}
        payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        return {
            item["dataset_id"]: ProjectDatasetRecord(**item)
            for item in payload.get("datasets", [])
            if isinstance(item, dict)
            and item.get("dataset_id")
            and item.get("user_id")
        }


    def _write(self, records: dict[str, ProjectDatasetRecord]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "datasets": [asdict(records[key]) for key in sorted(records)],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix="project-datasets-",
            suffix=".json",
            dir=str(self.metadata_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.metadata_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def list(self, project_id: str | None, user_id: str) -> list[ProjectDatasetRecord]:
        rows = [
            item
            for item in self._load().values()
            if item.project_id == project_id and item.user_id == user_id
        ]
        return sorted(rows, key=lambda item: (item.created_at, item.dataset_id))

    def get(
        self,
        dataset_id: str,
        user_id: str,
        project_id: str | None = None,
    ) -> ProjectDatasetRecord | None:
        item = self._load().get(str(dataset_id or ""))
        if item is None or item.user_id != user_id:
            return None
        if item.project_id != project_id:
            return None
        return item

    def create(
        self,
        *,
        project_id: str | None,
        user_id: str,
        name: str,
        original_filename: str,
        content: bytes,
        description: str = "",
    ) -> ProjectDatasetRecord:
        description = self._description(description)
        with self._lock:
            records = self._load()
            dataset_id = str(uuid.uuid4())
            safe_name = self._safe_filename(original_filename)
            scope_dir = project_id or ("personal-" + uuid.uuid5(uuid.NAMESPACE_URL, user_id).hex)
            dataset_dir = self.storage_root / scope_dir / "datasets" / dataset_id
            dataset_dir.mkdir(parents=True, exist_ok=False)
            source_path = dataset_dir / safe_name
            fd, tmp_name = tempfile.mkstemp(prefix="source-", dir=str(dataset_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, source_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            now = time.time()
            record = ProjectDatasetRecord(
                dataset_id=dataset_id,
                project_id=project_id,
                user_id=user_id,
                name=name.strip() or Path(safe_name).stem or "Dataset",
                original_filename=Path(original_filename).name or safe_name,
                storage_path=str(source_path.resolve()),
                file_type=Path(safe_name).suffix.lower().lstrip(".") or "binary",
                source_kind="upload",
                status="ready",
                created_at=now,
                updated_at=now,
                description=description,
            )
            if record.file_type in {"sql", "csv", "txt"}:
                record.status = "source_ready"
                record.metadata = {"construction": {"status": "not_built", "revision": 0,
                    "source_type": record.file_type}, "dataset_kind": "construction_source"}
            records[dataset_id] = record
            try:
                self._write(records)
            except Exception:
                shutil.rmtree(dataset_dir, ignore_errors=True)
                raise
            return record

    def create_csv_bundle(
        self,
        *,
        project_id: str | None,
        user_id: str,
        name: str,
        original_filename: str,
        files: list[tuple[str, Any]],
        schema: dict[str, Any],
        description: str | None = None,
    ) -> ProjectDatasetRecord:
        """Persist several CSV files as one server-authored construction source.

        The archive is an internal transport format, while the public dataset
        remains a CSV construction source.  Only the retained manifest can name
        archive members, so build options never accept arbitrary server paths.
        """
        if not 1 <= len(files) <= 128:
            raise ValueError("CSV_BUNDLE_FILE_COUNT")
        if not isinstance(schema, dict) or schema.get("version") != "csv_bundle.v1":
            raise ValueError("CSV_BUNDLE_SCHEMA")
        description = self._description(schema.get("description", "") if description is None else description)
        definitions = schema.get("tables")
        if not isinstance(definitions, list) or len(definitions) != len(files):
            raise ValueError("CSV_BUNDLE_SCHEMA")

        uploaded: dict[str, tuple[str, Any]] = {}
        for filename, stream in files:
            clean = Path(str(filename or "")).name
            if clean != filename or Path(clean).suffix.lower() != ".csv":
                raise ValueError("CSV_BUNDLE_FILE_TYPE")
            key = clean.casefold()
            if key in uploaded:
                raise ValueError("CSV_BUNDLE_DUPLICATE_FILE")
            uploaded[key] = (clean, stream)

        normalized_tables: list[dict[str, Any]] = []
        seen_tables: set[str] = set()
        referenced_files: set[str] = set()
        for index, definition in enumerate(definitions):
            if not isinstance(definition, dict):
                raise ValueError("CSV_BUNDLE_SCHEMA")
            file_name = Path(str(definition.get("file_name") or "")).name
            file_key = file_name.casefold()
            if file_key not in uploaded or file_key in referenced_files:
                raise ValueError("CSV_BUNDLE_SCHEMA")
            table_name = str(definition.get("name") or "").strip()
            if not table_name or len(table_name) > 1024 or "\x00" in table_name:
                raise ValueError("CSV_BUNDLE_TABLE_NAME")
            if table_name in seen_tables:
                raise ValueError("CSV_BUNDLE_DUPLICATE_TABLE")
            seen_tables.add(table_name)
            referenced_files.add(file_key)
            primary_key = definition.get("primary_key", [])
            foreign_keys = definition.get("foreign_keys", [])
            columns = definition.get("columns", [])
            if (not isinstance(columns, list) or not all(isinstance(value, str) and value for value in columns)
                    or not isinstance(primary_key, list) or not all(isinstance(value, str) and value for value in primary_key)
                    or not isinstance(foreign_keys, list)):
                raise ValueError("CSV_BUNDLE_SCHEMA")
            safe_member = self._safe_filename(file_name)
            normalized_tables.append({
                "file_name": file_name,
                "entry": f"tables/{index:03d}-{safe_member}",
                "name": table_name,
                "columns": columns,
                "primary_key": primary_key,
                "foreign_keys": foreign_keys,
            })
        if referenced_files != set(uploaded):
            raise ValueError("CSV_BUNDLE_SCHEMA")

        with self._lock:
            records = self._load()
            dataset_id = str(uuid.uuid4())
            scope_dir = project_id or ("personal-" + uuid.uuid5(uuid.NAMESPACE_URL, user_id).hex)
            dataset_dir = self.storage_root / scope_dir / "datasets" / dataset_id
            dataset_dir.mkdir(parents=True, exist_ok=False)
            safe_name = self._safe_filename(original_filename)
            if Path(safe_name).suffix.lower() != ".csv":
                safe_name = (Path(safe_name).stem or "csv-bundle") + ".csv"
            source_path = dataset_dir / safe_name
            fd, tmp_name = tempfile.mkstemp(prefix="source-", dir=str(dataset_dir))
            os.close(fd)
            try:
                manifest = {"version": "csv_bundle.v1", "tables": normalized_tables}
                with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
                    for table in normalized_tables:
                        _, stream = uploaded[table["file_name"].casefold()]
                        if hasattr(stream, "seek"):
                            stream.seek(0)
                        with archive.open(table["entry"], "w", force_zip64=True) as target:
                            shutil.copyfileobj(stream, target, length=1024 * 1024)
                os.replace(tmp_name, source_path)
                now = time.time()
                record = ProjectDatasetRecord(
                    dataset_id=dataset_id,
                    project_id=project_id,
                    user_id=user_id,
                    name=name.strip() or Path(safe_name).stem or "CSV bundle",
                    original_filename=Path(original_filename).name or safe_name,
                    storage_path=str(source_path.resolve()),
                    file_type="csv",
                    status="source_ready",
                    created_at=now,
                    updated_at=now,
                    description=description,
                    metadata={
                        "construction": {"status": "not_built", "revision": 0, "source_type": "csv"},
                        "dataset_kind": "construction_source",
                        "csv_bundle": {
                            "version": "csv_bundle.v1",
                            "source_files": [table["file_name"] for table in normalized_tables],
                            "tables": [{key: table[key] for key in ("name", "columns", "primary_key", "foreign_keys")}
                                       for table in normalized_tables],
                            "total_bytes": source_path.stat().st_size,
                        },
                    },
                )
                records[dataset_id] = record
                self._write(records)
                return record
            except Exception:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                shutil.rmtree(dataset_dir, ignore_errors=True)
                raise

    def create_reference(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        original_filename: str,
        storage_path: Path,
        source_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ProjectDatasetRecord, bool]:
        """Register a server-owned source without copying or taking ownership."""

        with self._lock:
            records = self._load()
            for item in records.values():
                if (
                    item.project_id == project_id
                    and item.user_id == user_id
                    and item.source_kind == "server"
                    and item.source_id == source_id
                ):
                    return item, False
            dataset_id = str(uuid.uuid4())
            safe_name = self._safe_filename(original_filename)
            now = time.time()
            record = ProjectDatasetRecord(
                dataset_id=dataset_id,
                project_id=project_id,
                user_id=user_id,
                name=name.strip() or Path(safe_name).stem or "Dataset",
                original_filename=Path(original_filename).name or safe_name,
                storage_path=str(storage_path.resolve()),
                file_type=Path(safe_name).suffix.lower().lstrip(".") or "binary",
                source_kind="server",
                source_id=source_id,
                metadata=dict(metadata or {}),
                status="ready",
                created_at=now,
                updated_at=now,
            )
            records[dataset_id] = record
            self._write(records)
            return record, True

    def update(
        self,
        dataset_id: str,
        user_id: str,
        *,
        project_id: str | None = None,
        name: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> ProjectDatasetRecord | None:
        with self._lock:
            records = self._load()
            item = records.get(dataset_id)
            if item is None or item.user_id != user_id:
                return None
            if item.project_id != project_id:
                return None
            if name is not None:
                item.name = name.strip()
            if status is not None:
                item.status = status
            if metadata is not None:
                item.metadata = dict(metadata)
            if description is not None:
                item.description = self._description(description)
            item.updated_at = time.time()
            records[dataset_id] = item
            self._write(records)
            return item

    @staticmethod
    def _description(value: str) -> str:
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError("DATASET_DESCRIPTION_INVALID")
        return value.strip()

    def delete(self, dataset_id: str, user_id: str, project_id: str | None = None) -> bool:
        with self._lock:
            records = self._load()
            item = records.get(dataset_id)
            if item is None or item.user_id != user_id:
                return False
            if item.project_id != project_id:
                return False
            # Keep metadata until files are removed, so a failed cleanup can retry.
            if item.source_kind != "server":
                target = Path(item.storage_path).parent.resolve()
                if not target.is_relative_to(self.storage_root.resolve()):
                    raise ValueError("DATASET_PATH_OUTSIDE_STORAGE")
                if target.exists():
                    shutil.rmtree(target)
            del records[dataset_id]
            self._write(records)
            return True

    def delete_project(self, project_id: str, user_id: str) -> int:
        rows = self.list(project_id, user_id)
        for item in rows:
            self.delete(item.dataset_id, user_id, project_id)
        return len(rows)

    def migrate_to_personal(self, project_id: str, user_id: str) -> int:
        """Change ownership metadata while preserving source paths and identities."""
        with self._lock:
            records = self._load()
            matched = [item for item in records.values()
                       if item.user_id == user_id and item.project_id == project_id]
            for item in matched:
                item.project_id = None
            if matched:
                self._write(records)
            return len(matched)

    def delete_user(self, user_id: str) -> int:
        rows = [item for item in self._load().values() if item.user_id == user_id]
        for item in rows:
            self.delete(item.dataset_id, user_id, item.project_id)
        return len(rows)
