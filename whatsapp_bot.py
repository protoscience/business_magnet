import asyncio
import json
import logging
import os

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

from agent_core import build_options
from tools.confirm import confirm_callback


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("whatsapp-bot")


GATEWAY_URL = os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3000").rstrip("/")
GATEWAY_WS = (
    GATEWAY_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
)

ALLOWED_NUMBERS = {
    x.strip().lstrip("+")
    for x in os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").split(",")
    if x.strip()
}

CONFIRM_TIMEOUT_SEC = int(os.environ.get("WHATSAPP_CONFIRM_TIMEOUT", "120"))


_sessions: dict[str, ClaudeSDKClient] = {}
_locks: dict[str, asyncio.Lock] = {}
_pending_confirms: dict[str, asyncio.Future] = {}


def _number_from_jid(jid: str) -> str:
    return jid.split("@", 1)[0].split(":", 1)[0]


def _is_allowed(jid: str) -> bool:
    if not ALLOWED_NUMBERS:
        return False
    return _number_from_jid(jid) in ALLOWED_NUMBERS


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def _get_session(key: str) -> ClaudeSDKClient:
    if key not in _sessions:
        client = ClaudeSDKClient(options=build_options())
        await client.connect()
        _sessions[key] = client
        log.info(f"Created Claude session for {key}")
    return _sessions[key]


async def send_whatsapp(to: str, text: str):
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{GATEWAY_URL}/send", json={"to": to, "text": text})
        r.raise_for_status()


def _make_confirm(reply_jid: str):
    async def confirm(summary: str) -> bool:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _pending_confirms[reply_jid] = fut
        try:
            await send_whatsapp(
                reply_jid,
                f"🟡 *Proposed order*\n{summary}\n\nReply *CONFIRM* or *CANCEL* "
                f"(timeout {CONFIRM_TIMEOUT_SEC}s).",
            )
            try:
                return await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await send_whatsapp(reply_jid, "⏰ Confirmation timed out — order cancelled.")
                return False
        finally:
            _pending_confirms.pop(reply_jid, None)
    return confirm


async def handle_message(evt: dict):
    jid = evt["jid"]
    participant = evt.get("participant") or jid
    text = (evt.get("text") or "").strip()
    is_group = evt.get("isGroup", False)

    if not _is_allowed(participant):
        log.warning(f"Rejected message from {participant} ({text[:40]})")
        return

    if not text:
        return

    reply_jid = jid
    session_key = participant

    if session_key in _pending_confirms:
        fut = _pending_confirms[session_key]
        ans = text.strip().upper()
        if ans.startswith("CONFIRM") or ans in ("YES", "Y", "OK"):
            if not fut.done():
                fut.set_result(True)
        elif ans.startswith("CANCEL") or ans in ("NO", "N"):
            if not fut.done():
                fut.set_result(False)
        else:
            await send_whatsapp(reply_jid, "Please reply *CONFIRM* or *CANCEL*.")
        return

    if text.lower() in ("/reset", "!reset", "reset"):
        if session_key in _sessions:
            try:
                await _sessions[session_key].disconnect()
            except Exception:
                pass
            _sessions.pop(session_key, None)
        await send_whatsapp(reply_jid, "🔄 Conversation reset.")
        return

    lock = _get_lock(session_key)
    if lock.locked():
        await send_whatsapp(reply_jid, "⏳ Still working on your previous message — hang on.")
        return

    async with lock:
        confirm_callback.set(_make_confirm(reply_jid))
        client = await _get_session(session_key)

        try:
            await client.query(text)
            buffer = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            buffer += block.text
                        elif isinstance(block, ToolUseBlock):
                            log.info(f"tool: {block.name} {block.input}")
                elif isinstance(msg, ResultMessage):
                    if buffer.strip():
                        await send_whatsapp(reply_jid, buffer.strip())
                    cost = msg.total_cost_usd or 0
                    log.info(
                        f"jid={session_key} turns={msg.num_turns} cost=${cost:.4f}"
                    )
                    break
        except Exception as e:
            log.exception("Agent error")
            await send_whatsapp(reply_jid, f"⚠️ Error: {type(e).__name__}: {e}")


async def main():
    if not ALLOWED_NUMBERS:
        log.warning(
            "WHATSAPP_ALLOWED_NUMBERS empty — nobody will be allowed to use the bot."
        )
    else:
        log.info(f"Allowlist: {ALLOWED_NUMBERS}")

    while True:
        try:
            log.info(f"Connecting to gateway: {GATEWAY_WS}")
            async with websockets.connect(GATEWAY_WS, ping_interval=20) as ws:
                log.info("Connected to gateway")
                async for raw in ws:
                    try:
                        evt = json.loads(raw)
                        if evt.get("type") == "message":
                            asyncio.create_task(handle_message(evt))
                        elif evt.get("type") == "status":
                            log.info(
                                f"Gateway status: connected={evt.get('connected')} "
                                f"me={evt.get('me', {}).get('id') if evt.get('me') else None}"
                            )
                    except Exception:
                        log.exception("Event handler error")
        except (websockets.ConnectionClosed, OSError, ConnectionRefusedError) as e:
            log.warning(f"Gateway connection lost: {e} — retrying in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
