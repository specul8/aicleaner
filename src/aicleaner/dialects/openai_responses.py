"""OpenAI Responses (/responses) streaming rewriter - GitHub Copilot's chat and agent (2026-10-18).

Fragments arrive as `response.<kind>.delta` events, one stream per item and part:
output_text (per content_index), reasoning_summary_text (per summary_index),
function_call_arguments and custom_tool_call_input. Each is closed by its `.done` event, which
carries the whole value again, as do output_item.done and response.completed.

Responses events are numbered (`sequence_number`), so nothing is invented if it can be helped: each
delta event is held back by exactly one event, and what a buffer still holds when its .done arrives
is added to that held delta. Only when another item's event came between does the tail go out as one
more delta, just before its .done. Whole values (the .done events, output_item.done, completed) are
restored in place.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..mapping import Mapper
from ..sse import SSEEvent, TailRewriter, make_event
from ..transform import rehydrate_json, tool_is_kept
from . import GenericStreamRewriter

# The delta kinds, and which index tells one part of an item from another.
KINDS = {
    "response.output_text": "content_index",
    "response.reasoning_summary_text": "summary_index",
    "response.function_call_arguments": "",
    "response.custom_tool_call_input": "",
}
ARGS = "response.function_call_arguments"       # JSON text: a real with a quote must go in escaped

Key = tuple[str, str, int]  # (kind, item_id, part index)


class OpenAIResponsesStreamRewriter(GenericStreamRewriter):
    def __init__(self, mapper: Mapper, counter: Counter[str], keep_patterns: Iterable[str] = ()):
        super().__init__(mapper, counter, keep_patterns)
        self._buffers: dict[Key, TailRewriter] = {}
        self._held: tuple[SSEEvent, dict[str, Any], Key] | None = None
        self._kept_items: set[str] = set()       # function calls of a kept tool: arguments stay tokenised

    # ---- pieces ----------------------------------------------------------------------------------

    def _rewriter_for(self, key: Key) -> TailRewriter:
        tr = self._buffers.get(key)
        if tr is None:
            rewrite = self.mapper.rehydrate_json_text if key[0] == ARGS else self.mapper.rehydrate
            tr = TailRewriter(self.mapper.stream_re, lambda s: rewrite(s, self.counter), self.mapper.stream_hold)
            self._buffers[key] = tr
        return tr

    @staticmethod
    def _key(kind: str, obj: dict[str, Any]) -> Key:
        index = KINDS[kind]
        return (kind, str(obj.get("item_id") or ""), int(obj.get(index, 0) or 0) if index else 0)

    def _flush(self, keys: list[Key]) -> list[SSEEvent]:
        """What these buffers still hold, out before the event that ends them: added to the held delta
        when it is the same part, otherwise as one more delta of its own."""
        out: list[SSEEvent] = []
        for key in keys:
            rest = self._buffers.pop(key).flush() if key in self._buffers else ""
            if not rest:
                continue
            if self._held is not None and self._held[2] == key:
                self._held[1]["delta"] += rest
                continue
            out += self._release()
            index = KINDS[key[0]]
            out.append(make_event(f"{key[0]}.delta", json.dumps(
                {"type": f"{key[0]}.delta", "item_id": key[1], **({index: key[2]} if index else {}), "delta": rest},
                ensure_ascii=False)))
        return out

    def _release(self) -> list[SSEEvent]:
        if self._held is None:
            return []
        ev, obj, _key = self._held
        self._held = None
        return [ev.with_data(json.dumps(obj, ensure_ascii=False))]

    def _whole(self, ev: SSEEvent, obj: Any) -> SSEEvent:
        """A whole value restored in place - but a kept tool's arguments left as they came."""
        kept = self._kept_arguments(obj)
        restored = rehydrate_json(self.mapper, obj, self.counter)
        self._put_back_arguments(restored, kept)
        return ev.with_data(json.dumps(restored, ensure_ascii=False))

    def _items_in(self, obj: Any) -> list[dict[str, Any]]:
        if not isinstance(obj, dict):
            return []
        found = [obj["item"]] if isinstance(obj.get("item"), dict) else []
        response = obj.get("response")
        if isinstance(response, dict):
            found += [i for i in response.get("output") or [] if isinstance(i, dict)]
        return found

    def _kept_arguments(self, obj: Any) -> dict[str, Any]:
        """item id -> its arguments as they came, for each kept tool's call in this event."""
        return {str(item.get("id")): item.get("arguments") for item in self._items_in(obj)
                if item.get("id") in self._kept_items and "arguments" in item}

    def _put_back_arguments(self, obj: Any, kept: dict[str, Any]) -> None:
        for item in self._items_in(obj):
            if str(item.get("id")) in kept:
                item["arguments"] = kept[str(item.get("id"))]

    # ---- events ----------------------------------------------------------------------------------

    def on_event(self, ev: SSEEvent) -> list[SSEEvent]:
        data = ev.data
        if data is None:
            return self._release() + [ev]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return self._release() + [ev.with_data(self.mapper.rehydrate(data, self.counter))]
        kind_of = str(obj.get("type") or "") if isinstance(obj, dict) else ""
        kind, _dot, step = kind_of.rpartition(".")

        if kind_of == "response.output_item.added" and isinstance(obj.get("item"), dict):
            item = obj["item"]
            if isinstance(item.get("name"), str) and tool_is_kept(item["name"], self.keep_patterns):
                self._kept_items.add(str(item.get("id") or ""))
                self.kept += 1

        if kind in KINDS and step == "delta" and isinstance(obj.get("delta"), str):
            key = self._key(kind, obj)
            out = self._release()
            if key[1] in self._kept_items:
                return out + [ev]
            obj["delta"] = self._rewriter_for(key).feed(obj["delta"])
            self._held = (ev, obj, key)
            return out

        if kind in KINDS and step == "done":
            key = self._key(kind, obj)
            out = self._flush([key])                   # another item's event between: one more delta
            out += self._release()
            if key[1] in self._kept_items:
                return out + [ev]
            if kind == ARGS and isinstance(obj.get("arguments"), str):
                obj["arguments"] = self.mapper.rehydrate_json_text(obj["arguments"], self.counter)
                return out + [ev.with_data(json.dumps(obj, ensure_ascii=False))]
            return out + [self._whole(ev, obj)]

        # An item finished, or the whole response (completed, or cut short): nothing of theirs may stay
        # in a buffer - a part whose .done never came still gives back every word, before this event.
        ending: list[Key] = []
        if kind_of == "response.output_item.done" and isinstance(obj.get("item"), dict):
            ending = [k for k in self._buffers if k[1] == str(obj["item"].get("id") or "")]
        elif kind_of in ("response.completed", "response.incomplete", "response.failed"):
            ending = list(self._buffers)
        out = self._flush(ending)
        return out + self._release() + [self._whole(ev, obj)]

    def finish(self) -> list[SSEEvent]:
        out = []
        if self._held is not None:
            ev, obj, key = self._held
            if key in self._buffers:
                obj["delta"] += self._buffers.pop(key).flush()
            out += self._release()
        for key in list(self._buffers):
            rest = self._buffers.pop(key).flush()
            if rest:
                out.append(make_event(f"{key[0]}.delta", json.dumps(
                    {"type": f"{key[0]}.delta", "item_id": key[1], "delta": rest}, ensure_ascii=False)))
        return out
