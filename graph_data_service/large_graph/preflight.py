"""Bounded-memory probes used before an upload enters the small-graph path."""

from __future__ import annotations

import csv
import hashlib
import math
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class StructuredSourceProbe:
    """Streaming size signals without constructing nodes, edges, or a graph."""

    rows_scanned: int
    estimated_nodes: int
    edge_threshold_reached: bool
    node_threshold_reached: bool
    delimiter: str
    estimator: str = "linear-counting-2^20"


@dataclass(frozen=True)
class BinaryArchiveProbe:
    """Expansion metadata read from a ZIP central directory without extraction."""

    is_archive: bool
    entries: int
    uncompressed_bytes: int


class _LinearNodeCounter:
    """Approximate distinct counter backed by a fixed 128 KiB bitmap.

    BLAKE2 gives stable, adversarially robust slot selection for uploaded node
    names; only one 64-bit digest per endpoint is retained transiently.
    """

    _SLOT_BITS = 20
    _SLOTS = 1 << _SLOT_BITS
    _MASK = _SLOTS - 1

    def __init__(self) -> None:
        self._bitmap = bytearray(self._SLOTS // 8)
        self._occupied = 0

    def add(self, value: str) -> None:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest, "big") & self._MASK
        byte_index, bit_index = divmod(slot, 8)
        mask = 1 << bit_index
        if not self._bitmap[byte_index] & mask:
            self._bitmap[byte_index] |= mask
            self._occupied += 1

    def estimate(self) -> int:
        empty = self._SLOTS - self._occupied
        if empty <= 0:
            return self._SLOTS * 32
        estimate = -self._SLOTS * math.log(empty / self._SLOTS)
        return max(self._occupied, int(round(estimate)))


def _record_byte_limit() -> int:
    try:
        configured = int(os.getenv("LARGE_GRAPH_MAX_RECORD_BYTES", str(1024 * 1024)))
    except (TypeError, ValueError, OverflowError):
        configured = 1024 * 1024
    return max(1024, configured)


def _bounded_text_lines(source: Path) -> Iterator[str | None]:
    """Yield decoded records without ever materialising an oversized line."""
    limit = _record_byte_limit()
    with source.open("rb") as handle:
        while True:
            raw = handle.readline(limit + 1)
            if not raw:
                break
            if len(raw) > limit:
                while not raw.endswith(b"\n"):
                    raw = handle.readline(limit + 1)
                    if not raw:
                        break
                yield None
                continue
            yield raw.decode("utf-8", errors="replace")


def detect_delimiter(source: Path) -> str:
    for line in _bounded_text_lines(source):
        if line is not None:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "\t" in stripped:
                return "tab"
            if "," in stripped:
                return "comma"
            return "whitespace"
    return "whitespace"


def _endpoints(line: str, delimiter: str, input_format: str) -> tuple[str, str] | None:
    try:
        if delimiter == "whitespace":
            parts = line.split()
        else:
            actual = {"comma": ",", "tab": "\t"}.get(delimiter, delimiter)
            parts = [part.strip() for part in next(csv.reader([line], delimiter=actual))]
    except (csv.Error, StopIteration):
        return None
    if input_format == "triples":
        if len(parts) < 3:
            return None
        source, target = parts[0].strip(), parts[2].strip()
    else:
        if len(parts) < 2:
            return None
        source, target = parts[0].strip(), parts[1].strip()
    if not source or not target:
        return None
    if source.lower() in {"source", "src", "from"} and target.lower() in {
        "target", "dst", "to",
    }:
        return None
    return source, target


def probe_structured_source(
    source_path: str | Path,
    *,
    input_format: str,
    edge_threshold: int,
    node_threshold: int,
    delimiter: str = "auto",
) -> StructuredSourceProbe:
    """Stop once either large-graph threshold is reached.

    A 5% early-routing margin absorbs estimator error.  A false positive only
    sends a borderline source through the safer streaming preparation path;
    a false negative could materialise an oversized graph in the API process.
    """

    source = Path(source_path)
    actual_delimiter = detect_delimiter(source) if delimiter == "auto" else delimiter
    edge_threshold = max(1, int(edge_threshold))
    node_threshold = max(1, int(node_threshold))
    node_route_mark = max(1, int(math.floor(node_threshold * 0.95)))
    counter = _LinearNodeCounter()
    rows = 0
    estimate = 0
    edge_reached = False
    node_reached = False

    for raw_line in _bounded_text_lines(source):
        if raw_line is None:
            # Count an oversized non-empty record conservatively toward the
            # routing edge threshold, but never parse/materialise it.
            rows += 1
            edge_reached = rows >= edge_threshold
            if edge_reached:
                break
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows += 1
        endpoints = _endpoints(stripped, actual_delimiter, input_format)
        if endpoints is not None:
            counter.add(endpoints[0])
            counter.add(endpoints[1])
        edge_reached = rows >= edge_threshold
        # Avoid logarithms on every record at production thresholds while
        # retaining exact responsiveness in small-threshold tests/configs.
        if rows % 256 == 0 or edge_reached or node_threshold <= 512:
            estimate = counter.estimate()
            node_reached = estimate >= node_route_mark
        if edge_reached or node_reached:
            break

    if not estimate or not (edge_reached or node_reached):
        estimate = counter.estimate()
        node_reached = estimate >= node_route_mark
    return StructuredSourceProbe(
        rows_scanned=rows,
        estimated_nodes=estimate,
        edge_threshold_reached=edge_reached,
        node_threshold_reached=node_reached,
        delimiter=actual_delimiter,
    )


def probe_binary_archive(source_path: str | Path) -> BinaryArchiveProbe:
    """Return aggregate expansion size for NPZ and modern PyTorch archives."""

    source = Path(source_path)
    if not zipfile.is_zipfile(source):
        return BinaryArchiveProbe(False, 0, source.stat().st_size)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            entries = 0
            uncompressed_bytes = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                entries += 1
                uncompressed_bytes += max(0, int(info.file_size))
    except (OSError, zipfile.BadZipFile, ValueError):
        return BinaryArchiveProbe(False, 0, source.stat().st_size)
    return BinaryArchiveProbe(True, entries, uncompressed_bytes)


__all__ = [
    "BinaryArchiveProbe",
    "StructuredSourceProbe",
    "detect_delimiter",
    "probe_binary_archive",
    "probe_structured_source",
]
