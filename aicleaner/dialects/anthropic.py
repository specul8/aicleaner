"""Anthropic Messages API (/v1/messages) streaming rewriter.

Fragments arrive as content_block_delta events whose delta carries one of:
  text_delta.text | thinking_delta.thinking | input_json_delta.partial_json
Each content block index gets its own TailRewriter, flushed at
content_block_stop. Everything else is rehydrated leaf-by-leaf. A tool_use
block whose name matches keep_tokenised_tools is passed through untouched so
remote MCP servers never receive real identifiers.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Iterable

from ..mapping import Mapper
from ..sse import SSEEvent, TailRewriter, make_event
from ..transform import rehydrate_json, tool_is_kept
from . import GenericStreamRewriter

DELTA_FIELDS = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "input_json_delta": "partial_json",
}


class AnthropicStreamRewriter(GenericStreamRewriter):
    def __init__(self, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = (),
                 mark: bool = False):
        super().__init__(mapper, counter, keep_patterns, mark=mark)
        # index -> (delta_type, TailRewriter)
        self._blocks: dict[int, tuple[str, TailRewriter]] = {}
        self._kept_indexes: set[int] = set()
        self._arguments: dict[int, list[str]] = {}  # index -> a tool call's restored arguments so far

    def _rewriter_for(self, index: int, delta_type: str) -> TailRewriter:
        entry = self._blocks.get(index)
        if entry is None or entry[0] != delta_type:
            # partial_json is JSON text: a real containing a quote must go in escaped.
            if delta_type == "input_json_delta":
                restored = self._arguments.setdefault(index, [])

                def rewrite(s: str) -> str:
                    out = self.mapper.rehydrate_json_text(s, self.counter)
                    restored.append(out)
                    return out
            else:
                # Only text a person reads is marked: never thinking, never tool arguments.
                marked = self.mark and delta_type == "text_delta"
                rewrite = lambda s: self.mapper.rehydrate(s, self.counter, mark=marked)
            tr = TailRewriter(self.mapper.stream_re, rewrite, self.mapper.stream_hold)
            self._blocks[index] = (delta_type, tr)
            return tr
        return entry[1]

    def _flush_block(self, index: int) -> list[SSEEvent]:
        entry = self._blocks.pop(index, None)
        if entry is None:
            return []
        delta_type, tr = entry
        rest = tr.flush()
        arguments = self._arguments.pop(index, None)
        if arguments is not None:
            # The whole call is restored now; anything token-shaped left in it will reach a tool.
            self.fragments.extend(self.mapper.token_fragments("".join(arguments)))
        if not rest:
            return []
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": delta_type, DELTA_FIELDS[delta_type]: rest},
        }
        return [make_event("content_block_delta", json.dumps(payload, ensure_ascii=False))]

    def on_event(self, ev: SSEEvent) -> list[SSEEvent]:
        data = ev.data
        if data is None:
            return [ev]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return [ev.with_data(self.mapper.rehydrate(data, self.counter))]

        etype = obj.get("type")
        if etype == "content_block_start":
            block = obj.get("content_block") or {}
            if block.get("type") == "tool_use" and tool_is_kept(block.get("name"), self.keep_patterns):
                self._kept_indexes.add(int(obj.get("index", 0)))
                self.kept += 1
                return [ev]  # name and id carry no client content; leave the event as-is
            # A thinking block goes through untouched, for the same reason its signature does:
            # the provider signed the tokenised text and the client resends the block, so
            # rehydrating it here is what makes the resend invalid (D8). The `kept` count is not
            # incremented - that counts kept TOOLS, and widening it would quietly change what
            # the evidence log means.
            if block.get("type") in ("thinking", "redacted_thinking"):
                self._kept_indexes.add(int(obj.get("index", 0)))
                return [ev]
        if etype == "content_block_delta" and int(obj.get("index", 0)) in self._kept_indexes:
            return [ev]  # arguments for a kept tool stay tokenised
        if etype == "content_block_delta":
            delta = obj.get("delta") or {}
            dtype = delta.get("type")
            field_name = DELTA_FIELDS.get(dtype)
            if field_name and isinstance(delta.get(field_name), str):
                tr = self._rewriter_for(int(obj.get("index", 0)), dtype)
                emitted = tr.feed(delta[field_name])
                if emitted == "":
                    return []  # nothing safe to emit yet; drop this delta
                delta[field_name] = emitted
                return [ev.with_data(json.dumps(obj, ensure_ascii=False))]
            return [ev.with_data(json.dumps(rehydrate_json(self.mapper, obj, self.counter), ensure_ascii=False))]

        if etype == "content_block_stop":
            index = int(obj.get("index", 0))
            self._kept_indexes.discard(index)
            flushed = self._flush_block(index)
            return flushed + [ev]

        return [ev.with_data(json.dumps(rehydrate_json(self.mapper, obj, self.counter), ensure_ascii=False))]

    def finish(self) -> list[SSEEvent]:
        out: list[SSEEvent] = []
        for index in sorted(self._blocks):
            out.extend(self._flush_block(index))
        return out
