# desktop-agent

A voice-driven desktop assistant for the [niri](https://github.com/YaLTeR/niri) Wayland compositor, in the spirit of Copilot. You talk to it and it does things on your PC:

- **Answer questions about the system**: hardware, disk usage, running processes, network, installed packages
- **Operate the system**: open and focus apps, manage windows and workspaces through niri
- **Search**: files on disk, the web, app launchers
- **Run safe shell commands**: read-only and non-destructive commands only. Anything that deletes, overwrites, formats or escalates privileges (`rm`, `dd`, `mkfs`, `sudo`, …) is refused.

Everything runs locally. Speech-to-text uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and the agent is a [pydantic-ai](https://ai.pydantic.dev/) agent backed by a local [Ollama](https://ollama.com/) model.

## Status

Early work in progress.

| Piece | State |
|---|---|
| Floating mic button (PyQt6) with live waveform | ✅ done |
| Local speech-to-text (faster-whisper `base.en`, CPU int8) | ✅ done |
| Agent loop over Ollama | ✅ done, `print_to_ui` tool only |
| niri window rule setup | ✅ done |
| Tools: open apps, niri control, system info, search, safe bash | 🚧 planned |
| Chat bubble above the mic for transcript, progress and replies | ✅ done |

## Requirements

- Linux running niri
- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) with a model that supports tool calling
- A microphone

## Setup

```sh
# 1. install deps
uv sync

# 2. pull a tool-capable model (default is gemma4:e4b)
ollama pull gemma4:e4b

# 3. add the niri window rule so the button floats at bottom center
./scripts/niri-setup.sh
```

`niri-setup.sh` backs up `~/.config/niri/config.kdl`, adds `window-rule`s for app-id `desktop-agent` (mic at bottom center, bubble just above it), and restores the backup if `niri validate` fails. It is safe to re-run.

## Usage

```sh
uv run desktop-agent
```

Click the mic, speak, then click again to stop. The transcript, any progress updates the agent sends with its `print_to_ui` tool, and the final reply show up in a chat bubble above the mic (also echoed to stdout). The bubble keeps the last few messages and hides after 15s of quiet. It follows the mic if you drag it somewhere else (tracked through `niri msg`). The Whisper model loads in the background at startup, so the first click might report that it is still loading.

To try the agent without voice:

```sh
uv run python -m desktop_agent.agent "what's using the most memory?"
```

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | `gemma4:e4b` | Ollama model name (any tool-capable model from `ollama list`) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint |

## Layout

```
src/desktop_agent/
  main.py    # Qt mic button, audio capture, whisper transcription
  agent.py   # pydantic-ai agent + tools
scripts/
  niri-setup.sh  # installs the niri window rule
```

## Safety

The agent acts on your real machine, so shell access is limited by design. The planned bash tool checks every command against an allow/deny list in code before it runs. Destructive operations, privilege escalation and writes outside expected locations get blocked there, so the model can't talk its way around the check. Keep this in mind when you add tools: a new tool should expose the narrowest capability that does the job.
