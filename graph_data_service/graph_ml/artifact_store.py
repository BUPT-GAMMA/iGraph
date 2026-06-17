"""
GraphML Artifact 存储。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np


@dataclass
class ArtifactRef:
    model_id: str
    session_id: str
    model_dir: str


class ArtifactStore:
    def __init__(self, root_dir: str = "artifacts/graph_ml") -> None:
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def create_model_dir(self, session_id: str, task: str) -> ArtifactRef:
        ts = int(time.time())
        suffix = secrets.token_hex(3)
        model_id = f"mdl_{task}_{ts}_{suffix}"
        model_dir = os.path.join(self.root_dir, session_id, model_id)
        os.makedirs(model_dir, exist_ok=True)
        return ArtifactRef(model_id=model_id, session_id=session_id, model_dir=model_dir)

    def save_json(self, model_dir: str, filename: str, payload: Dict[str, Any]) -> str:
        path = os.path.join(model_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def load_json(self, model_dir: str, filename: str) -> Dict[str, Any]:
        path = os.path.join(model_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_npy(self, model_dir: str, filename: str, arr: np.ndarray) -> str:
        path = os.path.join(model_dir, filename)
        np.save(path, arr)
        return path

    def load_npy(self, model_dir: str, filename: str) -> np.ndarray:
        path = os.path.join(model_dir, filename)
        return np.load(path, allow_pickle=False)

    def save_torch(self, model_dir: str, filename: str, payload: Dict[str, Any]) -> str:
        import torch

        path = os.path.join(model_dir, filename)
        torch.save(payload, path)
        return path

    def load_torch(self, model_dir: str, filename: str) -> Dict[str, Any]:
        import torch

        path = os.path.join(model_dir, filename)
        return torch.load(path, map_location="cpu", weights_only=False)

    def resolve_model(self, session_id: str, model_id: str) -> Optional[ArtifactRef]:
        primary = os.path.join(self.root_dir, session_id, model_id)
        if os.path.isdir(primary):
            return ArtifactRef(model_id=model_id, session_id=session_id, model_dir=primary)
        return None
