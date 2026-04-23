"""Per-sender memory for the agents.

Each agent (sonic, supersonic) has its own siloed memory tree:
    logs/memory/<agent>/<sender_key>/MEMORY.md

`sender_key` is the parsed E.164 phone for WhatsApp (e.g. "+14084258476")
or "discord:<user_id>" for Discord. Sanitized for filesystem safety.

MEMORY.md is plain markdown — one fact per bullet, prefixed with a date
stamp. Human-readable and hand-editable. Injected into the system prompt
at turn start.

All functions are best-effort: failures log and return sensible defaults
so a conversation turn never crashes because of memory.
"""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

MEMORY_ROOT = Path(__file__).resolve().parent.parent / "logs" / "memory"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Cap per-sender bullets to keep prompt injection bounded. Oldest fall off.
MAX_FACTS = 50

log = logging.getLogger("memory")

_SAFE = re.compile(r"[^A-Za-z0-9+_:-]")


def _safe(s: str) -> str:
    return _SAFE.sub("_", s) or "unknown"


def memory_path(agent: str, sender_key: str) -> Path:
    return MEMORY_ROOT / _safe(agent) / _safe(sender_key) / "MEMORY.md"


def soul_path(agent: str) -> Path:
    return PROMPTS_DIR / f"{_safe(agent)}_soul.md"


def load_soul(agent: str) -> str:
    try:
        return soul_path(agent).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        log.exception(f"soul read failed for {agent}")
        return ""


def load_memory(agent: str, sender_key: str) -> str:
    try:
        p = memory_path(agent, sender_key)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        log.exception(f"memory read failed for {agent}/{sender_key}")
        return ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_duplicate(fact: str, existing: str) -> bool:
    norm_new = _normalize(fact)
    for line in existing.splitlines():
        core = re.sub(r"^\s*[-*]\s*(?:\[[^\]]+\]\s*)?", "", line)
        if _normalize(core) == norm_new:
            return True
    return False


def _cap_bullets(md: str) -> str:
    lines = md.splitlines()
    bullet_idx = [i for i, l in enumerate(lines) if l.lstrip().startswith("- ")]
    if len(bullet_idx) <= MAX_FACTS:
        return md
    keep = set(bullet_idx[-MAX_FACTS:])
    out = [l for i, l in enumerate(lines) if (i not in bullet_idx) or (i in keep)]
    return "\n".join(out).strip() + "\n"


def append_fact(agent: str, sender_key: str, fact: str) -> bool:
    """Append a fact to a sender's MEMORY.md.

    - Silently skips if the fact already exists (normalized comparison).
    - Light dedup only — does not do semantic merging.
    - Caps total bullets at MAX_FACTS; oldest drop off.

    Returns True if a new line was written, False otherwise.
    """
    fact = (fact or "").strip()
    if not fact:
        return False
    try:
        p = memory_path(agent, sender_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        if _is_duplicate(fact, existing):
            return False
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_content = existing.rstrip() + f"\n- [{ts}] {fact}\n"
        p.write_text(_cap_bullets(new_content), encoding="utf-8")
        return True
    except Exception:
        log.exception(f"memory write failed for {agent}/{sender_key}")
        return False


def build_preamble(agent: str, sender_key: str | None, sender_name: str | None) -> str:
    """Return the soul + memory preamble to prepend to the system prompt.

    Empty string if nothing to inject. Never raises.
    """
    parts: list[str] = []
    soul = load_soul(agent)
    if soul:
        parts.append("# Who you are (your soul)\n\n" + soul)
    if sender_key:
        mem = load_memory(agent, sender_key)
        if mem:
            header = f"# What you remember about this person"
            if sender_name:
                header += f" ({sender_name})"
            parts.append(f"{header}\n\n{mem}")
        elif sender_name:
            parts.append(
                f"# Who you're talking to\n\n"
                f"Their name: {sender_name}. You haven't built a memory for them yet."
            )
    return "\n\n---\n\n".join(parts)
