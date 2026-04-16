import asyncio
import hashlib
import logging
import os
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

from agent_core import build_options, IMAGE_MARKER
from tools.confirm import confirm_callback


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wa-bridge")

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "4000"))
MAX_REQUEST_MESSAGES = 50
MAX_REQUESTS_PER_MINUTE = 10

if not BRIDGE_TOKEN:
    log.error("BRIDGE_TOKEN is not set. Refusing to start without auth.")
    sys.exit(1)

app = FastAPI()

SESSION_MAX_AGE = 6 * 60 * 60  # 6 hours idle → auto-reset

_sessions: dict[str, ClaudeSDKClient] = {}
_session_meta: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}
_rate_window: dict[str, list[float]] = {}


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


def _derive_peer_key(req: "ChatRequest") -> str | None:
    """
    Derive a stable per-caller key.

    Priority:
      1. explicit `user` field on the request (if a future OpenClaw version
         learns to forward sender identity, or when tested via curl).
      2. SHA-256 of the FIRST user message in the replayed history.
         OpenClaw replays the full history per WhatsApp sender on every
         request, so messages[0] is stable within a user's ongoing
         conversation and ~unique between distinct WhatsApp users.

    Returns None if neither is available (caller should 400).
    """
    if req.user and req.user.strip():
        return req.user.strip()

    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        return None
    first = _content_to_text(user_msgs[0].content).strip()
    if not first:
        return None
    return "wa-" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:16]


def _check_rate_limit(key: str) -> bool:
    now = time.time()
    window = _rate_window.setdefault(key, [])
    window[:] = [t for t in window if now - t < 60]
    if len(window) >= MAX_REQUESTS_PER_MINUTE:
        return False
    window.append(now)
    return True


async def _expire_session(key: str):
    if key in _sessions:
        try:
            await _sessions[key].disconnect()
        except Exception:
            pass
        _sessions.pop(key, None)
        _session_meta.pop(key, None)
        log.info(f"Expired session for peer={key[:8]}...")


async def _get_session(key: str) -> ClaudeSDKClient:
    meta = _session_meta.get(key, {})
    idle = time.time() - meta.get("last_used", 0)
    if key in _sessions and idle > SESSION_MAX_AGE:
        await _expire_session(key)

    if key not in _sessions:
        client = ClaudeSDKClient(options=build_options(mode="research"))
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
    # hashes the first user message (stable per WhatsApp sender because
    # OpenClaw replays full history each request).
    key = _derive_peer_key(req)
    if not key:
        raise HTTPException(status_code=400, detail="unable to derive caller identity")

    # Rate limit per caller
    if not _check_rate_limit(key):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # Message count cap
    if len(req.messages) > MAX_REQUEST_MESSAGES:
        raise HTTPException(status_code=400, detail=f"max {MAX_REQUEST_MESSAGES} messages")

    log.info(f"Request: peer={key[:8]}... msgs={len(req.messages)} stream={req.stream}")

    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="no user message")
    latest = _content_to_text(user_msgs[-1].content)

    lock = _get_lock(key)
    async with lock:
        confirm_callback.set(_wa_confirm_stub)
        client = await _get_session(key)
        await client.query(latest)

        text_buf = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_buf += block.text
                    elif isinstance(block, ToolUseBlock):
                        log.info(f"tool: {block.name}")
            elif isinstance(msg, ResultMessage):
                break

        reply = "\n".join(l for l in text_buf.splitlines() if IMAGE_MARKER not in l).strip()
        if not reply:
            reply = "(no reply)"

    log.info(f"Reply: peer={key[:8]}... chars={len(reply)}")

    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model_name = req.model or "trading-agent"
    now = int(time.time())

    if req.stream:
        async def _sse():
            chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            done = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream")

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
