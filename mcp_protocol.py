#!/usr/bin/env python3
"""
mcp_protocol.py — transport-agnostic stateless MCP server core for Fables.

This module contains the full MCP protocol layer (JSON-RPC over the
2026-07-28 stateless spec: ``server/discover``, ``tools/list``,
``tools/call``, legacy ``initialize`` handshake, per-request ``_meta``
version checks) plus the transcript renderer, parameterized by a
:class:`McpBackend` so the same handler serves local session stores
(fables-mcp.py) and the remote cloud (fables-cloud.py).

A backend provides:

    list_sessions() -> (entries, sources)
        entries: list of dicts with id/source/title/cwd/project/mtime
            and optional native_id (the provider's own session id)
        sources: available source names for the "no sessions" message

    load(sid) -> raw transcript text
        sid is the opaque hash from list_sessions; raises KeyError when unknown
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from providers import AmbiguousSessionId, resolve_session_entry, session_haystack


def _identifier_match_snippet(entry: dict, query: str) -> str | None:
    """Return a snippet when query targets an opaque or native id, else None.

    Short tokens like ``pi`` must not match every ``pi:<uuid>`` composite.
    Exact id matches are always accepted; substring matches need 8+ chars
    (UUID prefixes).
    """
    opaque = str(entry.get("id") or "")
    native = str(entry.get("native_id") or "")
    aliases = [str(alias) for alias in (entry.get("aliases") or ()) if alias]
    if query == opaque.lower() or (len(query) >= 8 and query in opaque.lower()):
        return f"id {opaque}"
    if native and (query == native.lower() or (len(query) >= 8 and query in native.lower())):
        return f"native_id {native}"
    for alias in aliases:
        lowered = alias.lower()
        if query == lowered or (len(query) >= 8 and query in lowered):
            return f"native_id {alias}"
    source = str(entry.get("source") or "")
    if native and source:
        composite = f"{source}:{native}".lower()
        if query == composite:
            return f"native_id {native}"
    return None

PROTOCOL_VERSION = "2026-07-28"

MAX_RENDER_CHARS = 2_000_000      # get_session readable transcript cap
SEARCH_SCAN_LIMIT = 250           # newest sessions searched by search_sessions
SEARCH_RENDER_CHARS = 60_000      # per-session search window
MAX_SNIPPET_CHARS = 300

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_VERSION = -32022


class McpBackend:
    """Session source behind the MCP handler."""

    def list_sessions(self) -> tuple[list[dict], list[str]]:
        raise NotImplementedError

    def load(self, sid: str) -> str:
        raise NotImplementedError

    # Local durable-library operations. Remote/read-only backends do not
    # advertise these unless make_handler(import_tools=True) is requested.
    def inspect_import(self, input_path: str, origin: str | None = None) -> dict:
        raise NotImplementedError

    def apply_import(self, input_path: str, origin: str,
                     expect_sha256: str) -> dict:
        raise NotImplementedError

    def get_import(self, import_id: str) -> dict:
        raise NotImplementedError

    def get_provenance(self, session_id: str) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Transcript rendering (shared by get_session and search_sessions)
# ---------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    """Extract plain text from a content value (string, block list, or dict)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text") or ""))
            elif kind in ("tool_result", "toolResult"):
                parts.append(_content_text(block.get("content")))
            elif kind == "image":
                parts.append("[image attached]")
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return _content_text(content.get("text") or content.get("content"))
    return ""


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _tool_output_text(content: Any) -> str:
    value = _json_value(content)
    if isinstance(value, dict):
        if value.get("output") is not None:
            return str(value["output"])
        if value.get("stdout") is not None or value.get("stderr") is not None:
            parts = [str(value.get("stdout") or "")]
            if value.get("stderr"):
                parts.append("[stderr] " + str(value["stderr"]))
            return "\n".join(part for part in parts if part)
    return _content_text(value) if not isinstance(value, str) else value.strip()


def _push(items: list[dict], kind: str, text: str, **extra: Any) -> dict:
    item: dict[str, Any] = {"kind": kind, "text": text}
    item.update(extra)
    items.append(item)
    return item


def _add_model(meta: dict, model: str) -> None:
    if model and model not in meta["models"]:
        meta["models"].append(model)


def _parse_message(record_type: str, message: dict, meta: dict, items: list,
                   pending: dict) -> None:
    role = message.get("role") or record_type
    content = message.get("content")
    if role == "user":
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and str(block.get("text") or "").strip():
                    _push(items, "user", str(block["text"]))
                elif kind == "tool_result":
                    tool = pending.pop(block.get("tool_use_id"), None)
                    text = _content_text(block.get("content"))
                    if tool is not None:
                        tool["output"] = text
                        tool["isError"] = bool(block.get("is_error"))
                    elif text:
                        _push(items, "info", text, label="tool result (unpaired)")
                elif kind == "image":
                    _push(items, "user", "[image attached]")
        else:
            text = _content_text(content)
            if text:
                _push(items, "user", text)
    elif role == "assistant":
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            text = _tool_output_text(reasoning)
            if text:
                _push(items, "thinking", text)
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind in ("text", "input_text", "output_text") and \
                        str(block.get("text") or "").strip():
                    _push(items, "assistant", str(block["text"]))
                elif kind == "thinking" or kind == "reasoning":
                    thinking = str(block.get("thinking") or block.get("text") or "")
                    if thinking.strip():
                        _push(items, "thinking", thinking)
                elif kind in ("tool_use", "toolCall", "tool_call"):
                    tool = _push(
                        items, "tool", "",
                        name=str(block.get("name") or ""),
                        input=block.get("input", block.get("arguments")),
                        output=None, isError=False,
                    )
                    block_id = block.get("id")
                    if block_id:
                        pending[block_id] = tool
        else:
            text = _content_text(content)
            if text:
                _push(items, "assistant", text)
        tool_calls = _json_value(message.get("tool_calls"))
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                arguments = _json_value(
                    function.get("arguments", call.get("arguments"))
                )
                tool = _push(
                    items, "tool", "",
                    name=str(function.get("name") or call.get("name") or "tool"),
                    input=arguments,
                    output=None, isError=False,
                )
                call_id = call.get("id") or call.get("call_id") or call.get("tool_call_id")
                if call_id:
                    pending[str(call_id)] = tool
    elif role in ("toolResult", "tool"):
        decoded = _json_value(content)
        text = _tool_output_text(decoded)
        call_id = (message.get("toolCallId") or message.get("tool_use_id")
                   or message.get("tool_call_id"))
        tool = pending.pop(call_id, None) if call_id else None
        name = str(message.get("toolName") or message.get("tool_name") or "")
        is_error = bool(
            message.get("isError") or message.get("is_error") or
            message.get("effect_disposition") == "error" or
            (isinstance(decoded, dict) and decoded.get("error")) or
            (isinstance(decoded, dict) and decoded.get("exit_code") not in (None, 0))
        )
        if tool is not None:
            tool["output"] = text
            tool["isError"] = is_error
        elif name:
            # Kimi-style archive: result carries its call metadata.
            _push(items, "tool", "", name=name,
                  input=message.get("arguments"), output=text,
                  isError=is_error)
        elif text:
            _push(items, "info", text, label=f"{name or 'tool'} result (unpaired)")
    elif role == "bashExecution":
        _push(
            items, "tool", "",
            name="bash", input=message.get("command"),
            output=message.get("output") or "",
            isError=message.get("exitCode") not in (None, 0),
        )
    elif role == "system":
        text = _content_text(content)
        if text:
            _push(items, "info", text, label="system")


def _parse_codex_item(payload: dict, meta: dict, items: list, pending: dict) -> None:
    kind = payload.get("type")
    if kind == "message":
        _parse_message("assistant", payload, meta, items, pending)
    elif kind == "function_call" or (kind and kind.endswith("_call")):
        tool = _push(
            items, "tool", "",
            name=str(payload.get("name") or kind[:-5]),
            input=payload.get("arguments"),
            output=None, isError=False,
        )
        if payload.get("call_id"):
            pending[str(payload["call_id"])] = tool
    elif kind == "function_call_output" or (kind and kind.endswith("_call_output")):
        tool = pending.pop(payload.get("call_id"), None)
        text = str(payload.get("output") or "")
        if tool is not None:
            tool["output"] = text
        elif text:
            _push(items, "info", text, label="tool result (unpaired)")


def _parse_record(obj: dict, meta: dict, items: list, pending: dict) -> None:
    kind = obj.get("type")
    message = obj.get("message")
    role = obj.get("role")
    if isinstance(message, dict) and (kind in ("user", "assistant", "message", "system")
                                     or role in ("user", "assistant", "system")):
        _parse_message(role or kind, message, meta, items, pending)
        return
    if kind is None and role in ("user", "assistant", "system", "tool") and \
            obj.get("content") is not None:
        # Command Code / Qwen style: role and content on the record itself.
        _parse_message(role, obj, meta, items, pending)
        return
    if kind == "response_item":
        payload = obj.get("payload")
        if isinstance(payload, dict):
            _parse_codex_item(payload, meta, items, pending)
        return
    if kind == "session_meta":
        payload = obj.get("payload")
        if isinstance(payload, dict) and payload.get("cwd") and not meta["cwd"]:
            meta["cwd"] = str(payload["cwd"])
        return
    if kind == "event_msg":
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "thread_name_updated" and payload.get("thread_name"):
            meta["title"] = str(payload["thread_name"])
        elif payload.get("type") == "user_message" and not meta["title"]:
            text = _content_text(payload.get("message"))
            if text:
                meta["title"] = text[:120]
        return
    if kind == "session" and obj.get("cwd") and not meta["cwd"]:
        meta["cwd"] = str(obj["cwd"])
    elif kind == "model_change" and obj.get("modelId"):
        _add_model(meta, str(obj["modelId"]))
    elif kind in ("summary", "ai-title"):
        value = obj.get("summary") or obj.get("aiTitle")
        if value and not meta["title"]:
            meta["title"] = str(value)[:120]


def parse_transcript(text: str) -> dict:
    """Turn a raw archive (JSONL or synthetic JSON) into a flat item list."""
    meta = {"title": "", "cwd": "", "models": [], "start": "", "end": ""}
    items: list[dict] = []
    pending: dict[str, dict] = {}
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("fablesVersion") == 2 and \
                isinstance(data.get("items"), list):
            raw_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            meta = {
                "title": str(raw_meta.get("title") or ""),
                "cwd": str(raw_meta.get("cwd") or ""),
                "models": [str(value) for value in (raw_meta.get("models") or [])],
                "start": raw_meta.get("start") or "",
                "end": raw_meta.get("end") or "",
            }
            normalized = []
            for value in data["items"]:
                if not isinstance(value, dict) or not value.get("kind"):
                    continue
                item = dict(value)
                item.setdefault("text", "")
                normalized.append(item)
            return {"meta": meta, "items": normalized}
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            session = data.get("session")
            if isinstance(session, dict):
                if session.get("cwd"):
                    meta["cwd"] = str(session["cwd"])
                if session.get("title"):
                    meta["title"] = str(session["title"])[:120]
                if session.get("model"):
                    _add_model(meta, str(session["model"]))
                meta["start"] = session.get("started_at") or meta["start"]
                meta["end"] = (
                    session.get("ended_at") or session.get("last_activity_at") or
                    meta["start"]
                )
            for entry in data["messages"]:
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                _parse_message(message.get("type") or "message", message, meta, items, pending)
            if not meta["title"]:
                for item in items:
                    if item["kind"] == "user":
                        meta["title"] = item["text"][:120]
                        break
            return {"meta": meta, "items": items}
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            _parse_record(obj, meta, items, pending)
    if not meta["title"]:
        for item in items:
            if item["kind"] == "user":
                meta["title"] = item["text"][:120]
                break
    return {"meta": meta, "items": items}


def _compact(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "\n… (truncated)"
    return text


def _keep_item(kind: str, include_tools: bool, include_thinking: bool) -> bool:
    if kind in ("user", "assistant"):
        return True
    if kind == "thinking":
        return include_thinking
    if kind in ("tool", "info"):
        return include_tools
    return False


def _omission_hint(skipped_thinking: bool, skipped_tools: bool) -> str:
    omitted: list[str] = []
    flags: list[str] = []
    if skipped_thinking:
        omitted.append("thinking")
        flags.append("include_thinking=true")
    if skipped_tools:
        omitted.append("tool calls and results")
        flags.append("include_tools=true")
    if not omitted:
        return ""
    if skipped_thinking and skipped_tools:
        what = "thinking, tool calls, and results"
        how = "pass include_thinking=true and/or include_tools=true"
    else:
        what = omitted[0]
        how = f"pass {flags[0]}"
    return f"\n\n({what} omitted; {how} for those parts)"


def render_transcript(text: str, max_chars: int = MAX_RENDER_CHARS,
                      include_tools: bool = False,
                      include_thinking: bool = False) -> str:
    """Render a raw archive as readable text.

    By default only user and assistant messages are rendered, keeping the
    output compact for agent context. ``include_thinking`` adds reasoning
    blocks. ``include_tools`` adds tool calls with arguments, tool results,
    and info notes. The two flags are independent.
    """
    parsed = parse_transcript(text)
    meta = parsed["meta"]
    out: list[str] = []
    if not parsed["items"] and text.strip():
        # Non-JSON transcripts (e.g. aider markdown histories, ACP logs)
        # render as-is.
        return _compact(text, max_chars)
    if meta["title"]:
        out.append(f"# {meta['title']}")
    details = []
    if meta["cwd"]:
        details.append(f"cwd: {meta['cwd']}")
    if meta["models"]:
        details.append(f"models: {', '.join(meta['models'])}")
    if details:
        out.append(" · ".join(details))
    skipped_thinking = False
    skipped_tools = False
    for item in parsed["items"]:
        kind = item["kind"]
        if not _keep_item(kind, include_tools, include_thinking):
            if kind == "thinking":
                skipped_thinking = True
            else:
                skipped_tools = True
            continue
        text = item["text"]
        if kind == "user":
            out.append(f"\n## user\n{text}")
        elif kind == "assistant":
            out.append(f"\n## assistant\n{text}")
        elif kind == "thinking":
            out.append(f"\n> thinking\n{text}")
        elif kind == "tool":
            name = item.get("name") or "tool"
            out.append(f"\n## tool · {name}")
            if item.get("input") is not None and item["input"] != "":
                out.append(_compact(item["input"]))
            if item.get("output") is not None:
                marker = "⚠ error:" if item.get("isError") else "↳"
                out.append(f"{marker} {_compact(item['output'], 4000)}")
        elif kind == "info":
            label = item.get("label") or "note"
            out.append(f"\n({label}) {_compact(text, 2000)}")
    rendered = "\n".join(out).strip()
    hint = _omission_hint(skipped_thinking, skipped_tools)
    if hint:
        if not rendered or rendered == "(empty transcript)":
            rendered = "(no messages in this session" + hint[1:]
        elif len(rendered) <= max_chars:
            rendered += hint
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip() + "\n… (transcript truncated; use format=\"json\" for the full archive)"
    return rendered or "(empty transcript)"


# ---------------------------------------------------------------------------
# MCP protocol (stateless, 2026-07-28)
# ---------------------------------------------------------------------------

class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolFailure(Exception):
    """Tool-level failure reported as isError in the result, not a protocol error."""


def _result(request_id: Any, body: dict, cache: dict | None = None,
            server_name: str = "fables-mcp", server_version: str = "") -> dict:
    result = {"resultType": "complete"}
    result.update(body)
    if cache:
        result.update(cache)
    result["_meta"] = {
        "io.modelcontextprotocol/serverInfo": {
            "name": server_name,
            "version": server_version,
        },
    }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


SESSION_TOOLS = [
    {
        "name": "list_sessions",
        "description": (
            "List coding-agent conversation sessions in this Fables library, "
            "newest first. Sources include pi, prime, hermes, claude (Claude Code), "
            "codex, gemini, goose, cline, roo, vscode, opencode, cursor, "
            "cursor-cli, kimi, commandcode, copilot, amp, qwen, aider, trae, "
            "kiro, kilo, and zed. Returns opaque ids and native provider ids "
            "(for example a pi UUID) to pass to get_session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string",
                           "description": "Restrict to one source, e.g. 'pi' or 'claude'."},
                "query": {"type": "string",
                          "description": "Case-insensitive substring matched against title, "
                                         "project, cwd, opaque id, and native provider id."},
                "origin": {"type": "string", "description": "Restrict imported sessions to one origin."},
                "scope": {"type": "string", "enum": ["all", "live", "imported"], "default": "all"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
    },
    {
        "name": "get_session",
        "description": (
            "Fetch the complete transcript of one session as readable text. "
            "By default only user and assistant messages are returned, which "
            "keeps the output compact for agent context. Pass "
            "include_thinking=true for reasoning/thinking blocks, and/or "
            "include_tools=true for tool calls with arguments and tool "
            "results (the two flags are independent). Use an id returned by "
            "list_sessions or search_sessions, a native provider id "
            "(for example a pi UUID), or source:native_id "
            "(for example pi:019ffc61-...). Intended for resuming a "
            "conversation: read the transcript, then continue the work in a "
            "new session. With format='json', returns the raw normalized "
            "archive instead of the readable rendering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "Opaque id from list_sessions, a native provider "
                                      "id (for example a pi UUID), or source:native_id."},
                "format": {"type": "string", "enum": ["text", "json"], "default": "text",
                           "description": "text = readable transcript (default); "
                                          "json = raw archive with full record fidelity."},
                "include_thinking": {"type": "boolean", "default": False,
                                      "description": "Also include reasoning/thinking blocks "
                                                     "(default: messages only). Independent of "
                                                     "include_tools."},
                "include_tools": {"type": "boolean", "default": False,
                                   "description": "Also include tool calls and tool results "
                                                  "(default: messages only). Independent of "
                                                  "include_thinking."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "search_sessions",
        "description": (
            "Search the transcripts of the most recent sessions for a text "
            "query (case-insensitive substring). Matches native provider ids "
            "and opaque hashes as well as transcript text. By default only user "
            "and assistant messages are searched. Pass include_thinking=true "
            "to also search reasoning/thinking blocks, and/or "
            "include_tools=true to also search tool calls and results "
            "(the two flags are independent). Returns matching sessions with "
            "a snippet of the first match."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Case-insensitive substring to find in transcripts, "
                                         "native provider ids, or opaque hashes."},
                "source": {"type": "string",
                           "description": "Restrict to one source, e.g. 'pi' or 'codex'."},
                "origin": {"type": "string", "description": "Restrict imported sessions to one origin."},
                "scope": {"type": "string", "enum": ["all", "live", "imported"], "default": "all"},
                "include_thinking": {"type": "boolean", "default": False,
                                      "description": "Also search reasoning/thinking blocks "
                                                     "(default: messages only). Independent of "
                                                     "include_tools."},
                "include_tools": {"type": "boolean", "default": False,
                                   "description": "Also search tool calls and tool results "
                                                  "(default: messages only). Independent of "
                                                  "include_thinking."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["query"],
        },
    },
]

IMPORT_TOOLS = [
    {
        "name": "inspect_import",
        "description": (
            "Read-only inspection of one exact Fables import input. Validates "
            "format and ZIP safety, calculates SHA-256, reports completeness "
            "and classifications, and never prints transcript bodies. Pass an "
            "origin only when the user supplied it; it makes revision/conflict "
            "classification precise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Exact local input path."},
                "origin": {"type": "string", "description": "Optional user-supplied source label."},
            },
            "required": ["input"],
        },
    },
    {
        "name": "apply_import",
        "description": (
            "Atomically apply an already inspected input to the durable Fables "
            "library. Requires the exact inspected SHA-256, an explicit "
            "user-supplied origin, and user confirmation. Never writes native "
            "harness storage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "origin": {"type": "string"},
                "expect_sha256": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "True only after user approval."},
            },
            "required": ["input", "origin", "expect_sha256", "confirmed"],
        },
    },
    {
        "name": "get_import",
        "description": "Verify import state and counts using the opaque import ID returned by apply_import.",
        "inputSchema": {
            "type": "object",
            "properties": {"import_id": {"type": "string"}},
            "required": ["import_id"],
        },
    },
    {
        "name": "get_session_provenance",
        "description": "Return provider/native identity, origins, import IDs, hashes, completeness, attachments, and revision relationships.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]

# Public compatibility constant: the local server advertises all tools; cloud
# backends pass import_tools=False and expose SESSION_TOOLS only.
TOOLS = SESSION_TOOLS + IMPORT_TOOLS


def _discover(scope: str, import_tools: bool = False) -> dict:
    return {
        "supportedVersions": [PROTOCOL_VERSION],
        "capabilities": {"tools": {}},
        "instructions": (
            "Fables exposes the coding-agent conversation sessions "
            f"{scope} (pi, Prime Agent, Hermes Agent, Claude Code, Codex, Gemini CLI, "
            "Goose, Cline, Roo Code, OpenCode, Cursor, Cursor CLI, Kimi CLI, "
            "Command Code, VS Code Chat, Copilot CLI, Amp, Qwen Code, Aider, "
            "Trae, Kiro, Kilo Code, and Zed). Use "
            "list_sessions to find a session, get_session to fetch a transcript "
            "(messages by default; include_thinking and include_tools are independent "
            "opt-ins) for example to resume a conversation started in another agent, "
            "and search_sessions to find sessions by text. "
            "get_session accepts opaque hashes, native provider ids (for example a "
            "pi UUID), and source:native_id. "
            + ("Import tools require read-only inspection before an explicitly "
               "confirmed digest-bound apply and never write provider stores."
               if import_tools else "Everything is read-only.")
        ),
    }


def _session_row(entry: dict) -> dict:
    mtime = entry.get("mtime") or 0
    stamp = ""
    if mtime:
        stamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "id": entry["id"],
        "source": entry.get("source"),
        "title": entry.get("title"),
        "cwd": entry.get("cwd") or entry.get("project") or "",
        "mtime": stamp,
    }
    if entry.get("native_id"):
        row["native_id"] = entry["native_id"]
    if entry.get("archived"):
        row["archived"] = True
        row["origin"] = entry.get("origin")
        row["import_id"] = entry.get("import_id")
    else:
        row["archived"] = False
    return row


def make_handler(backend: McpBackend, *, server_name: str = "fables-mcp",
                 server_version: str = "", scope: str = "in this library",
                 import_tools: bool = False) -> Callable[[str], dict | None]:
    """Build a ``handle_message(raw) -> dict | None`` for one backend."""

    def tool_list_sessions(arguments: dict) -> str:
        query = str(arguments.get("query") or "").strip().lower()
        source = str(arguments.get("source") or "").strip().lower()
        origin = str(arguments.get("origin") or "").strip()
        archive_scope = str(arguments.get("scope") or "all").strip().lower()
        if archive_scope not in ("all", "live", "imported"):
            raise McpError(INVALID_PARAMS, "scope must be 'all', 'live', or 'imported'")
        try:
            limit = int(arguments.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))
        sessions, sources = backend.list_sessions()
        rows = []
        for entry in sessions:
            if source and entry.get("source", "").lower() != source:
                continue
            if origin and entry.get("origin") != origin:
                continue
            if archive_scope == "live" and entry.get("archived"):
                continue
            if archive_scope == "imported" and not entry.get("archived"):
                continue
            if query:
                if query not in session_haystack(entry):
                    continue
            rows.append(_session_row(entry))
            if len(rows) >= limit:
                break
        if not rows:
            filters = " ".join(
                part for part in (f"source={source}" if source else "",
                                  f"query={arguments.get('query')!r}" if query else "")
                if part
            )
            return (f"No sessions found{f' ({filters})' if filters else ''}.\n"
                    f"Available sources: {', '.join(sources)}")
        return json.dumps(rows, indent=2, ensure_ascii=False)

    def tool_get_session(arguments: dict) -> str:
        sid = arguments.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise McpError(INVALID_PARAMS, "Missing required argument: id")
        fmt = str(arguments.get("format") or "text")
        if fmt not in ("text", "json"):
            raise McpError(INVALID_PARAMS, "format must be 'text' or 'json'")
        include_tools = bool(arguments.get("include_tools"))
        include_thinking = bool(arguments.get("include_thinking"))
        sessions, _sources = backend.list_sessions()
        try:
            entry = resolve_session_entry(sid.strip(), sessions)
        except AmbiguousSessionId as exc:
            raise ToolFailure(str(exc)) from None
        except KeyError:
            raise ToolFailure(
                f"Session {sid.strip()!r} not found. Pass a list_sessions id, "
                "a native provider id (for example a pi UUID), or "
                "source:native_id (for example pi:019ffc61-...)."
            ) from None
        try:
            raw = backend.load(entry["id"])
        except KeyError:
            raise ToolFailure(
                f"Session {sid.strip()!r} not found. Pass a list_sessions id, "
                "a native provider id (for example a pi UUID), or "
                "source:native_id (for example pi:019ffc61-...)."
            ) from None
        if fmt == "json":
            return raw
        return render_transcript(raw, include_tools=include_tools,
                                 include_thinking=include_thinking)

    def tool_search_sessions(arguments: dict) -> str:
        query = str(arguments.get("query") or "").strip().lower()
        if not query:
            raise McpError(INVALID_PARAMS, "Missing required argument: query")
        source = str(arguments.get("source") or "").strip().lower()
        origin = str(arguments.get("origin") or "").strip()
        archive_scope = str(arguments.get("scope") or "all").strip().lower()
        if archive_scope not in ("all", "live", "imported"):
            raise McpError(INVALID_PARAMS, "scope must be 'all', 'live', or 'imported'")
        include_tools = bool(arguments.get("include_tools"))
        include_thinking = bool(arguments.get("include_thinking"))
        try:
            limit = int(arguments.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        sessions, _sources = backend.list_sessions()
        matches = []
        for entry in sessions[:SEARCH_SCAN_LIMIT]:
            if source and entry.get("source", "").lower() != source:
                continue
            if origin and entry.get("origin") != origin:
                continue
            if archive_scope == "live" and entry.get("archived"):
                continue
            if archive_scope == "imported" and not entry.get("archived"):
                continue
            snippet = _identifier_match_snippet(entry, query)
            if snippet is None:
                try:
                    raw = backend.load(entry["id"])
                except KeyError:
                    continue
                hay = render_transcript(raw, max_chars=SEARCH_RENDER_CHARS,
                                        include_tools=include_tools,
                                        include_thinking=include_thinking).lower()
                idx = hay.find(query)
                if idx < 0:
                    continue
                snippet = " ".join(
                    hay[max(0, idx - 100): idx + 200].split()
                )[:MAX_SNIPPET_CHARS]
            match = {
                "id": entry["id"],
                "source": entry.get("source"),
                "title": entry.get("title"),
                "cwd": entry.get("cwd") or entry.get("project") or "",
                "snippet": snippet,
            }
            if entry.get("native_id"):
                match["native_id"] = entry["native_id"]
            matches.append(match)
            if len(matches) >= limit:
                break
        if not matches:
            scanned = min(len(sessions), SEARCH_SCAN_LIMIT)
            return (f"No sessions match {arguments.get('query')!r} "
                    f"(searched the {scanned} most recent sessions).")
        return json.dumps(matches, indent=2, ensure_ascii=False)

    def _required_text(arguments: dict, key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise McpError(INVALID_PARAMS, f"Missing required argument: {key}")
        return value.strip()

    def _envelope(operation: Callable[[], dict]) -> str:
        try:
            result = operation()
        except Exception as exc:
            if hasattr(exc, "envelope"):
                raise ToolFailure(json.dumps(exc.envelope(), indent=2,
                                             ensure_ascii=False)) from None
            raise
        return json.dumps({"ok": True, "result": result}, indent=2,
                          ensure_ascii=False)

    def tool_inspect_import(arguments: dict) -> str:
        input_path = _required_text(arguments, "input")
        origin = arguments.get("origin")
        if origin is not None and not isinstance(origin, str):
            raise McpError(INVALID_PARAMS, "origin must be a string")
        return _envelope(lambda: backend.inspect_import(input_path, origin))

    def tool_apply_import(arguments: dict) -> str:
        if arguments.get("confirmed") is not True:
            raise McpError(INVALID_PARAMS,
                           "apply_import requires confirmed=true after user approval")
        input_path = _required_text(arguments, "input")
        origin = _required_text(arguments, "origin")
        expected = _required_text(arguments, "expect_sha256")
        return _envelope(lambda: backend.apply_import(input_path, origin, expected))

    def tool_get_import(arguments: dict) -> str:
        import_id = _required_text(arguments, "import_id")
        return _envelope(lambda: backend.get_import(import_id))

    def tool_get_provenance(arguments: dict) -> str:
        sid = _required_text(arguments, "id")
        sessions, _sources = backend.list_sessions()
        try:
            entry = resolve_session_entry(sid, sessions)
        except AmbiguousSessionId as exc:
            raise ToolFailure(str(exc)) from None
        except KeyError:
            raise ToolFailure(f"Session {sid!r} not found. Pass a list_sessions ID.") from None
        return _envelope(lambda: backend.get_provenance(entry["id"]))

    def call_tool(params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "arguments must be an object")
        try:
            if name == "list_sessions":
                text = tool_list_sessions(arguments)
            elif name == "get_session":
                text = tool_get_session(arguments)
            elif name == "search_sessions":
                text = tool_search_sessions(arguments)
            elif import_tools and name == "inspect_import":
                text = tool_inspect_import(arguments)
            elif import_tools and name == "apply_import":
                text = tool_apply_import(arguments)
            elif import_tools and name == "get_import":
                text = tool_get_import(arguments)
            elif import_tools and name == "get_session_provenance":
                text = tool_get_provenance(arguments)
            else:
                raise McpError(INVALID_PARAMS, f"Unknown tool: {name}")
        except ToolFailure as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": text}]}

    def initialize(params: dict) -> dict:
        """Legacy initialize handshake for clients that still speak the
        pre-2026-07-28 protocol (e.g. Codex CLI 0.147)."""
        version = params.get("protocolVersion") or "2025-06-18"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": server_name, "version": server_version},
            "instructions": _discover(scope, import_tools)["instructions"],
        }

    def handle_message(raw: str) -> dict | None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return _error(None, PARSE_ERROR, "Parse error")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or \
                not isinstance(message.get("method"), str):
            return _error(None, INVALID_REQUEST, "Invalid Request")
        request_id = message.get("id")
        if request_id is None:
            return None  # notification; stateless server keeps no state to update
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_REQUEST, "Invalid Request")
        meta = params.get("_meta")
        if isinstance(meta, dict):
            version = meta.get("io.modelcontextprotocol/protocolVersion")
            if version is not None and version != PROTOCOL_VERSION:
                return _error(
                    request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                    {"supportedVersions": [PROTOCOL_VERSION]},
                )
        method = message["method"]
        try:
            if method == "initialize":
                return _result(request_id, initialize(params),
                               server_name=server_name, server_version=server_version)
            if method == "ping":
                return _result(request_id, {}, server_name=server_name,
                               server_version=server_version)
            if method == "server/discover":
                return _result(request_id, _discover(scope, import_tools),
                               cache={"ttlMs": 3_600_000, "cacheScope": "public"},
                               server_name=server_name, server_version=server_version)
            if method == "tools/list":
                available_tools = TOOLS if import_tools else SESSION_TOOLS
                return _result(request_id, {"tools": available_tools},
                               cache={"ttlMs": 300_000, "cacheScope": "public"},
                               server_name=server_name, server_version=server_version)
            if method == "tools/call":
                return _result(request_id, call_tool(params),
                               server_name=server_name, server_version=server_version)
            return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except McpError as exc:
            return _error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # never take the server down over one request
            sys.stderr.write(f"{server_name}: {method} failed: {exc}\n")
            return _error(request_id, INTERNAL_ERROR, "Internal error")

    return handle_message
