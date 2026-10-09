"""Dialects know where streamed text lives for a given API family.

Non-streaming bodies need no dialect: every string leaf is rewritten.
Streaming bodies do, because text arrives in fragments and a token may be
split across them; each dialect knows which fields are fragments and when a
fragment sequence ends so the tail buffer can be flushed.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..mapping import Mapper
from ..sse import SSEEvent
from ..transform import rehydrate_json


class GenericStreamRewriter:
    """Rehydrate every string in every event; no tail buffering.

    Used for dialects we do not model. A token split across two events would
    be missed, which is why known dialects get a real rewriter.
    """

    def __init__(self, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = (),
                 mark: bool = False):
        self.mapper = mapper
        self.counter = counter
        self.keep_patterns = tuple(keep_patterns)
        self.kept = 0  # tool_use blocks left tokenised
        # Wrap restored names in assistant text for the reader. A dialect we do not model cannot
        # tell text from arguments, so the generic rewriter never marks anything.
        self.mark = mark
        # Pieces of tokens still in a tool call's restored arguments (the D87 safety net). Only the
        # Anthropic rewriter sees whole tool calls; everywhere else this stays empty.
        self.fragments: list[str] = []

    def _rewrite_data(self, data: str) -> str:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return self.mapper.rehydrate(data, self.counter)
        return json.dumps(rehydrate_json(self.mapper, obj, self.counter), ensure_ascii=False)

    def on_event(self, ev: SSEEvent) -> list[SSEEvent]:
        data = ev.data
        if data is None:
            return [ev]
        return [ev.with_data(self._rewrite_data(data))]

    def finish(self) -> list[SSEEvent]:
        return []


def get_stream_rewriter(
    dialect: str, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = (),
    mark: bool = False,
) -> Any:
    if dialect == "anthropic":
        from .anthropic import AnthropicStreamRewriter

        return AnthropicStreamRewriter(mapper, counter, keep_patterns, mark=mark)
    if dialect == "openai":
        from .openai_chat import OpenAIChatStreamRewriter

        return OpenAIChatStreamRewriter(mapper, counter, keep_patterns)
    return GenericStreamRewriter(mapper, counter, keep_patterns)


KNOWN_DIALECTS = ("anthropic", "openai")
