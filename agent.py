import anyio
import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    query,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

from agent_core import build_options
from tools.confirm import confirm_callback, terminal_confirm


async def run_agent(prompt: str):
    confirm_callback.set(terminal_confirm)
    options = build_options()

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
                elif isinstance(block, ToolUseBlock):
                    print(f"\n[tool: {block.name}({block.input})]", flush=True)
        elif isinstance(message, ResultMessage):
            print(f"\n\n--- done (turns: {message.num_turns}, cost: ${message.total_cost_usd or 0:.4f}) ---")


def main():
    parser = argparse.ArgumentParser(description="Claude trading research agent (CLI)")
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    args = parser.parse_args()

    prompt = args.prompt or sys.stdin.read().strip()
    if not prompt:
        print("Usage: python agent.py \"research NVDA and recommend an action\"", file=sys.stderr)
        sys.exit(1)

    anyio.run(run_agent, prompt)


if __name__ == "__main__":
    main()
