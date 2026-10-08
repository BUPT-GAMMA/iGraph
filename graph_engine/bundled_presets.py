"""Install checked bundled graph snapshots without replacing user data."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def install_bundled_preset(data_root: Path, *, resources: Path, server_root: Path | None = None) -> dict:
    """Publish complete snapshots once; restarts never replace saved user data."""
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    filename = manifest["filename"]
    dataset_id = manifest["dataset"]["dataset_id"]
    if Path(filename).name != filename or not re.fullmatch(r"[a-zA-Z0-9_-]+", dataset_id):
        raise ValueError("Invalid bundled example identity")
    asset = resources / filename
    if hashlib.sha256(asset.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Bundled example graph checksum mismatch")
    registry = data_root / "global" / "datasets"
    registry.mkdir(parents=True, exist_ok=True)
    target = registry / dataset_id
    if not target.exists():
        stage = Path(tempfile.mkdtemp(prefix=".preset-", dir=registry))
        try:
            (stage / "versions").mkdir()
            shutil.copyfile(asset, stage / "graph.npz")
            shutil.copyfile(asset, stage / "versions" / "v001.npz")
            (stage / "meta.json").write_text(json.dumps(manifest["dataset"], ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.rename(stage, target)
            except OSError:
                if not (target / "meta.json").is_file():
                    raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    server_root = server_root or Path(os.getenv("GRAPH_ENGINE_SERVER_DATASETS_DIR") or data_root / "server_datasets")
    server_root.mkdir(parents=True, exist_ok=True)
    public_asset = server_root / filename
    if not public_asset.exists():
        fd, temporary = tempfile.mkstemp(prefix=".preset-", dir=server_root)
        os.close(fd)
        try:
            shutil.copyfile(asset, temporary)
            # Publish the complete file exclusively; registry copies stay independent.
            try:
                os.link(temporary, public_asset)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"dataset_id": dataset_id, "server_filename": filename,
        "node_count": manifest["dataset"]["node_count"], "edge_count": manifest["dataset"]["edge_count"]}
