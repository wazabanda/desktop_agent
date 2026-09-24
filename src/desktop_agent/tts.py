"""Local text-to-speech: Kokoro (onnx) played through PipeWire's pw-play."""

import fcntl
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import numpy as np

URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# ponytail: fp32 model (310MB); the int8 one is smaller but ~3x slower on x86 CPUs
FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
CACHE = Path.home() / ".cache" / "desktop-agent"
VOICE, SPEED = "af_heart", 1.1  # main.py sets VOICE from settings
# English voices in voices-v1.0.bin: a/b = American/British, f/m = female/male
VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
RATE = 24000  # Kokoro's output sample rate

_kokoro = None
_player: subprocess.Popen | None = None


def load(gpu: bool = False) -> None:
    """Download the model on first run (~340MB), then load it. Slow: call from a thread."""
    global _kokoro
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    CACHE.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = CACHE / name
        if not path.exists():
            print(f"downloading {name} for text-to-speech…", flush=True)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(URL + name, tmp)
            tmp.rename(path)  # a cut download leaves .part, not a broken model
    providers = ["CPUExecutionProvider"]
    if gpu:
        ort.preload_dlls()  # CUDA/cuDNN from the nvidia-* pip wheels, they aren't on the library path
        providers.insert(0, "CUDAExecutionProvider")  # onnxruntime falls back to CPU if CUDA won't start
    ort.set_default_logger_severity(3)  # errors only: Kokoro trips harmless ScatterND/Memcpy warnings
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    session = ort.InferenceSession(str(CACHE / FILES[0]), opts, providers=providers)
    _kokoro = Kokoro.from_session(session, str(CACHE / FILES[1]))
    print(f"tts on {session.get_providers()[0]}", flush=True)


def _clean(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks are for reading, not hearing
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^\s*(?:[-+]|\d+\.)\s+", "", text, flags=re.M)  # list bullets
    return re.sub(r"[*_#`>\[\]]", "", text).strip()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", _clean(text)) if s.strip()]


def speak(text: str) -> None:
    """Say text and block until done (or stop() is called). No-op until load() finished."""
    global _player
    parts = sentences(text)
    if _kokoro is None or not parts:
        return
    # one player for the whole reply; each sentence is piped in as soon as it's synthesized,
    # so the first one plays after ~1s instead of waiting for the whole reply
    _player = p = subprocess.Popen(
        ["pw-play", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdin=subprocess.PIPE, bufsize=0,  # stderr left on the terminal: a silent failure here is invisible
    )
    try:
        # room for ~20s of audio, so synthesizing the next sentence never waits on playback
        fcntl.fcntl(p.stdin, fcntl.F_SETPIPE_SZ, 1 << 20)
    except OSError:
        pass  # capped by /proc/sys/fs/pipe-max-size: still works, just less lookahead
    try:
        for s in parts:
            if p.poll() is not None:  # stop() killed the player
                break
            lang = "en-gb" if VOICE.startswith("b") else "en-us"  # British voices need British pronunciation
            samples, _ = _kokoro.create(s, voice=VOICE, speed=SPEED, lang=lang)
            p.stdin.write((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        p.stdin.close()
        p.wait()
    except (BrokenPipeError, ValueError):  # stop() killed it mid-write
        pass
    _player = None


def stop() -> None:
    """Cut off speech, e.g. when the user starts talking."""
    if (p := _player) is not None:  # local copy: speak() may clear _player between check and kill
        p.kill()


if __name__ == "__main__":
    from desktop_agent.agent import load_settings

    s = load_settings()
    VOICE = s["voice"]
    load(s["gpu"])
    speak(" ".join(sys.argv[1:]) or "Hi, I'm your desktop assistant.")
