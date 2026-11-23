"""Bounded, user-grounded working memory for one Playground conversation."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

MAX_NOTES = 32
RECENT_MESSAGES = 6
HISTORY_CHARS = 14000
NoteText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ScenarioNote(BaseModel):
    kind: Literal["goal", "graph_fact", "constraint", "question_choice"]
    text: NoteText
    quote: NoteText


class ContextUpdate(BaseModel):
    # IDs refer to the numbered notes in this turn's input; the store serializes turns.
    retire: list[Annotated[int, Field(ge=1)]] = Field(max_length=MAX_NOTES)
    remember: list[ScenarioNote] = Field(max_length=MAX_NOTES)

    def apply(self, previous: list[ScenarioNote], user_messages: list[str]) -> list[ScenarioNote]:
        retired = set(self.retire)
        if any(index > len(previous) for index in retired):
            raise ValueError("Retire only existing active note IDs")
        def normalize(text):
            return "".join(text.casefold().split())

        sources = [normalize(text) for text in user_messages]
        sources.extend(normalize(previous[index - 1].quote) for index in retired)
        remaining = [note for index, note in enumerate(previous, 1) if index not in retired]
        for note in self.remember:
            if not any(normalize(note.quote) in source for source in sources):
                raise ValueError("Each new note needs a short verbatim quote from an eligible user message")
            if not any(old.kind == note.kind and old.text == note.text for old in remaining):
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
