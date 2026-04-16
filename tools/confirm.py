from contextvars import ContextVar
from typing import Awaitable, Callable


ConfirmCallback = Callable[[str], Awaitable[bool]]

confirm_callback: ContextVar[ConfirmCallback | None] = ContextVar(
    "confirm_callback", default=None
)


async def terminal_confirm(summary: str) -> bool:
    print(f"\n>>> {summary}")
    answer = input(">>> Confirm? [y/N]: ").strip().lower()
    return answer == "y"


async def deny_confirm(summary: str) -> bool:
    return False
