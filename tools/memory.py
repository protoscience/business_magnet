"""Per-sender memory for the agents.

Each agent (sonic, supersonic) has its own siloed memory tree:
    logs/memory/<agent>/<sender_key>/MEMORY.md

`sender_key` is the parsed E.164 phone for WhatsApp (e.g. "+14084258476")
or "discord:<user_id>" for Discord. Sanitized for filesystem safety.

MEMORY.md is plain markdown — one fact per bullet, prefixed with a date
stamp. Human-readable and hand-editable. Injected into the system prompt
at session creation.

Retention rules:
- A fact carries its last-refresh date in the [YYYY-MM-DD] prefix.
- Facts silent for FACT_TTL_DAYS (60) are dropped on the next write or
  the next read — whichever comes first. Filter-on-read keeps what the
  agent sees consistent even between writes; write-time cleanup keeps
  the on-disk file compact.
- When a fact is re-asserted via append_fact(), the existing entry is
  removed and a new one appended with today's date — a reinforced fact
  never ages out.

All functions are best-effort: failures log and return sensible defaults
so a conversation turn never crashes because of memory.
"""
import logging
import re
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

MEMORY_ROOT = Path(__file__).resolve().parent.parent / "logs" / "memory"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Cap per-sender bullets so the prompt injection stays bounded. Oldest fall off.
MAX_FACTS = 50

# Facts silent for this many days are forgotten. Mentioning a fact again
# refreshes its date, so active facts never age out.
FACT_TTL_DAYS = 60

log = logging.getLogger("memory")

_SAFE = re.compile(r"[^A-Za-z0-9+_:-]")
_DATE_PREFIX = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\]\s*")
_BULLET = re.compile(r"^\s*[-*]\s+")


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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _bullet_core(line: str) -> str:
    """Strip bullet marker and date prefix to get the fact text."""
    return re.sub(r"^\s*[-*]\s*(?:\[[^\]]+\]\s*)?", "", line)


def _parse_date(line: str) -> date | None:
    m = _DATE_PREFIX.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _filter_stale(md: str, today: date | None = None, ttl_days: int = FACT_TTL_DAYS) -> str:
    """Return the markdown with stale bullets dropped.

    A bullet is stale if its [YYYY-MM-DD] prefix is older than ttl_days.
    Bullets without a parseable date are kept (conservative — might be
    hand-edited by the user).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=ttl_days)
    out = []
    for line in md.splitlines():
        if _BULLET.match(line):
            d = _parse_date(line)
            if d is not None and d < cutoff:
                continue
        out.append(line)
    return "\n".join(out)


def _cap_bullets(md: str) -> str:
    lines = md.splitlines()
    bullet_idx = [i for i, l in enumerate(lines) if _BULLET.match(l)]
    if len(bullet_idx) <= MAX_FACTS:
        return md
    keep = set(bullet_idx[-MAX_FACTS:])
    out = [l for i, l in enumerate(lines) if (i not in bullet_idx) or (i in keep)]
    return "\n".join(out).strip() + "\n"


def load_memory(agent: str, sender_key: str) -> str:
    """Return the sender's memory markdown with stale bullets filtered.

    Read-side filter only — does not write back to disk. The next
    append_fact() call will compact the file on disk.
    """
    try:
        p = memory_path(agent, sender_key)
        if not p.exists():
            return ""
        raw = p.read_text(encoding="utf-8")
        return _filter_stale(raw).strip()
    except Exception:
        log.exception(f"memory read failed for {agent}/{sender_key}")
        return ""


def append_fact(agent: str, sender_key: str, fact: str) -> bool:
    """Append a fact; refresh the date if the fact already exists.

    Also compacts the file: drops stale (>FACT_TTL_DAYS old) entries and
    caps at MAX_FACTS (oldest drop off).

    Returns True if the file was written (either a new fact or a refresh),
    False if the fact was empty or a write failed.
    """
    fact = (fact or "").strip()
    if not fact:
        return False
    try:
        p = memory_path(agent, sender_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""

        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=FACT_TTL_DAYS)
        norm_new = _normalize(fact)

        kept_lines: list[str] = []
        for line in existing.splitlines():
            if _BULLET.match(line):
                d = _parse_date(line)
                if d is not None and d < cutoff:
                    continue  # drop stale
                if _normalize(_bullet_core(line)) == norm_new:
                    continue  # drop duplicate — new one takes its place below
            kept_lines.append(line)

        ts = today.strftime("%Y-%m-%d")
        new_content = ("\n".join(kept_lines)).rstrip() + f"\n- [{ts}] {fact}\n"
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
            header = "# What you remember about this person"
            if sender_name:
                header += f" ({sender_name})"
            parts.append(f"{header}\n\n{mem}")
        elif sender_name:
            parts.append(
                f"# Who you're talking to\n\n"
                f"Their name: {sender_name}. You haven't built a memory for them yet."
            )
    return "\n\n---\n\n".join(parts)
