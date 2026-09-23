import os
import sys

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

MODEL = os.environ.get("AGENT_MODEL", "gemma4:e4b")  # any tool-capable model from `ollama list`
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

agent = Agent(
    OpenAIChatModel(MODEL, provider=OllamaProvider(base_url=OLLAMA_URL)),
    instructions="You are a desktop assistant. Be brief.",
)

# tools go here:
# @agent.tool_plain
# def open_app(name: str) -> str: ...


def ask(prompt: str) -> str:
    print(f"asking agent {prompt}")
    return agent.run_sync(prompt).output


if __name__ == "__main__":
    print(ask(" ".join(sys.argv[1:]) or "Say hi."))
