"""Provision independent, user-editable bundled input copies once per user."""
import json
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "graph_engine" / "resources" / "example_inputs"


def ensure_example_inputs(directory, server_root, user_id):
    manifest_path = RESOURCES / "manifest.json"
    if not manifest_path.is_file():
        return
    for source in json.loads(manifest_path.read_text(encoding="utf-8"))["inputs"]:
        path = server_root / ".example-inputs" / source["id"] / source["sha256"] / source["filename"]
        receipt = path.parent / "manifest.json"
        if not path.is_file() or not receipt.is_file():
            continue
        published = json.loads(receipt.read_text(encoding="utf-8"))
        if published["sha256"] != source["sha256"] or path.stat().st_size != source["size_bytes"]:
            raise ValueError("Installed example input does not match its manifest")
        directory.ensure_user_case_source(user_id=user_id,
            case={"id": "input-" + source["id"], "name": source["name"], "description": source["description"]},
            source_path=path, source=source)
