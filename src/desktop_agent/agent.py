import configparser
import difflib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.moonshotai import MoonshotAIProvider
from pydantic_ai.providers.ollama import OllamaProvider

MODEL = os.environ.get("AGENT_MODEL", "gemma4:e4b")  # any tool-capable model from `ollama list`
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"), "desktop-agent", "settings.json")
# provider -> suggested models (first is the default); the settings dialog also accepts any typed name
PROVIDERS = {
    "ollama": [MODEL],
    "kimi": ["kimi-k3", "kimi-k2.6"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
}


def load_settings() -> dict:
    s = {"provider": "ollama", "model": MODEL, "keys": {}}
    try:
        s |= json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds api keys
    os.fchmod(fd, 0o600)  # also tighten a file that already existed
    with open(fd, "w") as f:
        json.dump(s, f, indent=2)


def build_model(s: dict) -> OpenAIChatModel:
    # no key saved: providers fall back to MOONSHOTAI_API_KEY / DEEPSEEK_API_KEY, else raise
    key = s["keys"].get(s["provider"]) or None
    if s["provider"] == "kimi":
        return OpenAIChatModel(s["model"], provider=MoonshotAIProvider(api_key=key))
    if s["provider"] == "deepseek":
        return OpenAIChatModel(s["model"], provider=DeepSeekProvider(api_key=key))
    return OpenAIChatModel(s["model"], provider=OllamaProvider(base_url=OLLAMA_URL))


@dataclass
class UI:
    say: Callable[[str], None] = print  # main.py passes a Qt signal emit (thread-safe)


agent = Agent(
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


App = tuple[str, Path]  # (display name, .desktop path)


def installed_apps() -> tuple[dict[str, App], dict[str, App]]:
    """({normalized Name: app}, {normalized GenericName/Keyword: app}) for launchable apps, user dirs first."""
    dirs = [os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))]
    dirs += os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    names, aliases = {}, {}
    for d in dirs:
        for path in sorted(Path(d, "applications").glob("**/*.desktop")):
            ini = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                ini.read(path, encoding="utf-8")
                entry = ini["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            hidden = entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true"
            if entry.get("Type") == "Application" and not hidden and "Name" in entry:
                app = (entry["Name"], path)
                names.setdefault(_norm(entry["Name"]), app)
                for alias in [entry.get("GenericName", ""), *entry.get("Keywords", "").split(";")]:
                    if _norm(alias):
                        aliases.setdefault(_norm(alias), app)  # "vscode", "browser"
    return names, aliases


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())  # whisper says "fire fox" / "VS code."


def find_app(query: str, names: dict[str, App], aliases: dict[str, App]) -> list[App]:
    """One app = launch it; several = ambiguous; none = no match."""
    q = _norm(query)
    if not q:
        return []
    if q in names:
        return [names[q]]
    # ponytail: fixed tier order tuned on real names: "code" must beat Text Editor's "code"
    # keyword, "browser" must beat three Avahi "... Browser" tools
    contains = list(dict.fromkeys(app for k, app in names.items() if q in k))
    if len(contains) == 1:
        return contains
    if q in aliases:
        return [aliases[q]]
    if contains:
        return contains
    return [names[k] for k in difflib.get_close_matches(q, names, n=1, cutoff=0.7)]


@agent.tool_plain
def open_app(name: str) -> str:
    """Launch an installed desktop application by its name, e.g. "firefox" or "visual studio code"."""
    names, aliases = installed_apps()
    matches = find_app(name, names, aliases)
    if len(matches) > 1:
        return f"Several apps match {name!r}: {', '.join(t for t, _ in matches)}. Call again with the exact name."
    if not matches:
        guesses = difflib.get_close_matches(_norm(name), names, n=5, cutoff=0.4)
        return f"No app named {name!r}. Closest: {', '.join(names[g][0] for g in guesses) or 'none'}"
    title, path = matches[0]
    # own session so the app outlives us; gio handles Exec field codes, Terminal=true, etc.
    subprocess.Popen(
        ["gio", "launch", str(path)],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return f"Launched {title}"


def ask(prompt: str, say: Callable[[str], None] = print) -> str:
    print(f"asking agent {prompt}")
    # settings re-read every ask, so dialog changes apply without a restart
    return agent.run_sync(prompt, model=build_model(load_settings()), deps=UI(say)).output


if __name__ == "__main__":
    print(ask(" ".join(sys.argv[1:]) or "Say hi."))
