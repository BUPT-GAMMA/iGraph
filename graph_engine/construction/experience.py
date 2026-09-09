"""Private domain experience from bounded, source-independent graph evidence.

The caller supplies a trusted, user-isolated database path. This module stores
only fixed strategy identifiers, whitelisted structural features, measurements,
and SHA-256 identifiers. It never stores table/column names, cells, prompts,
tool arguments or SQL. One source contributes only its latest observation for
each domain/strategy/pattern, so repeated tuning cannot manufacture support.

Evidence is bounded to the most recent 2048 source-pattern pairs and 256
patterns. Retry receipts retain the latest 4096 runs plus runs still referenced
by evidence; retries within that retained window are idempotent.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path


STRATEGY_IDS = frozenset({
    "materialize_join", "collapse_node", "extract_property_node", "node_property_to_edge",
    "connect_columns", "generate_entity_table", "unfold_multi_category", "add_primary_key",
    "remove_primary_key", "reverse_edges", "rename_type", "delete",
})
QUERY_FAMILIES = frozenset({
    "lookup", "filter", "join", "multi_hop", "aggregation", "group_by", "sort",
    "existence", "left_join", "reverse_join",
})
CONFIDENCE_STATUSES = frozenset({"candidate", "supported", "cautioned"})
MAX_EVIDENCE = 2048
MAX_PATTERNS = 256
MAX_RECENT_RUNS = 4096
MAX_OBSERVATIONS = 100

_CONTEXT_COUNTS = {"table_count": 128, "row_count": 200_000, "column_count": 32_768,
                   "fk_count": 16_384, "query_count": 100, "max_join_hops": 3}
_CONTEXT_FLAGS = {"has_composite_keys", "has_nullable_fks"}
_PATTERN_COUNTS = {"join_hops": 3, "fk_arity": 256, "affected_table_count": 128}
_PATTERN_ENUMS = {
    "relationship_shape": {"single_fk", "bridge", "chain", "star", "many_to_many", "attribute"},
    "fanout_bucket": {"one", "few", "many"},
    "row_count_bucket": {"empty", "tiny", "small", "medium", "large"},
    "property_kind": {"categorical", "numeric", "text", "identifier", "mixed"},
    "query_family": QUERY_FAMILIES,
}
_OBSERVATION_FIELDS = {"strategy_id", "pattern", "outcome", "relative_gain", "query_correct", "query_total"}


class ExperienceError(ValueError):
    """An experience input violates the structural-only evidence contract."""


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ExperienceError("Experience identifiers must contain valid Unicode.") from exc
    return hashlib.sha256(encoded).hexdigest()


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ExperienceError("Experience identifiers must be nonempty strings of at most 1024 characters.")
    return _digest(value)


def _count(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ExperienceError(f"{label} requires a bounded nonnegative integer.")
    return value


def _enum(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ExperienceError(f"{label} requires a supported fixed enum value.")
    return value


def _number(value, label):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ExperienceError(f"{label} requires a finite number.")
    return float(value)


def _context(value):
    allowed = set(_CONTEXT_COUNTS) | _CONTEXT_FLAGS | {"source_kind", "query_families", "query_mix"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ExperienceError("Unsupported context fields; only structural/query features are permitted.")
    result = {}
    for field, item in value.items():
        if field in _CONTEXT_COUNTS:
            result[field] = _count(item, _CONTEXT_COUNTS[field], "Context count")
        elif field in _CONTEXT_FLAGS:
            if type(item) is not bool:
                raise ExperienceError("Context flags must be booleans.")
            result[field] = item
        elif field == "source_kind":
            result[field] = _enum(item, {"csv", "sqlite", "mixed"}, "Source kind")
        elif field == "query_families":
            if not isinstance(item, list) or len(item) > len(QUERY_FAMILIES):
                raise ExperienceError("Query families must be a bounded list of fixed enums.")
            families = [_enum(family, QUERY_FAMILIES, "Query family") for family in item]
            if len(families) != len(set(families)):
                raise ExperienceError("Query families cannot contain duplicates.")
            result[field] = sorted(families)
        else:
            if not isinstance(item, dict) or len(item) > len(QUERY_FAMILIES):
                raise ExperienceError("Query mix must map fixed query families to bounded counts.")
            result[field] = {_enum(family, QUERY_FAMILIES, "Query family"): _count(count, 100, "Query mix")
                             for family, count in item.items()}
    return result


def _pattern(value):
    allowed = set(_PATTERN_COUNTS) | set(_PATTERN_ENUMS) | {"repeated_value_ratio"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ExperienceError("Unsupported pattern fields; source names and values are not permitted.")
    result = {}
    for field, item in value.items():
        if field in _PATTERN_COUNTS:
            result[field] = _count(item, _PATTERN_COUNTS[field], "Pattern count")
        elif field in _PATTERN_ENUMS:
            result[field] = _enum(item, _PATTERN_ENUMS[field], "Pattern feature")
        else:
            number = _number(item, "Repeated-value ratio")
            if not 0 <= number <= 1:
                raise ExperienceError("Repeated-value ratio must lie between zero and one.")
            result[field] = number
    return result


def _observations(value):
    if not isinstance(value, list) or len(value) > MAX_OBSERVATIONS:
        raise ExperienceError("A run may contain at most 100 structured observations.")
    latest = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != _OBSERVATION_FIELDS:
            raise ExperienceError("Observations require exactly the six structured evidence fields.")
        strategy = _enum(item["strategy_id"], STRATEGY_IDS, "Strategy")
        pattern = _pattern(item["pattern"])
        outcome = _enum(item["outcome"], {"accepted", "no_gain", "unsafe"}, "Outcome")
        gain = max(-1.0, min(1.0, _number(item["relative_gain"], "Relative gain")))
        correct = _count(item["query_correct"], 100, "Correct queries")
        total = _count(item["query_total"], 100, "Total queries")
        if correct > total:
            raise ExperienceError("Correct query count cannot exceed the total query count.")
        if outcome in {"accepted", "no_gain"} and (total == 0 or correct != total):
            outcome = "unsafe"
        if outcome == "unsafe":
            gain = 0.0  # Invalid graph results provide no usable speed measurement.
        observation = {"strategy_id": strategy, "pattern": pattern, "outcome": outcome,
                       "relative_gain": gain, "query_correct": correct, "query_total": total}
        latest[strategy, _json(pattern)] = observation
    return [latest[key] for key in sorted(latest)]


def _similarity(requested, observed, pattern):
    if not requested:
        return 1.0
    if requested.get("source_kind") and observed.get("source_kind") and requested["source_kind"] != observed["source_kind"] and "mixed" not in (requested["source_kind"], observed["source_kind"]):
        return 0.0
    if pattern.get("join_hops", 0) > requested.get("max_join_hops", 3):
        return 0.0
    requested_families = set(requested.get("query_families", [])) | {
        family for family, count in requested.get("query_mix", {}).items() if count}
    if requested_families and pattern.get("query_family") and pattern["query_family"] not in requested_families:
        return 0.0
    weights = {"query_mix": 4.0, "query_families": 3.0, "source_kind": 2.0,
               "table_count": 2.0, "fk_count": 2.0, "max_join_hops": 2.0}
    score = weight_sum = 0.0
    for key, left in requested.items():
        weight = weights.get(key, 1.0)
        weight_sum += weight
        if key not in observed:
            value = 0.5
        else:
            right = observed[key]
            if key == "query_families":
                a, b = set(left), set(right)
                value = len(a & b) / len(a | b) if a or b else 1.0
            elif key == "query_mix":
                a, b = sum(left.values()), sum(right.values())
                value = (1.0 if not a and not b else 0.0) if not a or not b else 1.0 - 0.5 * sum(
                    abs(left.get(family, 0) / a - right.get(family, 0) / b) for family in QUERY_FAMILIES)
            elif key in _CONTEXT_COUNTS:
                value = 1.0 - abs(math.log1p(left) - math.log1p(right)) / max(math.log1p(left), math.log1p(right), 1.0)
            else:
                value = float(left == right)
        score += weight * value
    return max(0.0, min(1.0, score / weight_sum))


class DomainExperienceStore:
    """SQLite evidence owned by the private construction worker, never the main agent.

    Retrieval errors propagate so callers distinguish unavailable storage from
    a cold start; writes return a structured ``stored=False`` result on storage
    contention/unavailability. Invalid structural input is rejected explicitly.
    No operation waits more than a short SQLite busy timeout for a writer lock.
    """

    def __init__(self, path, *, timeout_seconds=0.15):
        timeout = _number(timeout_seconds, "SQLite timeout")
        if not 0 <= timeout <= 0.5:
            raise ExperienceError("SQLite timeout must be between zero and half a second.")
        self.path = Path(path)
        self.timeout_seconds = timeout

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS experience_runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_hash TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS experience_patterns (
                domain_hash TEXT NOT NULL, pattern_hash TEXT NOT NULL, strategy_id TEXT NOT NULL,
                pattern_json TEXT NOT NULL, last_sequence INTEGER NOT NULL,
                PRIMARY KEY(domain_hash, pattern_hash))""")
            db.execute("""CREATE TABLE IF NOT EXISTS experience_evidence (
                domain_hash TEXT NOT NULL, pattern_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
                run_sequence INTEGER NOT NULL, context_json TEXT NOT NULL, outcome TEXT NOT NULL,
                relative_gain REAL NOT NULL, query_correct INTEGER NOT NULL, query_total INTEGER NOT NULL,
                PRIMARY KEY(domain_hash, pattern_hash, source_hash),
                FOREIGN KEY(domain_hash, pattern_hash) REFERENCES experience_patterns(domain_hash, pattern_hash)
                    ON DELETE CASCADE)""")
            db.execute("CREATE INDEX IF NOT EXISTS experience_recent ON experience_evidence(run_sequence)")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _prune(db):
        db.execute("""DELETE FROM experience_evidence WHERE rowid IN (
            SELECT rowid FROM experience_evidence ORDER BY run_sequence DESC, rowid DESC LIMIT -1 OFFSET ?)
            """, (MAX_EVIDENCE,))
        db.execute("""DELETE FROM experience_patterns WHERE rowid IN (
            SELECT rowid FROM experience_patterns ORDER BY last_sequence DESC, rowid DESC LIMIT -1 OFFSET ?)
            """, (MAX_PATTERNS,))
        db.execute("""DELETE FROM experience_patterns WHERE NOT EXISTS (
            SELECT 1 FROM experience_evidence e WHERE e.domain_hash=experience_patterns.domain_hash
                AND e.pattern_hash=experience_patterns.pattern_hash)""")
        db.execute("""DELETE FROM experience_runs WHERE sequence NOT IN (
            SELECT sequence FROM experience_runs ORDER BY sequence DESC LIMIT ?)
            AND sequence NOT IN (SELECT run_sequence FROM experience_evidence)""", (MAX_RECENT_RUNS,))

    def record_run(self, run_id, source_key, domain_key, context, observations, *, final_correct):
        if type(final_correct) is not bool:
            raise ExperienceError("final_correct must be a boolean.")
        run_hash, source_hash, domain_hash = _identity(run_id), _identity(source_key), _identity(domain_key)
        normalized_context = _context(context)
        normalized = _observations(observations)
        eligible = normalized if final_correct else [item for item in normalized if item["outcome"] == "unsafe"]
        payload_hash = _digest(_json({"source": source_hash, "domain": domain_hash, "context": normalized_context,
                                      "observations": normalized, "final_correct": final_correct}))
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    previous = db.execute("SELECT payload_hash FROM experience_runs WHERE run_hash=?", (run_hash,)).fetchone()
                    if previous is not None:
                        if previous[0] != payload_hash:
                            raise ExperienceError("run_id was already recorded with different evidence.")
                        db.rollback()
                        return {"stored": False, "duplicate": True, "status": "duplicate", "evidence_count": 0}
                    cursor = db.execute("INSERT INTO experience_runs(run_hash,payload_hash) VALUES(?,?)", (run_hash, payload_hash))
                    sequence = cursor.lastrowid
                    for item in eligible:
                        pattern_json = _json(item["pattern"])
                        pattern_hash = _digest(_json([item["strategy_id"], item["pattern"]]))
                        db.execute("""INSERT INTO experience_patterns(domain_hash,pattern_hash,strategy_id,pattern_json,last_sequence)
                            VALUES(?,?,?,?,?) ON CONFLICT(domain_hash,pattern_hash) DO UPDATE SET last_sequence=excluded.last_sequence""",
                                   (domain_hash, pattern_hash, item["strategy_id"], pattern_json, sequence))
                        db.execute("""INSERT INTO experience_evidence(domain_hash,pattern_hash,source_hash,run_sequence,
                            context_json,outcome,relative_gain,query_correct,query_total) VALUES(?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(domain_hash,pattern_hash,source_hash) DO UPDATE SET
                            run_sequence=excluded.run_sequence, context_json=excluded.context_json, outcome=excluded.outcome,
                            relative_gain=excluded.relative_gain, query_correct=excluded.query_correct, query_total=excluded.query_total""",
                                   (domain_hash, pattern_hash, source_hash, sequence, _json(normalized_context), item["outcome"],
                                    item["relative_gain"], item["query_correct"], item["query_total"]))
                    self._prune(db)
                    db.commit()
                    return {"stored": True, "duplicate": False, "status": "recorded", "evidence_count": len(eligible),
                            "skipped_count": len(normalized) - len(eligible)}
                except BaseException:
                    db.rollback()
                    raise
        except (sqlite3.Error, OSError) as exc:
            busy = isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc).lower() or "busy" in str(exc).lower())
            return {"stored": False, "duplicate": False, "status": "busy" if busy else "unavailable", "evidence_count": 0}

    def retrieve(self, domain_key, context, limit=8):
        domain_hash = _identity(domain_key)
        requested = _context(context)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ExperienceError("Retrieval limit must be an integer between one and 32.")
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT p.strategy_id,p.pattern_json,e.source_hash,e.context_json,e.outcome,
                e.relative_gain,e.query_correct,e.query_total FROM experience_patterns p
                JOIN experience_evidence e ON p.domain_hash=e.domain_hash AND p.pattern_hash=e.pattern_hash
                WHERE p.domain_hash=?""", (domain_hash,)).fetchall()
        grouped = defaultdict(list)
        for strategy, pattern_json, source_hash, context_json, outcome, gain, correct, total in rows:
            try:
                # Revalidate persisted values; a corrupt/local legacy DB cannot
                # inject arbitrary prose into a future private worker prompt.
                strategy = _enum(strategy, STRATEGY_IDS, "Strategy")
                pattern = _pattern(json.loads(pattern_json))
                observed = _context(json.loads(context_json))
                outcome = _enum(outcome, {"accepted", "no_gain", "unsafe"}, "Outcome")
                gain = _number(gain, "Relative gain")
                correct, total = _count(correct, 100, "Correct queries"), _count(total, 100, "Total queries")
                if not -1 <= gain <= 1 or correct > total or outcome in {"accepted", "no_gain"} and (total == 0 or correct != total) or outcome == "unsafe" and gain != 0:
                    continue
                similarity = _similarity(requested, observed, pattern)
                if similarity < 0.35:
                    continue
                grouped[strategy, _json(pattern)].append((source_hash, outcome, gain, similarity))
            except (ExperienceError, ValueError, TypeError):
                continue
        result = []
        for (strategy, pattern_json), evidence in grouped.items():
            trials = len(evidence)
            independent = len({item[0] for item in evidence})
            accepted = sum(item[1] == "accepted" for item in evidence)
            no_gain = sum(item[1] == "no_gain" for item in evidence)
            unsafe = sum(item[1] == "unsafe" for item in evidence)
            gain_evidence = [item for item in evidence if item[1] in {"accepted", "no_gain"}]
            mean_gain = sum(item[2] for item in gain_evidence) / len(gain_evidence) if gain_evidence else 0.0
            weight = sum(item[3] for item in evidence)
            similarity = weight / trials
            # Support count controls confidence, not a separate score multiplier:
            # an extra failed source must not strengthen an existing success.
            # Keep measured gains above intact; policy rewards are distinct from
            # measurements, so a sub-threshold positive no_gain remains negative.
            def reward(item):
                if item[1] == "accepted":
                    return 0.25 + 0.75 * item[2]
                if item[1] == "no_gain":
                    return -0.25 + 0.75 * min(item[2], 0.0)
                return -1.0
            score = sum(reward(item) * item[3] for item in evidence) / (weight + 2.0) * similarity
            score = max(-1.0, min(1.0, score))
            if independent >= 3 and accepted / trials >= 2 / 3 and unsafe == 0 and mean_gain > 0:
                confidence = "supported"
            elif unsafe or no_gain >= accepted or score <= 0:
                confidence = "cautioned"
            else:
                confidence = "candidate"
            result.append({"strategy_id": strategy, "pattern": json.loads(pattern_json),
                           "independent_sources": independent, "trials": trials, "accepted": accepted,
                           "no_gain": no_gain, "unsafe": unsafe, "mean_gain": round(mean_gain, 6),
                           "gain_samples": len(gain_evidence),
                           "confidence_status": confidence, "policy_score": round(score, 6),
                           "context_similarity": round(similarity, 6)})
        result.sort(key=lambda item: (-item["policy_score"], -item["independent_sources"],
                                      -item["mean_gain"], item["strategy_id"], _json(item["pattern"])))
        return result[:limit]
