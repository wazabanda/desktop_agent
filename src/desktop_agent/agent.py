import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

MODEL = os.environ.get("AGENT_MODEL", "gemma4:e4b")  # any tool-capable model from `ollama list`
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")


@dataclass
class UI:
    say: Callable[[str], None] = print  # main.py passes a Qt signal emit (thread-safe)


agent = Agent(
    OpenAIChatModel(MODEL, provider=OllamaProvider(base_url=OLLAMA_URL)),
    deps_type=UI,
    instructions=(
        "You are a desktop assistant. Be brief. On multi-step tasks, call print_to_ui "
        "with short progress updates. Your final answer is shown to the user automatically."
    ),
)


@agent.tool
def print_to_ui(ctx: RunContext[UI], message: str) -> str:
    """Show a short progress update to the user in the chat bubble."""
    ctx.deps.say(message)
    return "shown"


def ask(prompt: str, say: Callable[[str], None] = print) -> str:
    print(f"asking agent {prompt}")
    return agent.run_sync(prompt, deps=UI(say)).output


if __name__ == "__main__":
    print(ask(" ".join(sys.argv[1:]) or "Say hi."))
