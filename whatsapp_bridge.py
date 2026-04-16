import asyncio
import logging
import os
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

app = FastAPI()

SESSION_MAX_AGE = 6 * 60 * 60  # 6 hours idle → auto-reset

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


def _peer_key(req: ChatRequest) -> str:
    return req.user or "default"


async def _expire_session(key: str):
    if key in _sessions:
        try:
            await _sessions[key].disconnect()
        except Exception:
            pass
        _sessions.pop(key, None)
        _session_meta.pop(key, None)
        log.info(f"Expired session for {key}")


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
        log.info(f"Created session for {key}")
    else:
        _session_meta[key]["last_used"] = time.time()
        _session_meta[key]["turns"] += 1
    return _sessions[key]


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def _wa_confirm_stub(summary: str) -> bool:
    log.warning(f"Order confirmation requested but no WA confirm path: {summary}")
    return False


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    if BRIDGE_TOKEN:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != BRIDGE_TOKEN:
            raise HTTPException(status_code=401, detail="bad token")

    log.info(f"Request: model={req.model} stream={req.stream} user={req.user} msgs={len(req.messages)}")
    for m in req.messages:
        log.info(f"  [{m.role}] {_content_to_text(m.content)[:120]}")

    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="no user message")
    latest = _content_to_text(user_msgs[-1].content)

    key = _peer_key(req)
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

    log.info(f"Reply ({len(reply)} chars): {reply[:200]}")

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
