"""OpenAI Responses API compatibility helpers for grokcli-2api.

sub2api (platform=openai, Anthropic inbound /v1/messages) forwards to upstream
as POST /v1/responses with stream=true. This module converts:

  Responses request  →  OpenAI chat/completions body
  chat completion    →  Responses object / SSE events

We intentionally keep the surface small: enough for Claude Code via sub2api
(text + function tools + usage + terminal response.completed).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def new_output_item_id(prefix: str = "msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _content_parts_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("text") is not None:
            return str(content.get("text") or "")
        return _stringify(content)
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for p in content:
        if isinstance(p, str):
            parts.append(p)
            continue
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type") or "").lower()
        if ptype in ("input_text", "output_text", "text"):
            if p.get("text") is not None:
                parts.append(str(p.get("text") or ""))
        elif p.get("text") is not None:
            parts.append(str(p.get("text") or ""))
    return "".join(parts)


def _multimodal_content_from_parts(parts: list[Any]) -> Any:
    """Return plain string when only text; otherwise OpenAI multimodal list."""
    out: list[dict[str, Any]] = []
    text_only: list[str] = []
    any_non_text = False
    for p in parts:
        if not isinstance(p, dict):
            if isinstance(p, str) and p:
                text_only.append(p)
                out.append({"type": "text", "text": p})
            continue
        ptype = str(p.get("type") or "").lower()
        if ptype in ("input_text", "output_text", "text"):
            t = str(p.get("text") or "")
            text_only.append(t)
            out.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image", "image_url"):
            any_non_text = True
            image_url = p.get("image_url") or p.get("url") or p.get("image")
            if isinstance(image_url, str):
                out.append({"type": "image_url", "image_url": {"url": image_url}})
            elif isinstance(image_url, dict):
                out.append({"type": "image_url", "image_url": image_url})
            else:
                out.append(dict(p))
        else:
            if p.get("text") is not None:
                t = str(p.get("text") or "")
                text_only.append(t)
                out.append({"type": "text", "text": t})
    if not any_non_text:
        return "".join(text_only)
    return out


def convert_responses_input_to_messages(
    raw_input: Any,
    *,
    instructions: str | None = None,
) -> list[dict[str, Any]]:
    """Map Responses `input` (+ optional instructions) → chat messages[]."""
    messages: list[dict[str, Any]] = []
    instr = (instructions or "").strip()
    if instr:
        messages.append({"role": "system", "content": instr})

    if raw_input is None:
        return messages
    if isinstance(raw_input, str):
        text = raw_input.strip()
        if text:
            messages.append({"role": "user", "content": text})
        return messages

    items: list[Any]
    if isinstance(raw_input, dict):
        items = [raw_input]
    elif isinstance(raw_input, list):
        items = raw_input
    else:
        return messages

    for item in items:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                messages.append({"role": "user", "content": item})
            continue

        typ = str(item.get("type") or "").lower()
        role = str(item.get("role") or "").lower()

        if typ in ("function_call_output", "tool_result"):
            call_id = (
                str(item.get("call_id") or item.get("tool_call_id") or "").strip()
            )
            output = item.get("output")
            if output is None:
                output = item.get("content")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id or f"call_{uuid.uuid4().hex[:12]}",
                    "content": _stringify(output),
                }
            )
            continue

        if typ == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            args = item.get("arguments")
            if not isinstance(args, str):
                args = _stringify(args)
            tc = {
                "id": call_id or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": args or "{}"},
            }
            # Merge consecutive function_call items into one assistant message.
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls")
                and not (messages[-1].get("content") or "").strip()
            ):
                messages[-1]["tool_calls"].append(tc)
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [tc]}
                )
            continue

        if typ in ("input_text", "text") and not role:
            text = str(item.get("text") or "")
            if text:
                messages.append({"role": "user", "content": text})
            continue

        if typ == "output_text" and not role:
            text = str(item.get("text") or "")
            if text:
                messages.append({"role": "assistant", "content": text})
            continue

        # message item or role-bearing object
        if typ == "message" or role:
            msg_role = role or "user"
            content = item.get("content")
            if isinstance(content, list):
                content = _multimodal_content_from_parts(content)
            elif content is None and item.get("text") is not None:
                content = str(item.get("text") or "")
            elif not isinstance(content, (str, list, dict)) and content is not None:
                content = str(content)

            msg: dict[str, Any] = {"role": msg_role, "content": content}
            # Assistant history may include nested tool calls in rare shapes.
            tcs = item.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                msg["tool_calls"] = tcs
                if msg.get("content") in ("", None):
                    msg["content"] = None
            messages.append(msg)
            continue

    return messages


def convert_responses_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Responses tools (flat function) → chat completions tools[]."""
    if not isinstance(tools, list) or not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type") or "function").lower()
        if ttype != "function":
            # Built-in search tools are dropped on the chat path.
            continue
        if isinstance(t.get("function"), dict):
            out.append(t)
            continue
        name = t.get("name")
        if not name:
            continue
        fn: dict[str, Any] = {"name": name}
        if t.get("description") is not None:
            fn["description"] = t["description"]
        params = t.get("parameters")
        if params is None:
            params = t.get("input_schema")
        if params is None:
            params = {"type": "object", "properties": {}}
        fn["parameters"] = params
        item: dict[str, Any] = {"type": "function", "function": fn}
        if t.get("strict") is not None:
            # Upstream chat may ignore strict; keep only on function if present.
            try:
                fn["strict"] = bool(t.get("strict"))
            except Exception:
                pass
        out.append(item)
    return out or None


def extract_reasoning_effort(req: dict[str, Any]) -> str | None:
    raw = req.get("reasoning_effort")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    reasoning = req.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort.strip():
            return effort.strip()
    return None


def responses_request_to_chat_body(req: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Build an OpenAI chat/completions-shaped body from a Responses request dict."""
    messages = convert_responses_input_to_messages(
        req.get("input"),
        instructions=req.get("instructions")
        if isinstance(req.get("instructions"), str)
        else None,
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(req.get("stream")),
    }

    # Token limits
    if req.get("max_output_tokens") is not None:
        try:
            body["max_tokens"] = int(req["max_output_tokens"])
        except (TypeError, ValueError):
            pass
    elif req.get("max_tokens") is not None:
        try:
            body["max_tokens"] = int(req["max_tokens"])
        except (TypeError, ValueError):
            pass

    tools = convert_responses_tools(req.get("tools"))
    if tools:
        body["tools"] = tools
        # Only forward tool_choice when tools survived conversion. Codex compact
        # and pure-text Responses turns often set tool_choice without tools;
        # upstream rejects that with 400 invalid-argument.
        if req.get("tool_choice") is not None:
            body["tool_choice"] = req.get("tool_choice")
        if req.get("parallel_tool_calls") is not None:
            body["parallel_tool_calls"] = bool(req.get("parallel_tool_calls"))
    if req.get("temperature") is not None:
        body["temperature"] = req.get("temperature")
    if req.get("top_p") is not None:
        body["top_p"] = req.get("top_p")
    if req.get("user") is not None:
        body["user"] = req.get("user")
    effort = extract_reasoning_effort(req)
    if effort:
        body["reasoning_effort"] = effort
    # OpenAI prompt-cache request fields (sub2api / Claude Code / new-api).
    # Kept on the body for sticky affinity; app._sanitize_upstream_body strips
    # them before cli-chat-proxy if unsupported.
    if req.get("prompt_cache_key") is not None:
        body["prompt_cache_key"] = req.get("prompt_cache_key")
    if req.get("prompt_cache_retention") is not None:
        body["prompt_cache_retention"] = req.get("prompt_cache_retention")
    if isinstance(req.get("metadata"), dict):
        # Keep conversation sticky hints if clients put them here.
        meta = req["metadata"]
        if meta.get("user") and not body.get("user"):
            body["user"] = meta.get("user")
        if body.get("prompt_cache_key") in (None, "") and meta.get("prompt_cache_key"):
            body["prompt_cache_key"] = meta.get("prompt_cache_key")
        if body.get("prompt_cache_retention") is None and meta.get(
            "prompt_cache_retention"
        ) is not None:
            body["prompt_cache_retention"] = meta.get("prompt_cache_retention")
    return body


def _detail_cached_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    for parent in ("input_tokens_details", "prompt_tokens_details"):
        node = usage.get(parent)
        if isinstance(node, dict):
            try:
                val = int(node.get("cached_tokens") or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
    for key in (
        "cached_tokens",
        "cache_read_input_tokens",
        "prompt_cache_hit_tokens",
    ):
        try:
            val = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            return val
    return 0


def _detail_cache_creation_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("cache_creation_input_tokens", "cache_creation_tokens"):
        try:
            val = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            return val
    for parent in ("input_tokens_details", "prompt_tokens_details"):
        node = usage.get(parent)
        if isinstance(node, dict):
            try:
                val = int(node.get("cache_creation_tokens") or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
    return 0


def _detail_reasoning_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    for parent in ("output_tokens_details", "completion_tokens_details"):
        node = usage.get(parent)
        if isinstance(node, dict):
            try:
                val = int(node.get("reasoning_tokens") or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
    try:
        return int(usage.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def chat_usage_to_responses_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize usage for OpenAI Responses clients (sub2api / Claude Code).

    Always includes input_tokens_details.cached_tokens (0 when unknown) so
    secondary relays do not treat a missing key as "no cache support".
    Never invents non-zero cache hits.
    """
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    try:
        inp = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
    except (TypeError, ValueError):
        inp = 0
    try:
        out = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
    except (TypeError, ValueError):
        out = 0
    try:
        total = int(usage.get("total_tokens") or (inp + out))
    except (TypeError, ValueError):
        total = inp + out
    cached = _detail_cached_tokens(usage)
    reasoning = _detail_reasoning_tokens(usage)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": reasoning},
        # Keep chat-completions aliases for mixed clients / ledgers.
        "prompt_tokens": inp,
        "completion_tokens": out,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": _detail_cache_creation_tokens(usage),
    }


def build_responses_object(
    *,
    response_id: str,
    model: str,
    content: str,
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    status: str = "completed",
    created_at: int | None = None,
    previous_response_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a non-stream Responses object from chat completion pieces."""
    output: list[dict[str, Any]] = []
    text = content or ""
    # Optional: expose reasoning as a reasoning item when present (best-effort).
    # sub2api mainly needs message / function_call outputs.
    if text or not tool_calls:
        output.append(
            {
                "id": new_output_item_id("msg"),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                    }
                ],
            }
        )
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = (fn.get("name") or tc.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments") if fn else tc.get("arguments")
        if not isinstance(args, str):
            args = _stringify(args) if args is not None else "{}"
        call_id = (tc.get("id") or tc.get("call_id") or "").strip()
        if not call_id:
            call_id = f"call_{uuid.uuid4().hex[:24]}"
        output.append(
            {
                "id": new_output_item_id("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": args or "{}",
            }
        )

    obj: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at or time.time()),
        "status": status,
        "model": model,
        "output": output,
        "usage": chat_usage_to_responses_usage(usage),
    }
    if previous_response_id:
        obj["previous_response_id"] = previous_response_id
    if metadata:
        obj["metadata"] = metadata
    # Non-standard debug field is fine; unknown keys are usually ignored.
    if reasoning:
        obj["x_grok2api_reasoning"] = reasoning
    return obj


def sse_event(
    event: str,
    payload: dict[str, Any],
    *,
    sequence_number: int | None = None,
) -> str:
    """Format one Responses SSE frame.

    OpenAI Responses stream events require a monotonic top-level
    ``sequence_number`` (starting at 0). Clients such as the official SDK /
    sub2api fail deserialization with ``missing field sequence_number`` when
    it is absent.
    """
    body = dict(payload or {})
    if "type" not in body and event:
        body["type"] = event
    if sequence_number is not None:
        body["sequence_number"] = int(sequence_number)
    elif "sequence_number" not in body:
        # Defensive default: prefer explicit seq from callers, but never omit.
        body["sequence_number"] = 0
    return f"event: {event}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


class _Seq:
    """Tiny monotonic counter for Responses SSE sequence_number."""

    __slots__ = ("n",)

    def __init__(self, start: int = 0) -> None:
        self.n = int(start)

    def next(self) -> int:
        cur = self.n
        self.n += 1
        return cur


def iter_responses_sse_from_completion(
    *,
    response_id: str,
    model: str,
    content: str,
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    created_at: int | None = None,
    previous_response_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    chunk_chars: int = 48,
) -> list[str]:
    """Build a complete Responses SSE sequence from a finished chat completion.

    Emits response.created → (text/tool events) → response.completed.
    Used when we collect upstream first (reliable terminal event for sub2api).
    Every event includes a top-level ``sequence_number`` starting at 0.
    """
    created = int(created_at or time.time())
    frames: list[str] = []
    seq = _Seq(0)

    def emit(event: str, payload: dict[str, Any]) -> None:
        frames.append(sse_event(event, payload, sequence_number=seq.next()))

    initial = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model,
        "output": [],
        "usage": chat_usage_to_responses_usage(None),
    }
    if previous_response_id:
        initial["previous_response_id"] = previous_response_id
    if metadata:
        initial["metadata"] = metadata

    emit("response.created", {"type": "response.created", "response": initial})
    emit(
        "response.in_progress",
        {"type": "response.in_progress", "response": initial},
    )

    output_index = 0
    text = content or ""

    if text:
        msg_id = new_output_item_id("msg")
        emit(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )
        emit(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": msg_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            },
        )
        # Emit text in small chunks so clients that only paint on delta still work.
        step = max(8, int(chunk_chars or 48))
        for i in range(0, len(text), step):
            delta = text[i : i + step]
            emit(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": msg_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "delta": delta,
                },
            )
        emit(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": msg_id,
                "output_index": output_index,
                "content_index": 0,
                "text": text,
            },
        )
        emit(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": msg_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text},
            },
        )
        emit(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                },
            },
        )
        output_index += 1

    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = (fn.get("name") or tc.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments") if fn else tc.get("arguments")
        if not isinstance(args, str):
            args = _stringify(args) if args is not None else "{}"
        call_id = (tc.get("id") or tc.get("call_id") or "").strip()
        if not call_id:
            call_id = f"call_{uuid.uuid4().hex[:24]}"
        fc_id = new_output_item_id("fc")
        emit(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "",
                },
            },
        )
        emit(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "item_id": fc_id,
                "output_index": output_index,
                "delta": args or "{}",
            },
        )
        emit(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "item_id": fc_id,
                "output_index": output_index,
                "arguments": args or "{}",
            },
        )
        emit(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": args or "{}",
                },
            },
        )
        output_index += 1

    final = build_responses_object(
        response_id=response_id,
        model=model,
        content=text,
        reasoning=reasoning or "",
        tool_calls=tool_calls,
        usage=usage,
        status="completed",
        created_at=created,
        previous_response_id=previous_response_id,
        metadata=metadata,
    )
    emit(
        "response.completed",
        {"type": "response.completed", "response": final},
    )
    # Some clients also accept OpenAI-style done sentinel after events.
    frames.append("data: [DONE]\n\n")
    return frames


def failed_responses_sse(
    *,
    response_id: str,
    message: str,
    err_type: str = "server_error",
) -> list[str]:
    """Emit a terminal response.failed event with sequence_number=0."""
    payload = {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {
            "id": response_id,
            "object": "response",
            "status": "failed",
            "error": {"type": err_type, "message": message},
        },
    }
    return [
        sse_event("response.failed", payload, sequence_number=0),
        "data: [DONE]\n\n",
    ]


class ResponsesLiveStreamer:
    """Incremental Responses SSE encoder for true first-token streaming.

    Emits response.created immediately, then text/tool events as upstream
    deltas arrive. This avoids the old collect-then-replay path that made
    /v1/responses TTFT equal to full completion latency.
    """

    def __init__(
        self,
        *,
        response_id: str,
        model: str,
        created_at: int | None = None,
        previous_response_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = int(created_at or time.time())
        self.previous_response_id = previous_response_id
        self.metadata = metadata if isinstance(metadata, dict) else None
        self._seq = _Seq(0)
        self._started = False
        self._text_open = False
        self._msg_id: str | None = None
        self._text_parts: list[str] = []
        self._tools: dict[int, dict[str, Any]] = {}
        self._tool_opened: set[int] = set()
        self._tool_done: set[int] = set()
        self._output_index = 0
        self._text_output_index = 0
        self._closed = False

    def _emit(self, event: str, payload: dict[str, Any]) -> str:
        return sse_event(event, payload, sequence_number=self._seq.next())

    def _initial_response(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": "in_progress",
            "model": self.model,
            "output": [],
            "usage": chat_usage_to_responses_usage(None),
        }
        if self.previous_response_id:
            obj["previous_response_id"] = self.previous_response_id
        if self.metadata:
            obj["metadata"] = self.metadata
        return obj

    def start(self) -> list[str]:
        if self._started:
            return []
        self._started = True
        initial = self._initial_response()
        return [
            self._emit(
                "response.created",
                {"type": "response.created", "response": initial},
            ),
            self._emit(
                "response.in_progress",
                {"type": "response.in_progress", "response": initial},
            ),
        ]

    def _ensure_text_open(self) -> list[str]:
        if self._text_open:
            return []
        self._text_open = True
        self._msg_id = new_output_item_id("msg")
        self._text_output_index = self._output_index
        frames = [
            self._emit(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": self._text_output_index,
                    "item": {
                        "id": self._msg_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            self._emit(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": self._msg_id,
                    "output_index": self._text_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            ),
        ]
        self._output_index += 1
        return frames

    def on_text_delta(self, delta: str) -> list[str]:
        if not delta or self._closed:
            return []
        frames = self.start()
        frames.extend(self._ensure_text_open())
        self._text_parts.append(delta)
        frames.append(
            self._emit(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": self._msg_id,
                    "output_index": self._text_output_index,
                    "content_index": 0,
                    "delta": delta,
                },
            )
        )
        return frames

    def _tool_slot(self, index: int) -> dict[str, Any]:
        slot = self._tools.get(index)
        if slot is None:
            slot = {
                "id": new_output_item_id("fc"),
                "call_id": "",
                "name": "",
                "arguments": "",
                "output_index": None,
                "args_emitted": False,
            }
            self._tools[index] = slot
        return slot

    def _merge_tool_name(self, current: str, incoming: str) -> str:
        cur = (current or "").strip()
        inc = (incoming or "").strip()
        if not inc:
            return cur
        if not cur:
            return inc
        # True suffix fragment.
        if inc.startswith(cur):
            return inc
        if cur.endswith(inc):
            return cur
        if inc in cur:
            return cur
        return cur + inc

    def _merge_tool_args(
        self, current: str, incoming: str, *, tool_name: str | None = None
    ) -> str:
        """Merge streamed tool args without double-append corruption.

        Secondary relays often re-send cumulative JSON. Always-append would
        produce `{"file_path":"a"}{"file_path":"a"}` and Claude Code Read fails
        with missing required fields after parse.
        """
        try:
            import anthropic_compat as anth

            return anth.merge_tool_argument_delta(
                current or "", incoming or "", tool_name=tool_name
            )
        except Exception:
            cur = current or ""
            inc = incoming or ""
            if not inc:
                return cur
            if not cur:
                return inc
            if inc.startswith(cur):
                return inc
            if cur.endswith(inc) or inc in cur:
                return cur
            return cur + inc

    def _args_ready(self, args: str, *, tool_name: str | None = None) -> bool:
        try:
            import anthropic_compat as anth

            return bool(
                anth.is_complete_tool_arguments_json(
                    args or "", tool_name=tool_name
                )
            )
        except Exception:
            text = str(args or "").strip()
            if not text or text[0] not in "{[":
                return False
            try:
                parsed = json.loads(text)
            except Exception:
                return False
            return isinstance(parsed, (dict, list)) and parsed not in ({}, [])

    def _emit_ready_tools(self) -> list[str]:
        """Emit at most one complete tool at a time (name + full JSON args).

        Critical for Claude Code via sub2api:
        - Hold until arguments are complete non-empty JSON object/array.
        - Never stream argument suffixes live (Read.file_path must arrive whole).
        - Never open tool N+1 while tool N is unfinished.
        """
        frames: list[str] = []
        for idx in sorted(self._tools.keys()):
            if idx in self._tool_done:
                continue
            slot = self._tools[idx]
            name = (slot.get("name") or "").strip()
            args = slot.get("arguments") or ""
            # Do not overtake a lower unfinished tool.
            blocked = False
            for lower in range(0, idx):
                if lower in self._tool_done:
                    continue
                low = self._tools.get(lower)
                if not low:
                    continue
                if (low.get("name") or low.get("call_id") or str(low.get("arguments") or "").strip()):
                    blocked = True
                    break
            if blocked:
                break
            if not name:
                # Known id/args without name — keep holding this slot.
                if slot.get("call_id") or str(args).strip():
                    break
                continue
            if not self._args_ready(args, tool_name=name):
                # Hold partial objects (e.g. Update with only file_path).
                break
            # Open + emit full args + close in one burst (atomic for converters).
            if idx not in self._tool_opened:
                if not slot.get("call_id"):
                    slot["call_id"] = f"call_{uuid.uuid4().hex[:24]}"
                slot["output_index"] = self._output_index
                self._output_index += 1
                self._tool_opened.add(idx)
                frames.append(
                    self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": slot["output_index"],
                            "item": {
                                "id": slot["id"],
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": slot["call_id"],
                                "name": name,
                                "arguments": "",
                            },
                        },
                    )
                )
            if not slot.get("args_emitted"):
                frames.append(
                    self._emit(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": slot["id"],
                            "output_index": slot["output_index"],
                            "delta": args,
                        },
                    )
                )
                slot["args_emitted"] = True
            frames.append(
                self._emit(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": slot["id"],
                        "output_index": slot["output_index"],
                        "arguments": args,
                    },
                )
            )
            frames.append(
                self._emit(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": slot["output_index"],
                        "item": {
                            "id": slot["id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": slot.get("call_id") or f"call_{uuid.uuid4().hex[:24]}",
                            "name": name,
                            "arguments": args,
                        },
                    },
                )
            )
            self._tool_done.add(idx)
            # One complete tool per on_tool_delta tick — let converters settle.
            break
        return frames

    def on_tool_delta(self, tool_calls: list[dict[str, Any]] | None) -> list[str]:
        if not tool_calls or self._closed:
            return []
        frames = self.start()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            try:
                idx = int(tc.get("index") if tc.get("index") is not None else 0)
            except (TypeError, ValueError):
                idx = 0
            slot = self._tool_slot(idx)
            if tc.get("id"):
                slot["call_id"] = str(tc.get("id"))
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if fn.get("name"):
                slot["name"] = self._merge_tool_name(slot.get("name") or "", str(fn.get("name") or ""))
            if fn.get("arguments") is not None:
                args_piece = fn.get("arguments")
                if not isinstance(args_piece, str):
                    args_piece = _stringify(args_piece)
                slot["arguments"] = self._merge_tool_args(slot.get("arguments") or "", args_piece or "", tool_name=slot.get("name") or "")
            elif tc.get("arguments") is not None:
                args_piece = tc.get("arguments")
                if not isinstance(args_piece, str):
                    args_piece = _stringify(args_piece)
                slot["arguments"] = self._merge_tool_args(slot.get("arguments") or "", args_piece or "", tool_name=slot.get("name") or "")
        # Only emit tools whose name+complete JSON args are ready.
        frames.extend(self._emit_ready_tools())
        return frames

    def _close_open_text(self) -> list[str]:
        if not self._text_open or not self._msg_id:
            return []
        text = "".join(self._text_parts)
        frames = [
            self._emit(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": self._msg_id,
                    "output_index": self._text_output_index,
                    "content_index": 0,
                    "text": text,
                },
            ),
            self._emit(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": self._msg_id,
                    "output_index": self._text_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text},
                },
            ),
            self._emit(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": self._text_output_index,
                    "item": {
                        "id": self._msg_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text}],
                    },
                },
            ),
        ]
        self._text_open = False
        return frames

    def _close_open_tools(self) -> list[str]:
        """Flush any still-held tools at stream end (best-effort complete JSON)."""
        frames: list[str] = []
        # Prefer the readiness path first (complete JSON only).
        while True:
            more = self._emit_ready_tools()
            if not more:
                break
            frames.extend(more)
        # Terminal flush: emit remaining known tools even if args incomplete.
        for idx in sorted(self._tools.keys()):
            if idx in self._tool_done:
                continue
            slot = self._tools.get(idx) or {}
            name = (slot.get("name") or "").strip()
            args = slot.get("arguments") or ""
            if not name and not slot.get("call_id") and not str(args).strip():
                continue
            if not name:
                continue
            if not str(args).strip():
                args = "{}"
            if idx not in self._tool_opened:
                if not slot.get("call_id"):
                    slot["call_id"] = f"call_{uuid.uuid4().hex[:24]}"
                slot["output_index"] = self._output_index
                self._output_index += 1
                self._tool_opened.add(idx)
                frames.append(
                    self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": slot["output_index"],
                            "item": {
                                "id": slot["id"],
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": slot["call_id"],
                                "name": name,
                                "arguments": "",
                            },
                        },
                    )
                )
            out_idx = slot.get("output_index")
            if out_idx is None:
                continue
            if not slot.get("args_emitted"):
                frames.append(
                    self._emit(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": slot.get("id"),
                            "output_index": out_idx,
                            "delta": args,
                        },
                    )
                )
                slot["args_emitted"] = True
            frames.append(
                self._emit(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": slot.get("id"),
                        "output_index": out_idx,
                        "arguments": args,
                    },
                )
            )
            frames.append(
                self._emit(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": out_idx,
                        "item": {
                            "id": slot.get("id"),
                            "type": "function_call",
                            "status": "completed",
                            "call_id": slot.get("call_id") or f"call_{uuid.uuid4().hex[:24]}",
                            "name": name,
                            "arguments": args,
                        },
                    },
                )
            )
            self._tool_done.add(idx)
        return frames

    def complete(
        self,
        *,
        usage: dict[str, Any] | None = None,
        reasoning: str = "",
    ) -> list[str]:
        if self._closed:
            return []
        frames = self.start()
        frames.extend(self._close_open_text())
        frames.extend(self._close_open_tools())
        # Build terminal response object for clients that only read completed.
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(self._tools.keys()):
            slot = self._tools[idx]
            if not slot.get("name") and not slot.get("arguments"):
                continue
            tool_calls.append(
                {
                    "id": slot.get("call_id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": slot.get("name") or "",
                        "arguments": slot.get("arguments") or "{}",
                    },
                }
            )
        final = build_responses_object(
            response_id=self.response_id,
            model=self.model,
            content="".join(self._text_parts),
            reasoning=reasoning or "",
            tool_calls=tool_calls or None,
            usage=usage,
            status="completed",
            created_at=self.created_at,
            previous_response_id=self.previous_response_id,
            metadata=self.metadata,
        )
        frames.append(
            self._emit(
                "response.completed",
                {"type": "response.completed", "response": final},
            )
        )
        frames.append("data: [DONE]\n\n")
        self._closed = True
        return frames
