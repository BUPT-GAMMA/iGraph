"""Helpers for stable node-label class mappings."""

from __future__ import annotations

import math
from typing import Any, Iterable, List, Optional

import numpy as np


def is_missing_label(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (int, np.integer)) and int(value) < 0:
        return True
    if isinstance(value, (float, np.floating)) and math.isnan(float(value)):
        return True
    return False


def label_value(value: Any) -> Any:
    return value.item() if hasattr(value, "item") else value


def label_key(value: Any) -> str:
    value = label_value(value)
    return f"{type(value).__name__}:{value}"


def sorted_distinct_label_values(values: Iterable[Any]) -> List[Any]:
    by_key: dict[str, Any] = {}
    for raw in values:
        if is_missing_label(raw):
            continue
        value = label_value(raw)
        by_key.setdefault(label_key(value), value)
    return sorted(by_key.values(), key=_label_sort_key)


def label_name_for_value(label_names: Optional[List[str]], value: Any) -> Optional[str]:
    if not label_names:
        return None
    value = label_value(value)
    if isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, (int, np.integer)):
        idx = int(value)
    elif isinstance(value, (float, np.floating)) and float(value).is_integer():
        idx = int(value)
    else:
        return None
    if 0 <= idx < len(label_names):
        return str(label_names[idx])
    return None


def display_label(label_values: List[Any], label_names: List[Optional[str]], class_idx: int) -> Any:
    if 0 <= class_idx < len(label_names) and label_names[class_idx]:
        return label_names[class_idx]
    if 0 <= class_idx < len(label_values):
        return label_values[class_idx]
    return class_idx


def aligned_label_names(label_values: List[Any], label_names: Optional[List[str]]) -> List[Optional[str]]:
    return [label_name_for_value(label_names, value) for value in label_values]


def class_mapping(label_values: List[Any], label_names: List[Optional[str]]) -> List[dict[str, Any]]:
    rows: List[dict[str, Any]] = []
    for idx, value in enumerate(label_values):
        name = label_names[idx] if idx < len(label_names) else None
        row = {
            "class_idx": idx,
            "label_value": value,
            "display_label": name or value,
        }
        if name:
            row["label_name"] = name
        if isinstance(value, (int, np.integer)) and int(value) >= 0:
            row["original_class_idx"] = int(value)
        rows.append(row)
    return rows


def _label_sort_key(value: Any) -> tuple:
    value = label_value(value)
    if isinstance(value, (bool, np.bool_)):
        return (0, int(value), "")
    if isinstance(value, (int, np.integer)):
        return (1, int(value), "")
    if isinstance(value, (float, np.floating)):
        return (2, float(value), "")
    return (3, str(value), repr(value))
