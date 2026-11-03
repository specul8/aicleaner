"""OpenAI Chat Completions (/v1/chat/completions) streaming rewriter.

Fragments live in choices[].delta.content and
choices[].delta.tool_calls[].function.arguments. A choice's buffers are
flushed into the chunk that carries its finish_reason; anything still held
at "data: [DONE]" goes out in one synthetic chunk before the DONE marker.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..mapping import Mapper
from ..sse import SSEEvent, TailRewriter, make_event
from ..transform import tool_is_kept, walk_strings
from . import GenericStreamRewriter

Key = tuple[int, str, int]  # (choice index, "content" | "args", tool_call index)


class OpenAIChatStreamRewriter(GenericStreamRewriter):
    def __init__(self, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = ()):
        super().__init__(mapper, counter, keep_patterns)
        self._buffers: dict[Key, TailRewriter] = {}
        self._template: dict[str, Any] | None = None
        self._kept_calls: set[Key] = set()

    def _rewriter_for(self, key: Key) -> TailRewriter:
        tr = self._buffers.get(key)
        if tr is None:
            # Tool-call arguments are JSON text: a real containing a quote must go in escaped.
            rewrite = self.mapper.rehydrate_json_text if key[1] == "args" else self.mapper.rehydrate
            tr = TailRewriter(self.mapper.stream_re, lambda s: rewrite(s, self.counter), self.mapper.stream_hold)
            self._buffers[key] = tr
        return tr

    def _rehydrate_leaf(self, s: str) -> str:
        return self.mapper.rehydrate(s, self.counter)

    def _process_choice(self, choice: dict[str, Any]) -> None:
        ci = int(choice.get("index", 0))
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
            choice["delta"] = delta
        content = delta.get("content")
        if isinstance(content, str):
            delta["content"] = self._rewriter_for((ci, "content", 0)).feed(content)
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            ti = int(tc.get("index", 0))
            key: Key = (ci, "args", ti)
            if isinstance(fn.get("name"), str) and tool_is_kept(fn["name"], self.keep_patterns):
                self._kept_calls.add(key)
                self.kept += 1
            if key in self._kept_calls:
                continue  # arguments for a kept tool stay tokenised
            args = fn.get("arguments")
            if isinstance(args, str):
                fn["arguments"] = self._rewriter_for(key).feed(args)
        if choice.get("finish_reason") is not None:
            self._flush_choice(ci, delta)

    def _flush_choice(self, ci: int, delta: dict[str, Any]) -> None:
        for key in [k for k in self._buffers if k[0] == ci]:
            rest = self._buffers.pop(key).flush()
            if not rest:
                continue
            if key[1] == "content":
                delta["content"] = (delta.get("content") or "") + rest
            else:
                calls = delta.setdefault("tool_calls", [])
                calls.append({"index": key[2], "function": {"arguments": rest}})

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
        if not isinstance(obj, dict) or "choices" not in obj:
            return [ev.with_data(json.dumps(walk_strings(obj, self._rehydrate_leaf), ensure_ascii=False))]
        self._template = {k: obj[k] for k in ("id", "object", "created", "model") if k in obj}
        for choice in obj.get("choices") or []:
            if isinstance(choice, dict):
                self._process_choice(choice)
        return [ev.with_data(json.dumps(obj, ensure_ascii=False))]

    def finish(self) -> list[SSEEvent]:
        if not self._buffers:
            return []
        choices: dict[int, dict[str, Any]] = {}
        for key in sorted(self._buffers):
            delta = choices.setdefault(key[0], {})
            self._flush_choice_key(key, delta)
        chunk = dict(self._template or {"object": "chat.completion.chunk"})
        chunk["choices"] = [
            {"index": ci, "delta": delta, "finish_reason": None} for ci, delta in choices.items()
        ]
        return [make_event(None, json.dumps(chunk, ensure_ascii=False))]

    def _flush_choice_key(self, key: Key, delta: dict[str, Any]) -> None:
        rest = self._buffers.pop(key).flush()
        if not rest:
            return
        if key[1] == "content":
            delta["content"] = (delta.get("content") or "") + rest
        else:
            delta.setdefault("tool_calls", []).append(
                {"index": key[2], "function": {"arguments": rest}}
            )
