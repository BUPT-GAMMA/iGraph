"""Install versioned example sources with streaming decompression and hashes."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

RESOURCES = Path(__file__).resolve().parent / "resources" / "example_inputs"


def _name(value):
    if not isinstance(value, str) or not value or Path(value).name != value or "/" in value or "\\" in value:
        raise ValueError("Invalid example asset name")
    return value


def install_example_inputs(data_root, *, resources=RESOURCES, server_root=None):
    server_root = server_root or Path(os.getenv("GRAPH_ENGINE_SERVER_DATASETS_DIR") or Path(data_root) / "server_datasets")
    entries = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))["inputs"]
    for entry in entries:
        identity, filename = _name(entry["id"]), _name(entry["filename"])
        digest = entry["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid example checksum")
        parent = server_root / ".example-inputs" / identity
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / digest
        if target.is_dir() and (target / filename).stat().st_size == entry["size_bytes"]:
            continue
        stage = Path(tempfile.mkdtemp(prefix=".input-", dir=parent))
        try:
            h = hashlib.sha256()
            size = 0
            with (stage / filename).open("wb") as output:
                for part in entry["parts"]:
                    asset = resources / _name(part["file"])
                    with asset.open("rb") as f:
                        if hashlib.file_digest(f, "sha256").hexdigest() != part["sha256"]:
                            raise ValueError("Example compressed part checksum mismatch")
                    with gzip.open(asset, "rb") as stream:
                        while block := stream.read(1024 * 1024):
                            size += len(block)
                            if size > entry["size_bytes"]:
                                raise ValueError("Example decompressed size exceeds manifest")
                            h.update(block)
                            output.write(block)
            if size != entry["size_bytes"] or h.hexdigest() != digest:
                raise ValueError("Example source checksum mismatch")
            (stage / "manifest.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            try:
                os.rename(stage, target)
            except OSError:
                if not (target / filename).is_file():
                    raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return entries

def verified_local_graph_source(path, *, max_nodes, max_edges, resources=RESOURCES):
    """Recognize an exact shipped graph, never a caller-provided allowlist."""
    path = Path(path)
    if path.suffix.lower() != ".pt":
        return False
    size = path.stat().st_size
    entries = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))["inputs"]
    for entry in entries:
        metadata = entry.get("metadata", {})
        if (entry.get("local_graph_loading") is not True
                or size != entry["size_bytes"]
                or not 0 < metadata.get("num_nodes", 0) < max_nodes
                or not 0 < metadata.get("num_edges", 0) < max_edges):
            continue
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == entry["sha256"]
    return False
