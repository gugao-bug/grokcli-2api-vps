"""Inbound chat history compaction for long Claude Code / agent tool loops.

Claude Code via sub2api can push multi-hundred-KB /v1/messages bodies as tool
rounds accumulate (Read results, command output, etc.). Upstream Grok then
times out, 400s, or returns stream-shape failures that surface as client
"API Error".

This module shrinks *past* tool results before the request is forwarded, while
keeping the latest tool rounds intact so the model can still act.
"""

from __future__ import annotations

import json
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 10_000_000) -> int:
    import os

    try:
        v = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(minimum, min(maximum, v))


# Opt-in only: long CC/sub2api tool loops can enable this to shrink 400–670KB bodies.
# Default off — compacting tool results can drop context the model still needs.
HISTORY_COMPACT_ENABLED = _env_bool("GROK2API_HISTORY_COMPACT", False)
# Keep this many most-recent tool rounds fully (assistant tool_calls + tool results).
HISTORY_KEEP_TOOL_ROUNDS = _env_int("GROK2API_HISTORY_KEEP_TOOL_ROUNDS", 6, minimum=1, maximum=64)
# Hard cap per single tool / tool_result content (chars). Recent rounds also truncated.
HISTORY_MAX_TOOL_RESULT_CHARS = _env_int(
    "GROK2API_HISTORY_MAX_TOOL_RESULT_CHARS", 12_000, minimum=512, maximum=2_000_000
)
# Soft budget for the whole messages array JSON size (chars). Older rounds collapse first.
HISTORY_MAX_MESSAGES_CHARS = _env_int(
    "GROK2API_HISTORY_MAX_MESSAGES_CHARS", 280_000, minimum=8_000, maximum=5_000_000
)
# Max tools per assistant turn. Default 1: sub2api/Claude Code keep only one
# active content_block; multi-tool frames still race to "Content block not found"
# (especially Read) and agent frontends stop scheduling further turns.
# Set higher only if the client is pure OpenAI; 0 = unlimited (not recommended).
OUTBOUND_MAX_TOOLS = _env_int("GROK2API_OUTBOUND_MAX_TOOLS", 1, minimum=0, maximum=64)
# Real wall-clock gap between consecutive outbound tool SSE frames (seconds).
# SSE comment keepalives alone are not enough: sub2api often drains a TCP window
# of back-to-back tool chunks in one converter tick and still races content_blocks.
# 0 disables the delay (pure OpenAI clients).
def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 5.0) -> float:
    try:
        v = float(__import__("os").getenv(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(minimum, min(maximum, v))


OUTBOUND_TOOL_GAP_SEC = _env_float("GROK2API_OUTBOUND_TOOL_GAP_SEC", 0.08, minimum=0.0, maximum=2.0)


_PLACEHOLDER_PREFIX = "[compacted tool result"


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = (block.get("type") or "").lower()
                if btype in ("text", "input_text", "output_text"):
                    parts.append(str(block.get("text") or ""))
                elif btype == "tool_result":
                    parts.append(_content_to_text(block.get("content")))
                else:
                    try:
                        parts.append(json.dumps(block, ensure_ascii=False))
                    except (TypeError, ValueError):
                        parts.append(str(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _set_text_content(msg: dict[str, Any], text: str) -> None:
    """Replace message content with plain text, preserving role/tool ids."""
    msg["content"] = text


def _truncate_text(text: str, limit: int, *, label: str = "content") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(0, limit - 80)
    omitted = len(text) - head
    return f"{text[:head]}\n…[{label} truncated, {omitted} chars omitted]"


def _placeholder(original: str, *, reason: str = "older round") -> str:
    n = len(original or "")
    return f"{_PLACEHOLDER_PREFIX}: {reason}; original {n} chars — re-Read if needed]"


def _messages_char_size(messages: list[Any]) -> int:
    try:
        return len(json.dumps(messages, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        total = 0
        for m in messages:
            total += len(str(m))
        return total


def _is_tool_message(msg: dict[str, Any]) -> bool:
    role = (msg.get("role") or "").lower()
    if role == "tool":
        return True
    if role == "function":
        return True
    return False


def _is_assistant_tool_call(msg: dict[str, Any]) -> bool:
    if (msg.get("role") or "").lower() != "assistant":
        return False
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        return True
    fc = msg.get("function_call")
    return isinstance(fc, dict) and bool(fc.get("name"))


def _tool_round_spans(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return (start, end_exclusive) spans for each tool round.

    A round is: assistant(tool_calls) + following contiguous tool messages.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if not isinstance(m, dict):
            i += 1
            continue
        if _is_assistant_tool_call(m):
            j = i + 1
            while j < n and isinstance(messages[j], dict) and _is_tool_message(messages[j]):
                j += 1
            spans.append((i, j))
            i = j
            continue
        i += 1
    return spans


def _shrink_tool_message(msg: dict[str, Any], *, max_chars: int, force_placeholder: bool) -> bool:
    """Mutate one tool message. Returns True if content changed."""
    original = _content_to_text(msg.get("content"))
    if not original:
        return False
    if force_placeholder:
        new = _placeholder(original, reason="older round")
        if new != original:
            _set_text_content(msg, new)
            return True
        return False
    if len(original) > max_chars:
        new = _truncate_text(original, max_chars, label="tool_result")
        _set_text_content(msg, new)
        return True
    return False


def _shrink_assistant_oversized_content(msg: dict[str, Any], *, max_chars: int) -> bool:
    """Trim huge assistant text (rare) without touching tool_calls structure."""
    if _is_assistant_tool_call(msg):
        # Keep tool_calls; only shrink text content if present and huge.
        content = msg.get("content")
        if content is None or content == "":
            return False
        text = _content_to_text(content)
        if len(text) > max_chars:
            _set_text_content(msg, _truncate_text(text, max_chars, label="assistant"))
            return True
        return False
    text = _content_to_text(msg.get("content"))
    if len(text) > max_chars * 2:
        _set_text_content(msg, _truncate_text(text, max_chars * 2, label="assistant"))
        return True
    return False


def compact_openai_messages(
    messages: list[Any] | None,
    *,
    enabled: bool | None = None,
    keep_tool_rounds: int | None = None,
    max_tool_result_chars: int | None = None,
    max_messages_chars: int | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Compact OpenAI-style messages in place-safe copy.

    Returns (messages, stats). Stats always present for response headers / logs.
    """
    stats: dict[str, Any] = {
        "enabled": False,
        "applied": False,
        "before_chars": 0,
        "after_chars": 0,
        "tool_rounds": 0,
        "compacted_tool_msgs": 0,
        "truncated_tool_msgs": 0,
    }
    if not isinstance(messages, list) or not messages:
        return messages or [], stats

    use = HISTORY_COMPACT_ENABLED if enabled is None else enabled
    keep = HISTORY_KEEP_TOOL_ROUNDS if keep_tool_rounds is None else keep_tool_rounds
    max_tr = (
        HISTORY_MAX_TOOL_RESULT_CHARS
        if max_tool_result_chars is None
        else max_tool_result_chars
    )
    budget = (
        HISTORY_MAX_MESSAGES_CHARS if max_messages_chars is None else max_messages_chars
    )
    keep = max(1, int(keep))
    max_tr = max(512, int(max_tr))
    budget = max(8_000, int(budget))

    # Shallow-copy messages + dicts so we never mutate caller's request objects.
    out: list[Any] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(dict(m))
        else:
            out.append(m)

    before = _messages_char_size(out)
    stats["before_chars"] = before
    stats["enabled"] = bool(use)

    if not use:
        stats["after_chars"] = before
        return out, stats

    # Compute spans on full `out` so indices match (non-dict entries are skipped).
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(out)
    while i < n:
        m = out[i]
        if isinstance(m, dict) and _is_assistant_tool_call(m):
            j = i + 1
            while j < n and isinstance(out[j], dict) and _is_tool_message(out[j]):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    stats["tool_rounds"] = len(spans)

    protected: set[int] = set()
    for start, end in spans[-keep:]:
        for idx in range(start, end):
            protected.add(idx)

    # Pass 1 (always): placeholder older tool rounds; clamp recent oversized results.
    # Do this even when under budget so long sessions don't slowly accumulate.
    for start, end in spans:
        recent = any(idx in protected for idx in range(start, end))
        for idx in range(start, end):
            m = out[idx]
            if not isinstance(m, dict) or not _is_tool_message(m):
                continue
            if recent:
                if _shrink_tool_message(m, max_chars=max_tr, force_placeholder=False):
                    stats["truncated_tool_msgs"] += 1
            else:
                if _shrink_tool_message(m, max_chars=max_tr, force_placeholder=True):
                    stats["compacted_tool_msgs"] += 1

    # Pass 2: if still over budget, hard-clamp recent tool results further.
    after = _messages_char_size(out)
    if after > budget:
        hard = max(1_500, max_tr // 3)
        for start, end in reversed(spans[-keep:]):
            for idx in range(start, end):
                m = out[idx]
                if not isinstance(m, dict) or not _is_tool_message(m):
                    continue
                text = _content_to_text(m.get("content"))
                if text.startswith(_PLACEHOLDER_PREFIX):
                    continue
                if len(text) > hard:
                    _set_text_content(m, _truncate_text(text, hard, label="tool_result"))
                    stats["truncated_tool_msgs"] += 1
            after = _messages_char_size(out)
            if after <= budget:
                break

    # Pass 3: still over budget — truncate older user/assistant prose (not system).
    after = _messages_char_size(out)
    if after > budget:
        soft = max(2_000, max_tr // 2)
        for idx, m in enumerate(out):
            if after <= budget:
                break
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "").lower()
            if role == "system":
                continue
            if idx in protected:
                continue
            if _is_tool_message(m):
                text = _content_to_text(m.get("content"))
                if not text.startswith(_PLACEHOLDER_PREFIX):
                    _set_text_content(m, _placeholder(text, reason="size budget"))
                    stats["compacted_tool_msgs"] += 1
                    after = _messages_char_size(out)
                continue
            if role in ("user", "assistant"):
                if _shrink_assistant_oversized_content(m, max_chars=soft):
                    after = _messages_char_size(out)
                else:
                    text = _content_to_text(m.get("content"))
                    if len(text) > soft:
                        _set_text_content(m, _truncate_text(text, soft, label=role))
                        after = _messages_char_size(out)

    after = _messages_char_size(out)
    stats["after_chars"] = after
    stats["applied"] = (
        stats["compacted_tool_msgs"] > 0
        or stats["truncated_tool_msgs"] > 0
        or after < before
    )
    return out, stats


def compact_upstream_body(body: dict[str, Any]) -> dict[str, Any]:
    """Apply message compaction to an OpenAI chat.completions body. Mutates body."""
    if not isinstance(body, dict):
        return {"enabled": False, "applied": False}
    messages = body.get("messages")
    new_messages, stats = compact_openai_messages(messages)
    body["messages"] = new_messages
    return stats


def cap_outbound_tools(tool_calls: list[Any] | None) -> list[Any] | None:
    """Optional safety valve: limit tools emitted in one assistant response."""
    if not tool_calls or OUTBOUND_MAX_TOOLS <= 0:
        return tool_calls
    if len(tool_calls) <= OUTBOUND_MAX_TOOLS:
        return tool_calls
    return tool_calls[:OUTBOUND_MAX_TOOLS]


def remaining_outbound_tool_budget(already_emitted: int) -> int | None:
    """How many more tools may be shipped this turn.

    None means unlimited (OUTBOUND_MAX_TOOLS <= 0). 0 means stop emitting.
    """
    if OUTBOUND_MAX_TOOLS <= 0:
        return None
    left = OUTBOUND_MAX_TOOLS - max(0, int(already_emitted or 0))
    return max(0, left)
