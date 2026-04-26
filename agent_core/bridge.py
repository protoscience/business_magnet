"""WhatsApp bridge — OpenAI-compatible chat-completions API.

Designed to sit behind OpenClaw (a WhatsApp gateway running on a VPS)
that POSTs the conversation to /v1/chat/completions on every turn.

Sessions are keyed by a stable per-peer hash so each WhatsApp sender
gets their own ClaudeSDKClient with the right per-sender memory.

Public API:
    run_whatsapp_bridge(build_opts, *, port=4000, token=None,
                        session_max_age_seconds=12*3600,
                        confirm_callback=None)

`build_opts` is `Callable[[*, sender_key, sender_name], ClaudeAgentOptions]`
— the framework calls it once per new session.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from typing import Callable, Awaitable

import httpx

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    ClaudeAgentOptions,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from agent_core.context import IMAGE_MARKER
from agent_core.tools import cost_log
from agent_core.tools.confirm import confirm_callback as confirm_cb_ctx, deny_confirm


log = logging.getLogger("agent-core.bridge")


BuildOptsFn = Callable[..., ClaudeAgentOptions]
ConfirmFn = Callable[[str], Awaitable[bool]]


class _Message(BaseModel):
    role: str
    content: str | list | None = None


class _ChatRequest(BaseModel):
    model: str | None = None
    messages: list[_Message]
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


def _derive_peer_key(req: _ChatRequest) -> str | None:
    """Stable per-caller identity.

    Priority:
      1. explicit `user` field (forwarded by some gateways, useful for curl).
      2. SHA-256 of the first user message — gateways like OpenClaw replay
         the full history per WhatsApp sender on every request, so messages[0]
         is stable per sender.
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


def run_whatsapp_bridge(
    build_opts: BuildOptsFn,
    *,
    port: int = 4000,
    token: str | None = None,
    session_max_age_seconds: int = 12 * 60 * 60,
    confirm_callback: ConfirmFn | None = None,
    model_id: str = "agent-core",
    log_channel: str = "wa",
    # Baileys-gateway integration. When all three are set, the bridge ALSO
    # mounts POST /wa-inbound — accepts webhook payloads from
    # baileys-gateway, runs the agent, dispatches text + image replies
    # back to the gateway's /send-text and /send-image endpoints. The
    # legacy /v1/chat/completions endpoint stays mounted (OpenClaw uses
    # it; preserving it keeps the migration rollback path working).
    gateway_url: str | None = None,
    gateway_api_key: str | None = None,
    webhook_key: str | None = None,
) -> None:
    """Run the WhatsApp bridge server (blocks until killed).

    Args:
        build_opts: Per-session options factory. Called as
            `build_opts(sender_key=..., sender_name=...)` for each new peer.
        port: TCP port to bind on 127.0.0.1. Default 4000.
        token: Bearer token required on /v1/chat/completions. If None, falls
            back to the BRIDGE_TOKEN env var. The server refuses to start
            without one.
        session_max_age_seconds: Idle-session TTL. Default 12h.
        confirm_callback: Async callback for `place_order`-style tools that
            need human approval. The default denies all confirmations
            (WhatsApp has no UI for confirming risky actions).
        model_id: Value reported by GET /v1/models. Cosmetic.
        log_channel: Tag passed to cost_log.log_turn(). Default "wa".
        gateway_url: When set with gateway_api_key, enables the
            POST /wa-inbound webhook endpoint and uses this URL to call
            back the gateway's /send-text and /send-image endpoints.
            Falls back to GATEWAY_URL env var.
        gateway_api_key: x-api-key header value used when calling the
            gateway. Falls back to GATEWAY_API_KEY env var.
        webhook_key: x-webhook-key required on incoming /wa-inbound
            requests. Falls back to WEBHOOK_KEY env var. Empty = no auth
            on the webhook (do not use in production).
    """
    if token is None:
        token = os.environ.get("BRIDGE_TOKEN", "")
    if not token:
        log.error("run_whatsapp_bridge: no token (pass token= or set BRIDGE_TOKEN)")
        sys.exit(1)

    if confirm_callback is None:
        confirm_callback = deny_confirm

    if gateway_url is None:
        gateway_url = os.environ.get("GATEWAY_URL", "").strip() or None
    if gateway_api_key is None:
        gateway_api_key = os.environ.get("GATEWAY_API_KEY", "").strip() or None
    if webhook_key is None:
        webhook_key = os.environ.get("WEBHOOK_KEY", "").strip() or None
    webhook_enabled = bool(gateway_url and gateway_api_key)

    sessions: dict[str, ClaudeSDKClient] = {}
    session_meta: dict[str, dict] = {}
    locks: dict[str, asyncio.Lock] = {}

    # Inbound message dedup for /wa-inbound. WhatsApp + LID transition
    # delivers the same message twice (same id, different chat JIDs).
    seen_message_ids: list[str] = []      # LRU order
    seen_message_set: set[str] = set()
    SEEN_CAP = 500

    def _mark_seen(mid: str) -> bool:
        if not mid:
            return True
        if mid in seen_message_set:
            return False
        seen_message_set.add(mid)
        seen_message_ids.append(mid)
        while len(seen_message_ids) > SEEN_CAP:
            old = seen_message_ids.pop(0)
            seen_message_set.discard(old)
        return True

    app = FastAPI()

    async def _expire_session(key: str):
        if key in sessions:
            try:
                await sessions[key].disconnect()
            except Exception:
                pass
            sessions.pop(key, None)
            session_meta.pop(key, None)
            log.info(f"Expired session for peer={key[:8]}...")

    async def _sweep_idle_sessions():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for key in list(sessions):
                meta = session_meta.get(key, {})
                if now - meta.get("last_used", 0) <= session_max_age_seconds:
                    continue
                lock = locks.get(key)
                if lock is not None and lock.locked():
                    continue
                await _expire_session(key)

    async def _get_session(key: str, sender_name: str | None = None) -> ClaudeSDKClient:
        meta = session_meta.get(key, {})
        idle = time.time() - meta.get("last_used", 0)
        if key in sessions and idle > session_max_age_seconds:
            await _expire_session(key)

        if key not in sessions:
            options = build_opts(sender_key=key, sender_name=sender_name)
            client = ClaudeSDKClient(options=options)
            await client.connect()
            sessions[key] = client
            session_meta[key] = {"last_used": time.time(), "turns": 0}
            log.info(f"Created session for peer={key[:8]}...")
        else:
            session_meta[key]["last_used"] = time.time()
            session_meta[key]["turns"] += 1
        return sessions[key]

    def _get_lock(key: str) -> asyncio.Lock:
        if key not in locks:
            locks[key] = asyncio.Lock()
        return locks[key]

    @app.on_event("startup")
    async def _start_sweeper():
        asyncio.create_task(_sweep_idle_sessions())

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(req: _ChatRequest, request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != token:
            raise HTTPException(status_code=401, detail="unauthorized")

        key = _derive_peer_key(req)
        if not key:
            raise HTTPException(status_code=400, detail="unable to derive caller identity")

        log.info(f"Request: peer={key[:8]}... msgs={len(req.messages)} stream={req.stream}")

        user_msgs = [m for m in req.messages if m.role == "user"]
        if not user_msgs:
            raise HTTPException(status_code=400, detail="no user message")
        latest = _content_to_text(user_msgs[-1].content)

        lock = _get_lock(key)
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model_name = req.model or model_id
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
                yield _delta_chunk({"role": "assistant"})
                line_buf = ""
                total_chars = 0
                result_msg = None
                async with lock:
                    confirm_cb_ctx.set(confirm_callback)
                    client = await _get_session(key)
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
                    cost_log.log_turn(log_channel, key, turns, cost, getattr(result_msg, "usage", None))
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
            confirm_cb_ctx.set(confirm_callback)
            client = await _get_session(key)
            await client.query(latest)
            text_buf = ""
            # IMAGE_MARKER paths surface in TWO places — sometimes Claude
            # echoes them in his TextBlock output, but the canonical source
            # is the ToolResult content of image-emitting tools (e.g.
            # create_price_chart, create_analysis_image). Mirror the Discord
            # bot's pattern: collect from tool results AND filter out any
            # echoed copies from the assistant text.
            image_paths: list[str] = []
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_buf += block.text
                        elif isinstance(block, ToolUseBlock):
                            log.info(f"tool: {block.name}")
                elif isinstance(msg, UserMessage):
                    # Tool results come back as UserMessage with ToolResultBlock
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            for item in (block.content or []):
                                t = item.get("text") if isinstance(item, dict) else None
                                if t and IMAGE_MARKER in t:
                                    for line in t.splitlines():
                                        if IMAGE_MARKER in line:
                                            idx = line.index(IMAGE_MARKER) + len(IMAGE_MARKER)
                                            p = line[idx:].strip()
                                            if p and p not in image_paths:
                                                image_paths.append(p)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
                    break
            # Strip any IMAGE_MARKER echoes Claude may have included in the
            # text. Webhook-style consumers (Baileys gateway etc.) read
            # `image_paths` and dispatch each PNG via their own image-send
            # endpoint. OpenAI clients (OpenClaw) ignore unknown fields and
            # see only `reply`.
            clean_lines: list[str] = []
            for line in text_buf.splitlines():
                if IMAGE_MARKER in line:
                    # Defensive: also pick up any echoes here just in case
                    idx = line.index(IMAGE_MARKER) + len(IMAGE_MARKER)
                    p = line[idx:].strip()
                    if p and p not in image_paths:
                        image_paths.append(p)
                else:
                    clean_lines.append(line)
            reply = "\n".join(clean_lines).strip()
            if not reply:
                reply = "(no reply)"

        cost = (result_msg.total_cost_usd or 0) if result_msg else 0
        turns = result_msg.num_turns if result_msg else 0
        log.info(
            f"Reply: peer={key[:8]}... chars={len(reply)} images={len(image_paths)} "
            f"turns={turns} cost=${cost:.4f}"
        )
        try:
            cost_log.log_turn(log_channel, key, turns, cost)
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
            # Extension: list of local PNG paths the agent generated this turn.
            # Standard OpenAI clients ignore this; webhook consumers read it
            # and dispatch each path through their image-send pipeline.
            "image_paths": image_paths,
        }

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [{"id": model_id, "object": "model", "owned_by": "local"}],
        }

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "webhook_enabled": webhook_enabled,
        }

    # ── /wa-inbound webhook (optional, only if gateway config provided) ──
    async def _gateway_post(client: httpx.AsyncClient, path: str,
                            body: dict, timeout: float = 60.0) -> int:
        try:
            r = await client.post(
                f"{gateway_url}{path}",
                json=body,
                headers={"x-api-key": gateway_api_key or ""},
                timeout=timeout,
            )
            return r.status_code
        except Exception as exc:
            log.warning(f"gateway POST {path} failed: {type(exc).__name__}: {exc}")
            return 0

    async def _typing_pulse(chat_id: str, stop: asyncio.Event) -> None:
        """Refresh `composing` presence every 8s while we wait on Claude.
        WhatsApp auto-clears after ~10s of silence."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            await _gateway_post(client, "/presence",
                                {"to": chat_id, "state": "composing"}, timeout=5.0)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    await _gateway_post(client, "/presence",
                                        {"to": chat_id, "state": "composing"},
                                        timeout=5.0)

    async def _process_inbound(*, chat_id: str, sender_key: str,
                               sender_name: str | None, text: str) -> None:
        """Run one Claude turn for the inbound message and dispatch the reply."""
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_typing_pulse(chat_id, stop_typing))
        lock = _get_lock(sender_key)
        try:
            async with lock:
                confirm_cb_ctx.set(confirm_callback)
                client_sdk = await _get_session(sender_key, sender_name=sender_name)
                await client_sdk.query(text)
                text_buf = ""
                image_paths: list[str] = []
                result_msg = None
                async for msg in client_sdk.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text_buf += block.text
                            elif isinstance(block, ToolUseBlock):
                                log.info(f"tool: {block.name}")
                    elif isinstance(msg, UserMessage):
                        for block in getattr(msg, "content", []) or []:
                            if isinstance(block, ToolResultBlock):
                                for item in (block.content or []):
                                    t = item.get("text") if isinstance(item, dict) else None
                                    if t and IMAGE_MARKER in t:
                                        for line in t.splitlines():
                                            if IMAGE_MARKER in line:
                                                idx = line.index(IMAGE_MARKER) + len(IMAGE_MARKER)
                                                p = line[idx:].strip()
                                                if p and p not in image_paths:
                                                    image_paths.append(p)
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg
                        break
                clean_lines = []
                for line in text_buf.splitlines():
                    if IMAGE_MARKER in line:
                        idx = line.index(IMAGE_MARKER) + len(IMAGE_MARKER)
                        p = line[idx:].strip()
                        if p and p not in image_paths:
                            image_paths.append(p)
                    else:
                        clean_lines.append(line)
                reply_text = "\n".join(clean_lines).strip() or "(no reply)"

            cost = (result_msg.total_cost_usd or 0) if result_msg else 0
            turns = result_msg.num_turns if result_msg else 0
            log.info(
                f"Reply (wa-inbound): peer={sender_key} chars={len(reply_text)} "
                f"images={len(image_paths)} turns={turns} cost=${cost:.4f}"
            )
            try:
                cost_log.log_turn(log_channel, sender_key, turns, cost,
                                  getattr(result_msg, "usage", None))
            except Exception:
                log.exception("cost_log failed")
        except Exception:
            log.exception(f"agent run failed for chat {chat_id}")
            reply_text = "[bridge error — see logs]"
            image_paths = []
        finally:
            stop_typing.set()
            try:
                await asyncio.wait_for(typing_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        # Dispatch reply via gateway. Text first so the user sees the
        # caption before the chart loads.
        async with httpx.AsyncClient(timeout=60.0) as client_http:
            await _gateway_post(client_http, "/presence",
                                {"to": chat_id, "state": "paused"}, timeout=5.0)
            sc = await _gateway_post(client_http, "/send-text",
                                     {"to": chat_id, "text": reply_text})
            log.info(f"send-text → {chat_id} status={sc}")
            for path in image_paths:
                isc = await _gateway_post(client_http, "/send-image",
                                          {"to": chat_id, "path": path})
                log.info(f"send-image → {chat_id} path={path} status={isc}")

    if webhook_enabled:
        @app.post("/wa-inbound")
        async def wa_inbound(req: Request,
                             x_webhook_key: str | None = Header(default=None)):
            if webhook_key and x_webhook_key != webhook_key:
                raise HTTPException(status_code=401, detail="bad webhook key")
            payload = await req.json()
            text = (payload.get("text") or "").strip()
            chat_id = payload.get("chat_id")
            sender_id = payload.get("sender_id") or "unknown"
            sender_name = payload.get("sender_name")
            if not text or not chat_id:
                # Empty text == typically the first decrypt-attempt before
                # Baileys has the Signal session. The retry comes a few
                # hundred ms later with the real text. Don't dedup empties.
                return {"ok": True, "noted": "empty"}

            mid = payload.get("message_id") or ""
            if not _mark_seen(mid):
                return {"ok": True, "duplicate": True}

            log.info(f"Inbound (wa-inbound): chat={chat_id} sender={sender_id} "
                     f"is_group={payload.get('is_group')} mention={payload.get('mentioned_bot')} "
                     f"text={text[:80]!r}")
            # Run agent + dispatch in the background so the gateway doesn't
            # block on a 30+ second Claude turn. Webhook returns immediately.
            asyncio.create_task(_process_inbound(
                chat_id=chat_id,
                sender_key=sender_id,
                sender_name=sender_name,
                text=text,
            ))
            return {"ok": True, "queued": True}
    else:
        log.info("/wa-inbound NOT mounted — gateway_url and gateway_api_key required")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
