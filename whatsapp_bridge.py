import asyncio
import hashlib
import logging
import os
import re
import sys
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

import agent_core
from agent_core import build_options, IMAGE_MARKER
from tools.confirm import confirm_callback
from tools import cost_log


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wa-bridge")

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "4000"))

if not BRIDGE_TOKEN:
    log.error("BRIDGE_TOKEN is not set. Refusing to start without auth.")
    sys.exit(1)

app = FastAPI()

SESSION_MAX_AGE = 12 * 60 * 60  # 12 hours idle → auto-reset

_sessions: dict[str, ClaudeSDKClient] = {}
_session_meta: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


class Message(BaseModel):
    role: str
    content: str | list | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    user: str | None = None
    stream: bool | None = False


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "".join(parts)
    return ""


_META_JSON_RE = re.compile(
    r"Conversation info[^\n]*\n```json\s*(\{.*?\})\s*```",
    flags=re.DOTALL,
)
_META_SID_RE = re.compile(r'"sender_id"\s*:\s*"([^"]+)"')
_META_NAME_RE = re.compile(r'"sender"\s*:\s*"([^"]+)"')


def _extract_openclaw_sender(content: str) -> tuple[str | None, str | None]:
    """Parse OpenClaw's "Conversation info" metadata block at the start of a
    user message. Returns (sender_id, sender_name) or (None, None).

    Best-effort: try strict JSON first, fall back to regex scrape."""
    if "Conversation info" not in content[:200]:
        return None, None
    m = _META_JSON_RE.search(content)
    if m:
        try:
            meta = json.loads(m.group(1))
            return meta.get("sender_id"), meta.get("sender")
        except Exception:
            pass
    head = content[:2000]
    sid_m = _META_SID_RE.search(head)
    name_m = _META_NAME_RE.search(head)
    return (sid_m.group(1) if sid_m else None,
            name_m.group(1) if name_m else None)


def _derive_peer_identity(req: "ChatRequest") -> tuple[str | None, str | None]:
    """Return (sender_key, sender_name).

    Priority:
      1. Explicit `user` field on the request.
      2. OpenClaw's per-message "Conversation info" metadata block containing
         sender_id (E.164 phone) and sender (display name).
      3. Legacy fallback: SHA-256 of the first user message. Coarser
         (per-group-session rather than per-sender) but works even if the
         metadata block is missing.

    Returns (None, None) if no caller identity can be derived.
    """
    if req.user and req.user.strip():
        return req.user.strip(), None

    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        return None, None

    latest = _content_to_text(user_msgs[-1].content).strip()
    sid, name = _extract_openclaw_sender(latest)
    if sid:
        return sid, name

    first = _content_to_text(user_msgs[0].content).strip()
    if not first:
        return None, None
    return "wa-" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:16], None


def _derive_peer_key(req: "ChatRequest") -> str | None:
    """Back-compat wrapper. Returns just the key."""
    key, _ = _derive_peer_identity(req)
    return key


async def _expire_session(key: str):
    if key in _sessions:
        try:
            await _sessions[key].disconnect()
        except Exception:
            pass
        _sessions.pop(key, None)
        _session_meta.pop(key, None)
        log.info(f"Expired session for peer={key[:8]}...")


async def _sweep_idle_sessions():
    # Without this, peers who chat once and disappear leak SDK subprocesses
    # until the bridge is restarted — _get_session only checks idle age when
    # the same peer returns.
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for key in list(_sessions):
            meta = _session_meta.get(key, {})
            if now - meta.get("last_used", 0) <= SESSION_MAX_AGE:
                continue
            lock = _locks.get(key)
            if lock is not None and lock.locked():
                continue
            await _expire_session(key)


@app.on_event("startup")
async def _start_sweeper():
    asyncio.create_task(_sweep_idle_sessions())


async def _get_session(key: str, sender_name: str | None = None) -> ClaudeSDKClient:
    meta = _session_meta.get(key, {})
    idle = time.time() - meta.get("last_used", 0)
    if key in _sessions and idle > SESSION_MAX_AGE:
        await _expire_session(key)

    if key not in _sessions:
        options = build_options(
            mode="research",
            agent_name="sonic",
            sender_key=key,
            sender_name=sender_name,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        _sessions[key] = client
        _session_meta[key] = {"last_used": time.time(), "turns": 0}
        log.info(f"Created session for peer={key[:8]}...")
    else:
        _session_meta[key]["last_used"] = time.time()
        _session_meta[key]["turns"] += 1
    return _sessions[key]


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def _wa_confirm_stub(summary: str) -> bool:
    log.warning("Order confirmation requested via WA bridge (denied)")
    return False


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    # Auth — always required
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    # Derive per-caller session key. Prefers explicit user field; otherwise
    # parses OpenClaw's per-message metadata block for sender_id/name; falls
    # back to a hash of the first user message for legacy coverage.
    key, sender_name = _derive_peer_identity(req)
    if not key:
        raise HTTPException(status_code=400, detail="unable to derive caller identity")

    log.info(f"Request: peer={key[:8]}... msgs={len(req.messages)} stream={req.stream}")

    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="no user message")
    latest = _content_to_text(user_msgs[-1].content)

    lock = _get_lock(key)
    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model_name = req.model or "trading-agent"
    now = int(time.time())

    def _delta_chunk(delta: dict) -> str:
        chunk = {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model_name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    if req.stream:
        async def _stream():
            # Line-buffered so IMAGE_MARKER lines can be dropped whole without
            # leaking the marker token into the user-visible stream.
            yield _delta_chunk({"role": "assistant"})
            line_buf = ""
            total_chars = 0
            result_msg = None
            async with lock:
                confirm_callback.set(_wa_confirm_stub)
                agent_core.active_agent.set("sonic")
                agent_core.active_sender.set(key)
                client = await _get_session(key, sender_name)
                await client.query(latest)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                line_buf += block.text
                                while "\n" in line_buf:
                                    line, line_buf = line_buf.split("\n", 1)
                                    if IMAGE_MARKER in line:
                                        continue
                                    out = line + "\n"
                                    total_chars += len(out)
                                    yield _delta_chunk({"content": out})
                            elif isinstance(block, ToolUseBlock):
                                log.info(f"tool: {block.name}")
                                # SSE comment — ignored by OpenAI parsers but keeps
                                # the TCP stream alive during long tool calls.
                                yield f": tool {block.name}\n\n"
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg
                        break
            if line_buf and IMAGE_MARKER not in line_buf:
                total_chars += len(line_buf)
                yield _delta_chunk({"content": line_buf})
            if total_chars == 0:
                yield _delta_chunk({"content": "(no reply)"})
            cost = (result_msg.total_cost_usd or 0) if result_msg else 0
            turns = result_msg.num_turns if result_msg else 0
            log.info(f"Reply: peer={key[:8]}... chars={total_chars} turns={turns} cost=${cost:.4f} (stream)")
            try:
                cost_log.log_turn("wa", key, turns, cost, getattr(result_msg, "usage", None))
            except Exception:
                log.exception("cost_log failed")
            done = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_stream(), media_type="text/event-stream")

    async with lock:
        confirm_callback.set(_wa_confirm_stub)
        agent_core.active_agent.set("sonic")
        agent_core.active_sender.set(key)
        client = await _get_session(key, sender_name)
        await client.query(latest)

        text_buf = ""
        result_msg = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_buf += block.text
                    elif isinstance(block, ToolUseBlock):
                        log.info(f"tool: {block.name}")
            elif isinstance(msg, ResultMessage):
                result_msg = msg
                break

        reply = "\n".join(l for l in text_buf.splitlines() if IMAGE_MARKER not in l).strip()
        if not reply:
            reply = "(no reply)"

    cost = (result_msg.total_cost_usd or 0) if result_msg else 0
    turns = result_msg.num_turns if result_msg else 0
    log.info(f"Reply: peer={key[:8]}... chars={len(reply)} turns={turns} cost=${cost:.4f}")
    try:
        cost_log.log_turn("wa", key, turns, cost)
    except Exception:
        log.exception("cost_log failed")

    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": now,
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "trading-agent", "object": "model", "owned_by": "local"}],
    }


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="info")
