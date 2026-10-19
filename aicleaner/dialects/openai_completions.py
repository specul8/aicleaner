"""OpenAI's legacy Completions (/v1/engines/<model>/completions) streaming rewriter - Copilot's inline
completions (2026-10-19; spec 2026-10-18-copilot-filtering-design.md, release 5).

Fragments live in choices[].text. A choice's buffer is flushed into the chunk that carries its
finish_reason; anything still held at "data: [DONE]" goes out in one chunk before the DONE marker.
The text becomes code in the person's editor, so restored names are never marked.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..mapping import Mapper
from ..sse import SSEEvent, TailRewriter, make_event
from ..transform import walk_strings
from . import GenericStreamRewriter


class OpenAICompletionsStreamRewriter(GenericStreamRewriter):
    def __init__(self, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = ()):
        super().__init__(mapper, counter, keep_patterns)
        self._buffers: dict[int, TailRewriter] = {}
        self._template: dict[str, Any] | None = None

    def _rewriter_for(self, index: int) -> TailRewriter:
        tr = self._buffers.get(index)
        if tr is None:
            tr = TailRewriter(self.mapper.stream_re, lambda s: self.mapper.rehydrate(s, self.counter),
                              self.mapper.stream_hold)
            self._buffers[index] = tr
        return tr

    def _rehydrate_leaf(self, s: str) -> str:
        return self.mapper.rehydrate(s, self.counter)

    def _process_choice(self, choice: dict[str, Any]) -> None:
        index = int(choice.get("index", 0))
        text = choice.get("text")
        if isinstance(text, str):
            choice["text"] = self._rewriter_for(index).feed(text)
        if choice.get("finish_reason") is not None and index in self._buffers:
            choice["text"] = (choice.get("text") or "") + self._buffers.pop(index).flush()

    def on_event(self, ev: SSEEvent) -> list[SSEEvent]:
        data = ev.data
        if data is None:
            return [ev]
        if data.strip() == "[DONE]":
            return self.finish() + [ev]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return [ev.with_data(self._rehydrate_leaf(data))]
        if not isinstance(obj, dict) or not isinstance(obj.get("choices"), list):
            return [ev.with_data(json.dumps(walk_strings(obj, self._rehydrate_leaf), ensure_ascii=False))]
        self._template = {k: obj[k] for k in ("id", "object", "created", "model") if k in obj}
        for choice in obj["choices"]:
            if isinstance(choice, dict):
                self._process_choice(choice)
        return [ev.with_data(json.dumps(obj, ensure_ascii=False))]

    def finish(self) -> list[SSEEvent]:
        held = {index: tr.flush() for index, tr in sorted(self._buffers.items())}
        self._buffers.clear()
        choices = [{"index": index, "text": rest, "finish_reason": None} for index, rest in held.items() if rest]
        if not choices:
            return []
        chunk = dict(self._template or {})
        chunk["choices"] = choices
        return [make_event(None, json.dumps(chunk, ensure_ascii=False))]
