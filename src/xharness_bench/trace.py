"""Export a readable trace of what the model did.

Both adapters already hold the complete session event stream: the XHarness arm reads it
back through ``session.history`` and the upstream driver prints it in its result JSON.
Neither used to keep it, so a finished trial left behind counts and no account of what
actually happened -- which is the wrong way round, because the counts are derived and
the events are the evidence.

The trace goes to ``/logs/artifacts/`` inside the container. Harbor injects that
directory as a conventional artifact source for every task and collects it into
``artifacts/logs/artifacts/`` under the trial directory, so nothing here has to know
where the trial is being written on the host.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .rpc import (
    tool_call_arguments,
    tool_call_id,
    tool_call_name,
    tool_result_call_id,
    tool_result_text,
)

# Where Harbor looks. Not configurable on purpose: a convention that can drift is not a
# convention, and the trial record already points at this path.
ARTIFACT_DIR = "/logs/artifacts"

# Preview bounds. The raw events are written in full beside the rendering, so these only
# decide how much of each item is *shown* inline. A single tool result can be megabytes
# of build output; inlining that would bury the trace it is supposed to explain.
PREVIEW_CHARS = 400
MAX_EVENTS = 50_000


def _clip(text: str, limit: int = PREVIEW_CHARS) -> str:
    """One line, bounded. A trace is read as a sequence, so a newline inside a message
    breaks the column and makes the sequence unreadable -- and tool output is mostly
    newlines."""
    text = " ".join(text.replace("\r\n", "\n").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [+{len(text) - limit} chars]"


def _arguments(raw: Any) -> str:
    """Render tool arguments compactly -- the command, not the JSON scaffolding."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return _clip(raw, 200)
    else:
        parsed = raw
    if isinstance(parsed, dict):
        # Prefer the fields that say what the call does.
        for key in ("command", "cmd", "file_path", "path", "pattern", "query", "url",
                    "description"):
            if key in parsed:
                return _clip(f"{key}={parsed[key]}", 200)
        return _clip(json.dumps(parsed, ensure_ascii=False), 200)
    return _clip(str(parsed), 200)


def render_trace(
    events: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    label: str = "",
) -> str:
    """One line per event, in order, with the parts that matter shown inline."""
    lines: list[str] = []
    if label:
        lines.append(f"# trace: {label}")
    if meta:
        for key in sorted(meta):
            value = meta[key]
            if value in (None, "", [], {}):
                continue
            lines.append(f"# {key}: {json.dumps(value, ensure_ascii=False)[:300]}")
    lines.append("")

    tool_names: dict[str, str] = {}
    for event in events[:MAX_EVENTS]:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        where = ""
        turn, step = data.get("turn"), data.get("step")
        if turn is not None:
            where = f"t{turn}"
            if step is not None:
                where += f".s{step}"

        if kind == "user/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else data
            lines.append(f"{where:<8} user      {_clip(_text_of(message))}")
        elif kind in ("assistant/message", "message/assistant"):
            lines.append(f"{where:<8} assistant {_clip(_text_of(data.get('message', data)))}")
        elif kind == "tool/call":
            # The same extractors the summary uses. Reading the fields here directly is
            # how the first version of this renderer printed "?" for every tool name:
            # the wire is flat camelCase, not the nested shape the Rust enum implies.
            name = tool_call_name(data) or "?"
            call_id = tool_call_id(data)
            if call_id:
                tool_names[call_id] = name
            lines.append(f"{where:<8} CALL      {name}  {_arguments(tool_call_arguments(data))}")
        elif kind == "tool/result":
            content = tool_result_text(data)
            size = len(content.encode("utf-8", "replace"))
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            call_id = tool_result_call_id(data)
            name = tool_names.get(call_id, "?") if call_id else "?"
            failed = " [error]" if result.get("outcome") == "error" else ""
            lines.append(f"{where:<8} result    {name} {size}B{failed}  {_clip(content)}")
        elif kind == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            detail = reason.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message")
            extra = f"  {_clip(str(detail), 300)}" if detail else ""
            lines.append(f"{where:<8} END       {reason.get('kind')}{extra}")
            lines.append("")
        elif kind == "request/context":
            window = data.get("contextWindow") or data.get("context_window")
            lines.append(f"{where:<8} context   window={window}")
        # Everything else stays in the raw JSON beside this file. Rendering all 48 event
        # kinds here would be a second, partial copy of the protocol.

    if len(events) > MAX_EVENTS:
        lines.append(f"... {len(events) - MAX_EVENTS} further events omitted (raw file has all)")
    return "\n".join(lines) + "\n"


def _text_of(message: Any) -> str:
    if not isinstance(message, dict):
        return str(message)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


async def export_trace(
    environment: Any,
    events: list[dict[str, Any]],
    *,
    label: str,
    meta: dict[str, Any] | None = None,
    raw: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write the rendered trace and the raw events into the collected artifact dir.

    Returns a small record for the result row. Failures are reported, not raised: a
    trace is diagnostic, and losing one must never turn a completed trial into a failed
    one.
    """
    import tempfile

    record: dict[str, Any] = {"events": len(events)}
    try:
        scratch = Path(tempfile.mkdtemp(prefix="xh-trace-"))
        rendered = render_trace(events, meta=meta, label=label)
        (scratch / "trace.txt").write_text(rendered, encoding="utf-8")
        (scratch / "events.json").write_text(
            json.dumps(raw if raw is not None else events, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        record["trace_bytes"] = len(rendered)

        # One exec to make the directory, then two uploads: uploading needs the parent
        # to exist, and the copy has to be a file Harbor's collector will accept.
        target = f"{ARTIFACT_DIR}/{label}"
        await environment.exec(f"mkdir -p {target}")
        await environment.upload_file(scratch / "trace.txt", f"{target}/trace.txt")
        await environment.upload_file(scratch / "events.json", f"{target}/events.json")
        code, listing = await environment.exec(f"ls -la {target}")
        record["trace_files"] = listing.strip()[:400] if code == 0 else "missing"
    except Exception as error:  # noqa: BLE001 - diagnostics must never fail a trial
        record["trace_error"] = f"{type(error).__name__}: {error}"[:300]
    return record