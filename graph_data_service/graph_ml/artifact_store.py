"""
GraphML Artifact 存储。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import numpy as np

import graph_engine.paths as _dp


@dataclass
class ArtifactRef:
    model_id: str
    session_id: str
    model_dir: str


class ArtifactStore:
    def __init__(self, root_dir: str = "artifacts/graph_ml") -> None:
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self.root_dir, _dp.safe_path_component(session_id))

    def record_owner(self, runtime_id: str, user_id: str, session_id: str, project_id: str | None) -> None:
        from execution_lifecycle import atomic_json
        path = Path(self.root_dir) / '_owners' / (_dp.safe_path_component(runtime_id) + '.json')
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get('user_id') != user_id or previous.get('session_id') != session_id:
                raise ValueError('ARTIFACT_OWNER_CONFLICT')
        atomic_json(path, {'runtime_id': runtime_id, 'user_id': user_id,
                          'session_id': session_id, 'project_id': project_id})

    def project_targets(self, user_id: str, project_id: str, session_ids: list[str]) -> dict[str, dict]:
        """Trusted membership covers legacy artifacts; receipts cover deleted chats."""
        owners = Path(self.root_dir) / '_owners'
        targets = {}
        for path in owners.glob('*.json'):
            owner = json.loads(path.read_text())
            if owner.get('user_id') == user_id and owner.get('project_id') == project_id:
                targets[owner['runtime_id']] = owner
        for sid in session_ids:
            runtime_id = f'{user_id}:{sid}' if user_id else sid
            receipt = owners / (_dp.safe_path_component(runtime_id) + '.json')
            if receipt.exists():
                owner = json.loads(receipt.read_text())
                if owner.get('user_id') != user_id or owner.get('project_id') != project_id:
                    raise ValueError('ARTIFACT_OWNER_CONFLICT')
            targets.setdefault(runtime_id, {'session_id': sid})
        return targets

    def delete_project(self, user_id: str, project_id: str, session_ids: list[str]) -> int:
        targets = self.project_targets(user_id, project_id, session_ids)
        root = Path(self.root_dir).resolve()
        for runtime_id in targets:
            # Resolve both current encoded paths and verified legacy paths.
            paths = {Path(self._session_dir(runtime_id))}
            legacy = root / runtime_id
            if legacy.resolve().parent == root:
                paths.add(legacy)
            for target in paths:
                resolved = target.resolve()
                if resolved.parent != root:
                    raise ValueError('ARTIFACT_PATH_OUTSIDE_STORAGE')
                if target.exists():
                    shutil.rmtree(target)
            receipt = root / '_owners' / (_dp.safe_path_component(runtime_id) + '.json')
            receipt.unlink(missing_ok=True)
        return len(targets)

    def ownership_report(self) -> dict:
        root = Path(self.root_dir)
        known = set()
        for path in (root / '_owners').glob('*.json'):
            owner = json.loads(path.read_text())
            known.update((_dp.safe_path_component(owner['runtime_id']), owner['runtime_id']))
        return {'unattributed_directories': sorted(path.name for path in root.iterdir()
                if path.is_dir() and path.name != '_owners' and path.name not in known)}

    def create_model_dir(self, session_id: str, task: str) -> ArtifactRef:
        ts = int(time.time())
        suffix = secrets.token_hex(3)
        model_id = f"mdl_{task}_{ts}_{suffix}"
        model_dir = os.path.join(self._session_dir(session_id), model_id)
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

    def save_graph_classification_checkpoint(
        self,
        model_dir: str,
        state_dict: Mapping[str, Any],
        filename: str = "checkpoint.pt",
    ) -> str:
        """Save the narrow, tensor-only checkpoint contract used by graph classification."""
        import torch

        normalized = self._validate_graph_classification_state_dict(state_dict)
        cpu_state = {
            key: tensor.detach().cpu().clone()
            for key, tensor in normalized.items()
        }
        path = os.path.join(model_dir, filename)
        torch.save({"model": cpu_state}, path)
        return path

    def load_graph_classification_checkpoint(
        self,
        model_dir: str,
        filename: str = "checkpoint.pt",
        *,
        map_location: Any = "cpu",
        expected_state_dict: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Safely load and, when supplied, dimension-check a graph classifier state."""
        import torch

        path = os.path.join(model_dir, filename)
        try:
            payload = torch.load(
                path,
                map_location=map_location,
                weights_only=True,
            )
        except Exception as exc:
            raise ValueError(f"图分类 checkpoint 安全读取失败: {exc}") from exc
        if not isinstance(payload, dict) or set(payload) != {"model"}:
            raise ValueError("图分类 checkpoint 根对象必须且只能包含 model")
        state_dict = self._validate_graph_classification_state_dict(payload["model"])
        if expected_state_dict is not None:
            expected = self._validate_graph_classification_state_dict(expected_state_dict)
            actual_keys = set(state_dict)
            expected_keys = set(expected)
            if actual_keys != expected_keys:
                missing = sorted(expected_keys - actual_keys)
                unexpected = sorted(actual_keys - expected_keys)
                raise ValueError(
                    "图分类 checkpoint 参数键与 metadata 重建模型不一致: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            for key, tensor in state_dict.items():
                reference = expected[key]
                if tensor.shape != reference.shape:
                    raise ValueError(
                        "图分类 checkpoint 参数维度与 metadata 不一致: "
                        f"{key}={list(tensor.shape)}, expected={list(reference.shape)}"
                    )
                if tensor.dtype != reference.dtype:
                    raise ValueError(
                        "图分类 checkpoint 参数 dtype 与 metadata 不一致: "
                        f"{key}={tensor.dtype}, expected={reference.dtype}"
                    )
        return state_dict

    @staticmethod
    def _validate_graph_classification_state_dict(
        state_dict: Mapping[str, Any],
    ) -> Dict[str, Any]:
        import torch

        if not isinstance(state_dict, Mapping) or not state_dict:
            raise ValueError("图分类 checkpoint.model 必须是非空 state_dict")
        normalized: Dict[str, Any] = {}
        for key, value in state_dict.items():
            if not isinstance(key, str) or not key:
                raise ValueError("图分类 checkpoint 的参数键必须是非空字符串")
            if not isinstance(value, torch.Tensor):
                raise ValueError(f"图分类 checkpoint 参数必须是 Tensor: {key}")
            normalized[key] = value
        return normalized

    def resolve_model(self, session_id: str, model_id: str) -> Optional[ArtifactRef]:
        primary = os.path.join(self._session_dir(session_id), model_id)
        if os.path.isdir(primary):
            return ArtifactRef(model_id=model_id, session_id=session_id, model_dir=primary)
        legacy = os.path.join(self.root_dir, session_id, model_id)
        if legacy != primary and os.path.isdir(legacy):
            return ArtifactRef(model_id=model_id, session_id=session_id, model_dir=legacy)
        return None
