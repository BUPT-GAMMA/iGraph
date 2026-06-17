"""
Pre-train 任务定义。
"""

from __future__ import annotations

from ..schemas import TrainRequest


PRETRAIN_METHODS_REQUIRE_MODEL = {
    "contrastive_learning",
    "graph_contrastive_learning",
    "mask_autoencoding",
    "graph_mask_autoencoding",
}


class PreTrainTask:
    def method(self, req: TrainRequest) -> str:
        return (req.method or "").strip().lower()
