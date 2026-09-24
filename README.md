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
| Agent loop over Ollama | ✅ done |
| Tool: `open_app` (launch installed apps by spoken name) | ✅ done |
| niri window rule setup | ✅ done |
| Mic and bubble follow you to whichever workspace/monitor you focus | ✅ done |
| Always-listening mode with a wake phrase (off by default, toggle in Settings) | ✅ done |
| Tools: windows (`list_windows`, `focus_window`), `open_url`, keyboard (`press_keys`, `type_text`) | ✅ done |
| Tools: app UI via accessibility tree (`list_elements`, `click_element`, `type_into`) | ✅ done |
| Conversation memory across requests (last 20, reset from the right-click menu) | ✅ done |
| Screenshot + numbered-box fallback for apps without an accessibility tree | 🚧 planned |
| Tools: system info, search, safe bash | 🚧 planned |
| Chat bubble above the mic for transcript, progress and replies | ✅ done |
| Replies read aloud with local Kokoro TTS (toggle in Settings) | ✅ done |

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
  agent.py   # pydantic-ai agent, settings, open_app
  desktop.py # desktop tools: niri windows, uinput keyboard, AT-SPI elements
  tts.py     # Kokoro text-to-speech, played with pw-play
tests/
  test_find_app.py  # app-name matching, run with `uv run python tests/test_find_app.py`
  test_keys.py      # key-name / character mapping
  test_history.py   # conversation memory and trimming (stub model, no LLM needed)
  test_wake.py      # wake-phrase queue matching
scripts/
  niri-setup.sh  # installs the niri window rule
```

## Always-listening mode

Turn on **Always listening** in Settings (right-click the mic) and set a wake phrase (default "hi bits"). A green dot on the mic shows it is listening.

- The mic stays open, and every 2 seconds whisper transcribes the last 2.5s locally. Quiet stretches are skipped, so it uses almost no CPU while the room is silent.
- The words go into a small rolling queue. When the queue contains your phrase, recording starts, the same as clicking. The match is on letters, so "Hi, Bitz" or "hibits" still count, and a phrase split across two windows is caught.
- **Nothing is sent to the LLM until the wake phrase is heard.** Only the recording after it goes to the agent.
- A wake-started recording stops by itself 1.5s after you stop talking. It also stops after 5s if you say nothing, or after 30s at most. Clicked recordings still wait for a second click.
- Wait for the mic to turn red before giving the command. Wake-up takes about half a second after the phrase.

Tuning knobs at the top of `main.py`: `SPEECH_RMS` (raise it in a noisy room), `WAKE_MATCH` (lower it if you get missed wakes, raise it if you get false ones) and `END_SILENCE_S`.

## Spoken replies

With **Read replies aloud** on (the default), the final reply is spoken by [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), running locally. Progress updates are shown but not spoken. Replies are synthesized one sentence at a time, so speech starts after the first sentence instead of the whole reply. The first run downloads the model (~340MB) to `~/.cache/desktop-agent/`. Audio plays through `pw-play` (PipeWire).

**Run speech models on the GPU** (Settings, on by default) puts both Whisper and Kokoro on an NVIDIA GPU. The CUDA libraries come from pip (`nvidia-*` wheels, ~2.5GB), so no system CUDA is needed. If CUDA won't start, both fall back to the CPU. On an RTX 4060, a 6.5s sentence takes 0.16s on the GPU versus ~2.4s on the CPU.

- Clicking the mic while it speaks cuts the reply off.
- The wake listener ignores the mic while the reply plays, so it doesn't hear itself.
- Change the voice or speed with `VOICE` / `SPEED` in `tts.py`. Try one with `uv run python -m desktop_agent.tts "hello there"`.

## Conversation memory

The agent remembers the last 20 requests of the session, so follow-ups work: "write a hello world in vim" followed by "run the program" types `python3 hello.py` into the same terminal. Old tool output such as element lists is trimmed to its first lines; what the agent did and in which window is kept. Right-click the mic and pick **New conversation** to start fresh. Memory is not saved when the app quits.

## Operating apps

The agent drives apps in three ways, most reliable first:

1. **Direct commands**: launch apps, open URLs, focus windows through niri.
2. **Keyboard**: key combos and typing through a virtual uinput keyboard. You need to be in the `uinput` group. Typing assumes the US layout and ASCII.
3. **Accessibility tree** (AT-SPI): `list_elements` returns a window's buttons, links and fields as a numbered list. `click_element` triggers the element's own action and `type_into` types into a field, with no mouse or coordinates involved.

On startup desktop-agent turns on the session's accessibility flag, as a screen reader would. GTK and Qt apps pick it up right away. Browsers only check it at startup, so restart Firefox or Chrome once after launching desktop-agent. VS Code and other Electron apps need `--force-renderer-accessibility`.

Large hosted models handle multi-step UI tasks well. Small local models (4B–9B) handle single tool calls but often misread longer tool output.

## Safety

The agent acts on your real machine, so shell access is limited by design. The planned bash tool checks every command against an allow/deny list in code before it runs. Destructive operations, privilege escalation and writes outside expected locations get blocked there, so the model can't talk its way around the check. Keep this in mind when you add tools: a new tool should expose the narrowest capability that does the job.

The keyboard tools are the exception: they can type anything into any window, including a terminal, so they effectively bypass any shell allow-list. The agent is told never to submit, send, buy or delete without being asked, but only the prompt enforces that. There is no hard check. Treat the model you pick accordingly.

## AI disclosure

This project was built with help from AI. [Claude Code](https://claude.com/claude-code) (Anthropic's Claude) was used to write and edit parts of the code and documentation. All changes were reviewed by the author.
