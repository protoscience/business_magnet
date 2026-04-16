import asyncio
import os
import logging

import discord
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)

from agent_core import build_options, IMAGE_MARKER
from tools.confirm import confirm_callback


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trading-bot")


TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",") if x.strip()
}
ALLOWED_CHANNEL_IDS = {
    int(x) for x in os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
}

DISCORD_MSG_LIMIT = 1900


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
bot = discord.Client(intents=intents)


SESSION_MAX_AGE = 6 * 60 * 60  # 6 hours idle → auto-reset

_sessions: dict[int, ClaudeSDKClient] = {}
_session_meta: dict[int, dict] = {}  # {last_used: float, turns: int}
_locks: dict[int, asyncio.Lock] = {}


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _locks:
        _locks[user_id] = asyncio.Lock()
    return _locks[user_id]


async def _expire_session(user_id: int):
    if user_id in _sessions:
        try:
            await _sessions[user_id].disconnect()
        except Exception:
            pass
        _sessions.pop(user_id, None)
        _session_meta.pop(user_id, None)
        log.info(f"Expired session for user {user_id}")


async def _get_session(user_id: int) -> ClaudeSDKClient:
    import time
    meta = _session_meta.get(user_id, {})
    idle = time.time() - meta.get("last_used", 0)
    if user_id in _sessions and idle > SESSION_MAX_AGE:
        await _expire_session(user_id)

    if user_id not in _sessions:
        client = ClaudeSDKClient(options=build_options())
        await client.connect()
        _sessions[user_id] = client
        _session_meta[user_id] = {"last_used": time.time(), "turns": 0}
        log.info(f"Created Claude session for user {user_id}")
    else:
        _session_meta[user_id]["last_used"] = time.time()
        _session_meta[user_id]["turns"] += 1
    return _sessions[user_id]


class ConfirmView(discord.ui.View):
    def __init__(self, allowed_user_id: int, summary: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.allowed_user_id = allowed_user_id
        self.summary = summary
        self.result: bool | None = None
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message("Not your order.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self._event.set()
        await interaction.response.edit_message(content=f"✅ **Confirmed**\n{self.summary}", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self._event.set()
        await interaction.response.edit_message(content=f"❌ **Cancelled**\n{self.summary}", view=None)
        self.stop()

    async def on_timeout(self):
        self.result = False
        self._event.set()


def _make_discord_confirm(channel: discord.abc.Messageable, user_id: int):
    async def confirm(summary: str) -> bool:
        view = ConfirmView(allowed_user_id=user_id, summary=summary)
        await channel.send(f"🟡 **Proposed order**\n```{summary}```", view=view)
        await view._event.wait()
        return view.result is True
    return confirm


async def _send_chunks(channel: discord.abc.Messageable, text: str):
    text = text.strip()
    if not text:
        return
    while text:
        chunk, text = text[:DISCORD_MSG_LIMIT], text[DISCORD_MSG_LIMIT:]
        await channel.send(chunk)


async def handle_message(message: discord.Message):
    user_id = message.author.id
    channel = message.channel

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        log.warning(f"Rejected message from unauthorized user {user_id} ({message.author})")
        return

    content = message.content.strip()
    if not content:
        return

    if content.lower() in ("/reset", "!reset"):
        if user_id in _sessions:
            try:
                await _sessions[user_id].disconnect()
            except Exception:
                pass
            _sessions.pop(user_id, None)
        await channel.send("🔄 Conversation reset.")
        return

    lock = _get_lock(user_id)
    if lock.locked():
        await channel.send("⏳ Still working on your previous message — hang on.")
        return

    async with lock:
        async with channel.typing():
            confirm_callback.set(_make_discord_confirm(channel, user_id))
            client = await _get_session(user_id)

            try:
                await client.query(content)

                buffer = ""
                image_paths: list[str] = []
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                buffer += block.text
                            elif isinstance(block, ToolUseBlock):
                                log.info(f"tool: {block.name} {block.input}")
                    elif isinstance(msg, UserMessage):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                for item in (block.content or []):
                                    text = item.get("text") if isinstance(item, dict) else None
                                    if text and IMAGE_MARKER in text:
                                        for line in text.splitlines():
                                            if line.startswith(IMAGE_MARKER):
                                                image_paths.append(line[len(IMAGE_MARKER):].strip())
                    elif isinstance(msg, ResultMessage):
                        cleaned = "\n".join(
                            l for l in buffer.splitlines()
                            if IMAGE_MARKER not in l
                        ).strip()
                        if cleaned:
                            await _send_chunks(channel, cleaned)
                        for p in image_paths:
                            if os.path.exists(p):
                                await channel.send(file=discord.File(p))
                        buffer = ""
                        image_paths = []
                        cost = msg.total_cost_usd or 0
                        log.info(f"user={user_id} turns={msg.num_turns} cost=${cost:.4f}")
                        break
            except Exception as e:
                log.exception("Agent error")
                await channel.send(f"⚠️ Error: `{type(e).__name__}: {e}`")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")
    log.info(f"Allowlist: {ALLOWED_USER_IDS or 'EMPTY (no users permitted)'}")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions
    is_allowed_channel = message.channel.id in ALLOWED_CHANNEL_IDS
    log.info(
        f"seen msg from {message.author} (id={message.author.id}) "
        f"channel={message.channel.id} dm={is_dm} mention={is_mention} "
        f"allowed_channel={is_allowed_channel} content={message.content!r}"
    )
    if not (is_dm or is_mention or is_allowed_channel):
        return
    await handle_message(message)


@bot.event
async def on_disconnect():
    log.warning("Gateway disconnected")


@bot.event
async def on_resumed():
    log.info("Gateway resumed")


if __name__ == "__main__":
    if not ALLOWED_USER_IDS:
        log.warning("DISCORD_ALLOWED_USER_IDS is empty — nobody will be allowed to use the bot.")
    bot.run(TOKEN)
