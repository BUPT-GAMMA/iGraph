"""Bounded, user-grounded working memory for one Playground conversation."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

MAX_NOTES = 32
RECENT_MESSAGES = 6
HISTORY_CHARS = 14000
NoteText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Kind = Literal["goal", "graph_fact", "constraint", "question_choice"]


class ScenarioNote(BaseModel):
    kind: Kind
    quote: NoteText
    # Only text from the displayed proposal can be attached to the user's own words.
    question: str | None = Field(default=None, max_length=400)


class NoteSelection(BaseModel):
    kind: Kind
    source: int = Field(ge=1)
    previous_question: int | None = Field(default=None, ge=1, le=6)


def source_excerpts(user_messages):
    """Give the planner numbered source text; never let it write its own durable facts."""
    excerpts = []
    for message in user_messages:
        for sentence in re.split(r"(?<=[。！？；!?;])|(?<=\.)\s+|[\r\n]+", message):
            text = sentence.strip()
            while text:
                end = min(200, len(text))
                if len(text) > end:
                    boundary = max(text.rfind(mark, 0, end) for mark in (" ", "，", ",", "、"))
                    if boundary >= end // 2:
                        end = boundary + 1
                excerpts.append(text[:end].strip())
                text = text[end:].strip()
    return list(dict.fromkeys(excerpts))


class ContextUpdate(BaseModel):
    # IDs refer to the numbered notes in this turn's input; the store serializes turns.
    retire: list[Annotated[int, Field(ge=1)]] = Field(max_length=MAX_NOTES)
    remember: list[NoteSelection] = Field(max_length=MAX_NOTES)

    def apply(self, previous: list[ScenarioNote], excerpts: list[str], questions: list[str]) -> list[ScenarioNote]:
        retired = set(self.retire)
        if any(index > len(previous) for index in retired):
            raise ValueError("Retire only existing active note IDs")
        remaining = [note for index, note in enumerate(previous, 1) if index not in retired]
        for selection in self.remember:
            if selection.source > len(excerpts):
                raise ValueError("Select an existing source ID from user_excerpts")
            question = None
            if selection.previous_question is not None:
                if selection.kind != "question_choice" or selection.previous_question > len(questions):
                    raise ValueError("Link only a question_choice to an existing displayed question number")
                question = questions[selection.previous_question - 1]
            note = ScenarioNote(kind=selection.kind, quote=excerpts[selection.source - 1], question=question)
            if note not in remaining:
                remaining.append(note)
        if len(remaining) > MAX_NOTES:
            raise ValueError(f"Consolidate the working memory to at most {MAX_NOTES} active notes")
        return remaining


def planning_history(messages, language, *, bootstrap):
    """Pin the opening request, include recent numbered proposals, and recover older user input on upgrade."""
    past = messages[:-1]  # The current request is supplied separately and in full.
    if not past:
        return []
    recent_start = max(0, len(past) - RECENT_MESSAGES)
    priority = [0, *reversed(range(recent_start, len(past)))]
    if bootstrap:
        priority.extend(i for i in reversed(range(1, recent_start)) if past[i]["role"] == "user")
    selected = {}
    budget = HISTORY_CHARS
    for index in dict.fromkeys(priority):
        message = past[index]
        content = message["text"]
        if message.get("questions"):
            questions = [f"{i}. {q[language]}" for i, q in enumerate(message["questions"], 1)]
            content += "\n" + "\n".join(questions)
        if len(selected) >= 12:
            break
        if len(content) > budget:
            continue
        selected[index] = {"role": message["role"], "content": content}
        budget -= len(content)
    return [selected[index] for index in sorted(selected)]
