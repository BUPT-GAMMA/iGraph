"""Incremental row budgets shared by database consumption and model previews."""
import base64
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from neo4j.graph import Node, Path, Relationship
from neo4j.spatial import Point
from neo4j.time import Date, DateTime, Duration, Time


class BudgetExceeded(Exception):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class _Encoder:
    def __init__(self, remaining):
        self.remaining = remaining

    def spend(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise BudgetExceeded

    def mapping(self, items, depth):
        self.spend(2)
        result = {}
        for key, value in items:
            if result:
                self.spend(1)
            key = self.value(str(key), depth + 1)
            self.spend(1)
            result[key] = self.value(value, depth + 1)
        return result

    def value(self, value, depth=0):
        if depth > 32:
            raise BudgetExceeded
        if isinstance(value, Node):
            # Encode properties as a mapping here so the Node is not wrapped twice.
            properties = self.mapping(value.items(), depth + 1)
            result = self.mapping((('type', 'node'), ('element_id', value.element_id),
                                   ('labels', sorted(value.labels))), depth)
            self.spend(len(',"properties":'))
            result['properties'] = properties
            return result
        if isinstance(value, Relationship):
            properties = self.mapping(value.items(), depth + 1)
            result = self.mapping((('type', 'relationship'), ('element_id', value.element_id),
                                   ('relationship_type', value.type),
                                   ('start_node', value.start_node.element_id if value.start_node is not None else None),
                                   ('end_node', value.end_node.element_id if value.end_node is not None else None)), depth)
            self.spend(len(',"properties":'))
            result['properties'] = properties
            return result
        if isinstance(value, Path):
            value = dict(type='path', nodes=value.nodes, relationships=value.relationships)
        elif isinstance(value, Point):
            value = dict(type='point', srid=value.srid, coordinates=list(value))
        elif isinstance(value, (Date, DateTime, Time, Duration)):
            value = dict(type=type(value).__name__, value=value.iso_format())
        elif isinstance(value, (datetime, date, time, timedelta)):
            value = dict(type=type(value).__name__, value=str(value))
        elif isinstance(value, (bytes, bytearray)):
            if len(value) * 4 // 3 > self.remaining:
                raise BudgetExceeded
            value = dict(type='bytes', base64=base64.b64encode(value).decode('ascii'))
        elif isinstance(value, float) and not math.isfinite(value):
            value = dict(type='float', value='NaN' if math.isnan(value) else ('Infinity' if value > 0 else '-Infinity'))
        elif type(value) is int and abs(value) > 9007199254740991:
            value = dict(type='integer', value=str(value))
        if isinstance(value, Mapping):
            return self.mapping(value.items(), depth)
        if isinstance(value, (list, tuple)):
            self.spend(2)
            result = []
            for item in value:
                if result:
                    self.spend(1)
                result.append(self.value(item, depth + 1))
            return result
        if isinstance(value, str) and len(value) > self.remaining:
            raise BudgetExceeded
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise BudgetExceeded
        self.spend(len(dumps(value).encode('utf-8')))
        return value


@dataclass
class RowBudget:
    max_rows: int
    max_bytes: int
    rows: list = field(default_factory=list)
    truncated: bool = False
    used_bytes: int = 2

    @property
    def returned_rows(self):
        return len(self.rows)

    def add(self, row) -> bool:
        if len(self.rows) >= self.max_rows:
            self.truncated = True
            return False
        remaining = self.max_bytes - self.used_bytes - bool(self.rows)
        encoder = _Encoder(remaining)
        try:
            encoded = encoder.value(row)
        except BudgetExceeded:
            self.truncated = True
            return False
        self.used_bytes += remaining - encoder.remaining + bool(self.rows)
        self.rows.append(encoded)
        return True


def bounded_rows(rows, *, max_rows, max_bytes):
    result = RowBudget(max_rows, max_bytes)
    for row in rows:
        if not result.add(row):
            break
    return result
