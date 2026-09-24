"""Auto-stop on silence/static, using the real Silero detector. Run: uv run python tests/test_silence.py"""

from types import SimpleNamespace

import numpy as np

from desktop_agent.main import BYTES_PER_S, SAMPLE_RATE, MicButton

rng = np.random.default_rng(0)


def pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def static(s):
    return pcm(rng.normal(0, 0.05, int(s * SAMPLE_RATE)))  # loud enough to pass the old loudness check


def voice(s):
    # vowel-like: pitched harmonics with a syllable-rate envelope, what Silero scores as speech
    t = np.arange(int(s * SAMPLE_RATE)) / SAMPLE_RATE
    f0 = 140 + 20 * np.sin(2 * np.pi * 0.5 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SAMPLE_RATE
    wave = sum(np.sin(k * phase) / k for k in range(1, 12))
    return pcm(0.2 * wave * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)))


def recorder(auto_stop, no_speech=5.0):
    r = SimpleNamespace(auto_stop=auto_stop, no_speech=no_speech, vad_pcm=bytearray(),
                        rec_bytes=0, quiet=0, spoke=False, stopped_at=None)
    r._stop = lambda: r.stopped_at is None and setattr(r, "stopped_at", r.rec_bytes / BYTES_PER_S)
    return r


def feed(r, audio, chunk=3200):  # 0.1s chunks, like QAudioSource delivers
    for i in range(0, len(audio), chunk):
        MicButton._check_silence(r, audio[i:i + chunk])


if __name__ == "__main__":
    r = recorder(auto_stop=False)  # clicked, only static
    feed(r, static(12))
    assert r.stopped_at is not None and 9.5 < r.stopped_at < 11, r.stopped_at

    r = recorder(auto_stop=False)  # clicked: speech resets the 10s timer
    feed(r, voice(2) + static(8) + voice(2) + static(8))
    assert r.spoke and r.stopped_at is None, r.stopped_at

    r = recorder(auto_stop=True)  # wake-started: ends shortly after speech
    feed(r, voice(2) + static(3))
    assert r.stopped_at is not None and r.stopped_at < 4.5, r.stopped_at
    print("ok")
