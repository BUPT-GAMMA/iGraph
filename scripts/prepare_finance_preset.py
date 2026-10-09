"""Regenerate the deployable finance preset from the three source CSV files."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph_engine.finance_preset import RESOURCES, prepare_finance_preset

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--output", type=Path, default=RESOURCES)
    args = parser.parse_args()
    manifest = prepare_finance_preset(args.source_dir, args.output)
    print(json.dumps({"dataset_id": manifest["dataset"]["dataset_id"],
        "node_count": manifest["dataset"]["node_count"], "edge_count": manifest["dataset"]["edge_count"],
        "inspection": manifest["inspection"]}, ensure_ascii=False, indent=2))
