from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent_runtime.agent.sub_agents.base import SubAgent


_WRAPPER_CHARS = (
    "\"'`"  # ASCII: double-quote, single-quote, backtick
    "“”‘’"  # curly double and single quotes
    "「」『』《》"  # CJK brackets
    "()（）[]【】{}"
)


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return "timeout" in type(exc).__name__.lower()


def _is_retryable_error(exc: Exception) -> bool:
    """瞬态错误（网络抖动、限流、5xx）值得重试；确定性错误（4xx）不重试。"""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("connection", "ratelimit", "overloaded", "serviceunavailable")):
        return True
    status = getattr(exc, "status_code", None)
    if status is not None:
        return int(status) >= 500 or int(status) == 429
    # 未知异常保守地认为可重试
    return True


_RELATION_ALIASES = {
    "就职于": "works_at",
    "工作于": "works_at",
    "任职于": "works_at",
    "works at": "works_at",
    "works for": "works_at",
    "employed by": "works_at",
    "属于": "belongs_to",
    "隶属于": "belongs_to",
    "is part of": "belongs_to",
    "belongs to": "belongs_to",
    "连接到": "connects_to",
    "链接到": "connects_to",
    "指向": "connects_to",
    "links to": "connects_to",
    "connects to": "connects_to",
    "related to": "related_to",
    "is related to": "related_to",
}


TEXT_GRAPH_EXTRACTION_SYSTEM_PROMPT = """\
你是文本知识图谱抽取 Sub-Agent。请从给定的文本 chunk 中抽取实体关系三元组。

输出规则：
- 可以先简短推理（不超过三句）。
- 然后，必须输出一个 <triples> 块，字段间用 ` | ` 分隔（竖线两侧带空格）：
  <triples>
  主体 | 关系 | 客体
  主体 | 关系 | 客体
  </triples>
- 若 chunk 中没有明确的实体关系，输出（英文模型可用 none）：
  <triples>
  无实体
  </triples>
- 所有三元组必须在 <triples> 块内，块外不要输出任何三元组行。
- 不要把标题、列表编号、章节名误当关系。
"""


@dataclass
class TextGraphExtractionResult:
    triples: list[dict[str, Any]]
    raw_triple_count: int
    deduped_triple_count: int
    skipped_count: int
    chunk_count: int
    entity_aliases: dict[str, list[str]] = field(default_factory=dict)
    relation_aliases: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TextChunk:
    chunk_id: int
    text: str
    start_char: int
    end_char: int
    title_path: tuple[str, ...] = ()


class TextChunker:
    """Deterministic chunker used only by TextGraphExtractionSubAgent."""

    _TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\s]")
    _SENTENCE_RE = re.compile(r"(?<=[。！？!?;；.])\s+|\n+")
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def __init__(self, *, max_chunk_tokens: int = 3000, overlap_tokens: int = 250) -> None:
        self.max_chunk_tokens = max(int(max_chunk_tokens), 1)
        self.overlap_tokens = max(int(overlap_tokens), 0)

    def split(self, text: str) -> list[TextChunk]:
        source = str(text or "")
        if not source.strip():
            return []

        units = self._split_units(source)
        chunks: list[TextChunk] = []
        current_parts: list[str] = []
        current_start = 0
        current_titles: tuple[str, ...] = ()
        current_tokens = 0

        for unit_text, start, end, titles in units:
            unit_tokens = self.count_tokens(unit_text)
            if unit_tokens > self.max_chunk_tokens:
                if current_parts:
                    chunks.append(
                        self._make_chunk(chunks, current_parts, current_start, start, current_titles)
                    )
                    current_parts = []
                    current_tokens = 0
                chunks.extend(self._split_large_unit(unit_text, start, titles, len(chunks)))
                current_start = end
                current_titles = titles
                continue

            would_exceed = current_parts and current_tokens + unit_tokens > self.max_chunk_tokens
            if would_exceed:
                chunks.append(
                    self._make_chunk(chunks, current_parts, current_start, start, current_titles)
                )
                overlap = self._tail_overlap(chunks[-1].text)
                current_parts = [overlap, unit_text] if overlap else [unit_text]
                current_start = max(chunks[-1].end_char - len(overlap), 0) if overlap else start
                current_tokens = self.count_tokens("".join(current_parts))
                current_titles = titles
            else:
                if not current_parts:
                    current_start = start
                    current_titles = titles
                current_parts.append(unit_text)
                current_tokens += unit_tokens

        if current_parts:
            chunks.append(
                self._make_chunk(chunks, current_parts, current_start, len(source), current_titles)
            )

        return [
            TextChunk(
                chunk_id=i,
                text=chunk.text,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                title_path=chunk.title_path,
            )
            for i, chunk in enumerate(chunks)
            if chunk.text.strip()
        ]

    @classmethod
    def count_tokens(cls, text: str) -> int:
        return len(cls._TOKEN_RE.findall(str(text or "")))

    def _split_units(self, text: str) -> list[tuple[str, int, int, tuple[str, ...]]]:
        units: list[tuple[str, int, int, tuple[str, ...]]] = []
        headings: list[str] = []
        pos = 0
        for block in re.split(r"(\n\s*\n)", text):
            if not block:
                continue
            start = pos
            end = pos + len(block)
            pos = end
            if not block.strip():
                continue
            first_line = block.strip().splitlines()[0] if block.strip() else ""
            match = self._HEADING_RE.match(first_line)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()
                headings = headings[: level - 1] + [title]
            units.append((block, start, end, tuple(headings)))
        return units

    def _split_large_unit(
        self,
        text: str,
        absolute_start: int,
        titles: tuple[str, ...],
        chunk_id_start: int,
    ) -> list[TextChunk]:
        spans: list[tuple[str, int, int]] = []
        piece_start = 0
        for match in self._SENTENCE_RE.finditer(text):
            piece_end = match.end()
            piece = text[piece_start:piece_end]
            if piece.strip():
                spans.append((piece, piece_start, piece_end))
            piece_start = piece_end
        if piece_start < len(text):
            piece = text[piece_start:]
            if piece.strip():
                spans.append((piece, piece_start, len(text)))
        if not spans:
            spans = [(text, 0, len(text))]

        chunks: list[TextChunk] = []
        buf: list[str] = []
        buf_start = absolute_start
        token_count = 0

        for piece, local_start, local_end in spans:
            piece_start = absolute_start + local_start
            piece_end = absolute_start + local_end
            piece_tokens = self.count_tokens(piece)
            if piece_tokens > self.max_chunk_tokens:
                # Sentence exceeds threshold — flush buffer first, then keep the
                # sentence intact as an oversized chunk rather than hard-splitting.
                if buf:
                    chunks.append(
                        TextChunk(
                            chunk_id_start + len(chunks),
                            "".join(buf),
                            buf_start,
                            piece_start,
                            titles,
                        )
                    )
                    buf = []
                    token_count = 0
                chunks.append(
                    TextChunk(chunk_id_start + len(chunks), piece, piece_start, piece_end, titles)
                )
                buf_start = piece_end
                continue
            if buf and token_count + piece_tokens > self.max_chunk_tokens:
                chunk_text = "".join(buf)
                chunks.append(
                    TextChunk(
                        chunk_id_start + len(chunks),
                        chunk_text,
                        buf_start,
                        piece_start,
                        titles,
                    )
                )
                overlap = self._tail_overlap(chunk_text)
                buf = [overlap, piece] if overlap else [piece]
                buf_start = max(piece_start - len(overlap), absolute_start) if overlap else piece_start
                token_count = self.count_tokens("".join(buf))
            else:
                if not buf:
                    buf_start = piece_start
                buf.append(piece)
                token_count += piece_tokens

        if buf:
            chunks.append(
                TextChunk(
                    chunk_id_start + len(chunks),
                    "".join(buf),
                    buf_start,
                    absolute_start + spans[-1][2],
                    titles,
                )
            )
        return chunks

    def _hard_split(
        self,
        text: str,
        absolute_start: int,
        titles: tuple[str, ...],
        chunk_id_start: int,
    ) -> list[TextChunk]:
        tokens = list(self._TOKEN_RE.finditer(text))
        if not tokens:
            return []
        chunks: list[TextChunk] = []
        step = max(self.max_chunk_tokens - min(self.overlap_tokens, self.max_chunk_tokens // 2), 1)
        for token_start in range(0, len(tokens), step):
            token_end = min(token_start + self.max_chunk_tokens, len(tokens))
            start_char = tokens[token_start].start()
            end_char = tokens[token_end - 1].end()
            chunks.append(
                TextChunk(
                    chunk_id_start + len(chunks),
                    text[start_char:end_char],
                    absolute_start + start_char,
                    absolute_start + end_char,
                    titles,
                )
            )
            if token_end >= len(tokens):
                break
        return chunks

    def _tail_overlap(self, text: str) -> str:
        if self.overlap_tokens <= 0:
            return ""
        matches = list(self._TOKEN_RE.finditer(text))
        if len(matches) <= self.overlap_tokens:
            return text
        return text[matches[-self.overlap_tokens].start() :]

    @staticmethod
    def _make_chunk(
        chunks: list[TextChunk],
        parts: list[str],
        start: int,
        end: int,
        titles: tuple[str, ...],
    ) -> TextChunk:
        return TextChunk(len(chunks), "".join(parts), start, end, titles)


@dataclass
class _TripleRecord:
    source: str
    relation: str
    target: str
    confidence: float
    evidences: list[str] = field(default_factory=list)
    chunk_ids: set[int] = field(default_factory=set)
    mentions: int = 0
    order: int = 0


class TripleAccumulator:
    def __init__(self, *, min_confidence: float = 0.0) -> None:
        self.min_confidence = float(min_confidence)
        self._records: dict[str, _TripleRecord] = {}
        self.raw_count = 0
        self.skipped_count = 0
        self.entity_aliases: dict[str, list[str]] = {}
        self.relation_aliases: dict[str, list[str]] = {}
        for alias, canonical in _RELATION_ALIASES.items():
            self.relation_aliases.setdefault(canonical, []).append(alias)

    def register_alias_candidates(self, candidates: list[Any]) -> None:
        for item in candidates:
            if not isinstance(item, dict):
                continue
            canonical = self.clean_entity(item.get("canonical"))
            if not canonical:
                continue
            aliases = item.get("aliases")
            if not isinstance(aliases, list):
                continue
            key = self.normalize_entity(canonical)
            bucket = self.entity_aliases.setdefault(canonical, [])
            for alias in aliases:
                cleaned = self.clean_entity(alias)
                if not cleaned or self.normalize_entity(cleaned) == key or cleaned in bucket:
                    continue
                bucket.append(cleaned)

    def add_triples(self, triples: list[dict[str, Any]], *, chunk_id: int | None = None) -> None:
        for item in triples:
            self.raw_count += 1
            if not isinstance(item, dict):
                self.skipped_count += 1
                continue

            source = self.clean_entity(item.get("source"))
            target = self.clean_entity(item.get("target"))
            if not source or not target:
                self.skipped_count += 1
                continue

            relation = self.normalize_relation(item.get("relation") or "related_to")
            confidence = self._confidence(item.get("confidence"))
            if confidence < self.min_confidence:
                self.skipped_count += 1
                continue

            source = self._canonical_entity(source)
            target = self._canonical_entity(target)
            source_key = self.normalize_entity(source)
            target_key = self.normalize_entity(target)
            key = f"{source_key}\t{relation}\t{target_key}"
            record = self._records.get(key)
            if record is None:
                record = _TripleRecord(
                    source=source,
                    relation=relation,
                    target=target,
                    confidence=confidence,
                    order=len(self._records),
                )
                self._records[key] = record
            else:
                record.confidence = max(record.confidence, confidence)
            evidence = str(item.get("evidence") or "").strip()
            if evidence and evidence not in record.evidences and len(record.evidences) < 3:
                record.evidences.append(evidence)
            if chunk_id is not None:
                self._add_chunk_id(record, chunk_id)
            else:
                raw_chunk_ids = item.get("chunk_ids")
                if isinstance(raw_chunk_ids, list):
                    for raw_id in raw_chunk_ids:
                        self._add_chunk_id(record, raw_id)
                elif "chunk_id" in item:
                    self._add_chunk_id(record, item.get("chunk_id"))
            record.mentions += 1

    def triples(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in sorted(self._records.values(), key=lambda row: row.order):
            chunk_ids = sorted(record.chunk_ids)
            evidence = "\n---\n".join(record.evidences)
            row: dict[str, Any] = {
                "source": record.source,
                "relation": record.relation,
                "target": record.target,
                "confidence": round(record.confidence, 4),
                "evidence": evidence,
                "chunk_ids": chunk_ids,
                "mentions": record.mentions,
            }
            if chunk_ids:
                row["chunk_id"] = chunk_ids[0]
            rows.append(row)
        return rows

    @property
    def deduped_count(self) -> int:
        return len(self._records)

    @property
    def unique_node_count(self) -> int:
        nodes: set[str] = set()
        for record in self._records.values():
            nodes.add(self.normalize_entity(record.source))
            nodes.add(self.normalize_entity(record.target))
        return len(nodes)

    @staticmethod
    def clean_entity(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip(_WRAPPER_CHARS + " \t\r\n")
        return text.strip()

    @classmethod
    def normalize_entity(cls, value: Any) -> str:
        text = cls.clean_entity(value)
        return text.lower()

    @staticmethod
    def normalize_relation(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "related_to"))
        text = re.sub(r"\s+", " ", text).strip(_WRAPPER_CHARS + " \t\r\n").lower()
        if not text:
            return "related_to"
        if text in _RELATION_ALIASES:
            return _RELATION_ALIASES[text]
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text, flags=re.UNICODE)
        text = re.sub(r"_+", "_", text).strip("_")
        return text or "related_to"

    def _canonical_entity(self, value: str) -> str:
        normalized = self.normalize_entity(value)
        for canonical, aliases in self.entity_aliases.items():
            if normalized == self.normalize_entity(canonical):
                return canonical
            if any(normalized == self.normalize_entity(alias) for alias in aliases):
                return canonical
        return value

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            conf = float(value)
        except (TypeError, ValueError):
            return 1.0
        if conf < 0:
            return 0.0
        if conf > 1:
            return 1.0
        return conf

    @staticmethod
    def _add_chunk_id(record: _TripleRecord, value: Any) -> None:
        try:
            record.chunk_ids.add(int(value))
        except (TypeError, ValueError):
            return


class TextGraphExtractionSubAgent(SubAgent):
    name = "text_graph_extractor"
    system_prompt = TEXT_GRAPH_EXTRACTION_SYSTEM_PROMPT
    default_max_tokens = 8192
    default_temperature = 0.2

    def __init__(
        self,
        llm_factory: Callable[[str], Awaitable[str]] | None = None,
        *,
        provider_gateway: Any | None = None,
    ) -> None:
        super().__init__(provider_gateway=provider_gateway, prompt_invoker=llm_factory)

    async def extract(
        self,
        text: str,
        *,
        directed: bool = True,
        max_chunk_tokens: int = 1000,
        overlap_tokens: int = 150,
        extraction_goal: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        max_concurrency: int = 8,
    ) -> TextGraphExtractionResult:
        if self._provider_gateway is None and self._prompt_invoker is None:
            raise RuntimeError("text_graph_extractor_llm_unavailable")

        effective_goal = extraction_goal.strip() or "抽取文本中明确陈述的实体关系。"
        chunker = TextChunker(max_chunk_tokens=max_chunk_tokens, overlap_tokens=overlap_tokens)
        chunks = chunker.split(text)
        total = len(chunks)
        accumulator = TripleAccumulator()
        warnings: list[str] = []
        completed = 0
        sem = asyncio.Semaphore(max_concurrency)

        async def _process_chunk(chunk: TextChunk) -> None:
            nonlocal completed
            prompt = self._render_chunk_prompt(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                title_path=chunk.title_path,
                directed=directed,
                extraction_goal=effective_goal,
            )
            chunk_triples: list[dict[str, Any]] | None = None
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    async with sem:
                        response = await self.invoke_text(prompt)
                    chunk_triples = self._parse_line_response(response)
                    # None  = LLM 明确说无实体，信任，停止重试
                    # []    = 解析失败（格式错），温度>0 可重试
                    # [..] = 成功
                    if chunk_triples is None or chunk_triples or attempt >= 2:
                        break
                except Exception as exc:
                    last_exc = exc
                    if _is_timeout_error(exc) or not _is_retryable_error(exc):
                        break
            else:
                warnings.append(
                    f"chunk {chunk.chunk_id} 抽取失败（重试 3 次）: "
                    f"{type(last_exc).__name__}: {last_exc}"
                )

            if chunk_triples is not None:
                normalized_triples: list[dict[str, Any]] = [
                    {**t, "chunk_id": chunk.chunk_id} for t in chunk_triples
                ]
                accumulator.add_triples(normalized_triples, chunk_id=chunk.chunk_id)

            completed += 1
            if progress_callback is not None:
                progress_callback({
                    "kind": "text2graph_chunk",
                    "chunk": completed,
                    "chunks": total,
                    "progress": completed / total if total > 0 else 1.0,
                    "nodes": accumulator.unique_node_count,
                    "edges": accumulator.deduped_count,
                    "message": f"chunk {completed}/{total}",
                    "extraction_goal": effective_goal,
                })

        await asyncio.gather(*(_process_chunk(chunk) for chunk in chunks))

        return TextGraphExtractionResult(
            triples=accumulator.triples(),
            raw_triple_count=accumulator.raw_count,
            deduped_triple_count=accumulator.deduped_count,
            skipped_count=accumulator.skipped_count,
            chunk_count=len(chunks),
            entity_aliases=accumulator.entity_aliases,
            relation_aliases=accumulator.relation_aliases,
            warnings=self._dedupe_warnings(warnings),
        )

    @staticmethod
    def _render_chunk_prompt(
        *,
        chunk_id: int,
        text: str,
        title_path: tuple[str, ...],
        directed: bool,
        extraction_goal: str,
    ) -> str:
        title = " > ".join(title_path) if title_path else "(无标题路径)"
        direction = "关系按文本语义方向（主体→客体）输出。" if directed else "即使最终图为无向图，仍按语义方向输出。"
        return f"""\
请从当前 chunk 抽取实体关系三元组。

chunk_id: {chunk_id}
标题路径: {title}
抽取目标: {extraction_goal}
方向要求: {direction}

输出格式示例：
<triples>
苹果公司 | CEO | 蒂姆·库克
苹果公司 | 总部位于 | 加利福尼亚州
</triples>

无实体时（英文模型可用 none）：
<triples>
无实体
</triples>

当前 chunk:
<<<TEXT
{text}
TEXT
>>>
"""

    _EMPTY_CONTENT = re.compile(
        r"^(无实体|无三元组|none|null|empty|no\s+triples?)$",
        re.IGNORECASE,
    )
    # <triple> / <triples> 均接受（模型常见手误）
    _OPEN_TAG = re.compile(r"<triples?\b[^>]*>", re.IGNORECASE)
    _CLOSE_TAG = re.compile(r"</triples?>", re.IGNORECASE)
    # 行首列表前缀：`- `、`* `、`1. `、`(1) ` 等
    _LIST_PREFIX = re.compile(r"^[\-\*•●]|\d+[\.\)、]\s*")

    @classmethod
    def _parse_line_response(cls, text: str) -> list[dict[str, Any]] | None:
        """提取三元组。
        返回 None  = LLM 明确声明无实体（信任，不重试）
        返回 []    = 解析失败，无法提取（可重试）
        返回 [...] = 成功
        四级降级：完整块 → 只有开标签 → 只有闭标签 → 全文扫描。
        """
        opens = [m.end() for m in cls._OPEN_TAG.finditer(text)]
        closes = [m.start() for m in cls._CLOSE_TAG.finditer(text)]

        if opens and closes:
            segments: list[str] = []
            close_idx = 0
            for open_pos in opens:
                while close_idx < len(closes) and closes[close_idx] <= open_pos:
                    close_idx += 1
                if close_idx < len(closes):
                    segments.append(text[open_pos:closes[close_idx]])
                    close_idx += 1
            if segments:
                return cls._parse_lines("\n".join(segments))

        if opens:
            return cls._parse_lines(text[opens[-1]:])

        if closes:
            return cls._parse_lines(text[:closes[0]])

        # 无标签：全文扫描，但若 LLM 明确说无实体则返回 None
        return cls._parse_lines(text)

    @classmethod
    def _parse_lines(cls, text: str) -> list[dict[str, Any]] | None:
        """逐行解析。
        若所有有效行均为"无实体"声明（无任何三元组行），返回 None。
        解析失败（有内容但格式不对）返回 []。
        """
        triples: list[dict[str, Any]] = []
        has_empty_decl = False
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("`").strip()
            if not line or line.startswith("#") or line.startswith("<"):
                continue
            if cls._EMPTY_CONTENT.match(line):
                has_empty_decl = True
                continue
            line = cls._LIST_PREFIX.sub("", line, count=1).strip()
            if not line:
                continue
            if "|" in line or "｜" in line:
                parts = [p.strip() for p in re.split(r"\s*[|｜]\s*", line)]
            elif "\t" in line:
                parts = [p.strip() for p in line.split("\t")]
            else:
                continue
            if len(parts) >= 3 and all(parts[:3]):
                triples.append({"source": parts[0], "relation": parts[1], "target": parts[2]})
        if triples:
            return triples
        # 没有三元组：区分"明确说无实体"和"解析失败"
        return None if has_empty_decl else []

    @staticmethod
    def _dedupe_warnings(warnings: list[str]) -> list[str]:
        seen: set[str] = set()
        rows: list[str] = []
        for item in warnings:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            rows.append(text)
        return rows
