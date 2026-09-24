import difflib
import json
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QByteArray, QEasingCurve, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton,
)

from desktop_agent import desktop, tts
from desktop_agent.agent import PROVIDERS, ask, forget, load_settings, save_settings

SAMPLE_RATE = 16000  # what whisper expects
MODEL_SIZE = "base.en"
SIZE = 56  # idle circle diameter / capsule height
WIDE = 220  # capsule width while recording
BAR_W, GAP = 4, 2
BARS = (WIDE - 32) // (BAR_W + GAP)
WAVE_GAIN = 8  # calibration knob: raise if bars barely move with your mic, lower if they max out
BUBBLE_W, BUBBLE_H = 360, 160
BUBBLE_LINES = 6  # messages kept in the bubble
BUBBLE_HIDE_MS = 15000
BUBBLE_GAP = 8  # px between bubble and mic
FOLLOW_MS = 250  # how often the visible bubble re-checks the mic position
MIC_TITLE, BUBBLE_TITLE = "desktop-agent-mic", "desktop-agent-bubble"
SETTINGS_TITLE = "desktop-agent-settings"
# always-listening mode
WAKE_WINDOW_S, WAKE_HOP_S = 2.5, 2.0  # whisper looks at 2.5s of audio every 2s (0.5s overlap)
WAKE_MATCH = 0.8  # 0..1 letter similarity to the phrase; lower if it misses you, raise on false wakes
SPEECH_RMS = 0.01  # calibration knob: mic level counted as speech; raise in a noisy room
END_SILENCE_S, NO_SPEECH_S, MAX_RECORD_S = 1.5, 5.0, 30.0  # auto-stop for wake-started recordings
FOLLOWUP_S = 10.0  # after a reply, listen this long for a follow-up before needing the wake phrase again
GOODBYES = {"bye", "goodbye", "byebye", "thanksbye", "thankyoubye", "thatsall", "nevermind", "stop"}  # whole utterance
BYTES_PER_S = SAMPLE_RATE * 2  # int16 mono


def _float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768


def _rms(pcm: bytes) -> float:
    return float(np.sqrt(np.mean(_float(pcm) ** 2)))


def _word(w: str) -> str:
    return "".join(c for c in w.lower() if c.isalnum())


class WakeQueue:
    """Rolling queue of the last words heard; fires once when it contains the wake phrase."""

    def __init__(self, phrase: str):
        self.phrase = "".join(_word(w) for w in phrase.split())
        n = len(phrase.split())
        self.sizes = range(max(1, n - 1), n + 2)  # whisper may merge or split words: "hibits", "hi bit s"
        self.words: deque[str] = deque(maxlen=n + 2)

    def heard(self, text: str) -> bool:
        self.words.extend(w for w in map(_word, text.split()) if w)
        words = list(self.words)
        spans = ("".join(words[i:i + k]) for k in self.sizes for i in range(len(words) - k + 1))
        # compare letters, not words: whisper spells a made-up name loosely ("Hi, Bitz!", "hibbits")
        if self.phrase and any(difflib.SequenceMatcher(None, s, self.phrase).ratio() >= WAKE_MATCH for s in spans):
            self.words.clear()  # don't fire again on the same words
            return True
        return False

# Lucide "mic" icon (ISC license)
MIC_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white"
 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>"""


class MicButton(QPushButton):
    said = pyqtSignal(str)  # transcript, progress updates and replies, for the bubble
    woke = pyqtSignal()  # wake phrase heard (from the listener thread)
    replied = pyqtSignal()  # agent answered (from the transcribe thread)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(MIC_TITLE)  # the bubble finds us by this in `niri msg windows`
        # ponytail: window stays WIDE so the compositor keeps it centered; the shape animates inside it
        self.setFixedSize(WIDE, SIZE)
        self.mic = QSvgRenderer(QByteArray(MIC_SVG))
        self.levels = deque([0.0] * BARS, maxlen=BARS)
        self.pill_w = SIZE
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(200)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(self._set_pill_w)
        self._set_pill_w(SIZE)
        self.clicked.connect(self.toggle)

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.source = QAudioSource(QMediaDevices.defaultAudioInput(), fmt)
        self.buffer = None  # audio device while the mic is open (recording or listening)
        self.recording = False
        self.auto_stop = False  # wake-started recordings end on silence
        self.listening = False
        self.wake_pcm = bytearray()
        self.wake_busy = False
        self.model = None
        self.model_lock = threading.Lock()  # listener and command transcription share one model
        self.speaking = False  # replies being read out: the wake listener ignores the mic meanwhile
        self.tts_loading = False
        self.gpu = None  # device the models are loaded on; apply_settings (re)loads them
        self.woke.connect(lambda: self.recording or self._start(auto_stop=True))
        self.replied.connect(lambda: self.recording or self._start(auto_stop=True, no_speech=FOLLOWUP_S))
        self.apply_settings()

    def _load_model(self, gpu: bool):
        from faster_whisper import WhisperModel

        if gpu:
            try:
                _preload_cublas()
                model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
                list(model.transcribe(np.zeros(SAMPLE_RATE, np.float32))[0])  # CUDA errors show up on first use
                self.model = model
                print("whisper on cuda", flush=True)
                return
            except Exception as e:  # no driver, libs missing, out of VRAM...
                print(f"whisper can't use the GPU, using CPU: {e}", flush=True)
        self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    def _pill(self):
        return QRectF((WIDE - self.pill_w) / 2, 0, self.pill_w, SIZE)

    def _set_pill_w(self, w):
        self.pill_w = w
        self.update()

    def hitButton(self, pos):
        # ponytail: only the shape reacts, but the transparent sides still block clicks to windows below;
        # setMask would pass them through but leaves ghost frames on Wayland
        return self._pill().contains(pos.toPointF())

    def _animate_to(self, w):
        self.anim.stop()
        self.anim.setStartValue(self.pill_w)
        self.anim.setEndValue(w)
        self.anim.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)  # wipe last frame
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#d33" if self.recording else "#333"))
        pill = self._pill()
        p.drawRoundedRect(pill, SIZE / 2, SIZE / 2)
        if not self.recording:
            self.mic.render(p, QRectF((WIDE - SIZE) / 2 + 14, 14, SIZE - 28, SIZE - 28))
            if self.listening:  # privacy cue: the mic is open, waiting for the wake phrase
                p.setBrush(QColor("#3c3"))
                p.drawEllipse(QRectF(WIDE / 2 + 12, 8, 8, 8))
            return
        # waveform: as many recent levels as fit the current width, newest on the right
        p.setBrush(QColor("white"))
        n = max(0, int((self.pill_w - 32 + GAP) // (BAR_W + GAP)))
        x = pill.center().x() - (n * (BAR_W + GAP) - GAP) / 2
        for level in list(self.levels)[BARS - n :]:
            h = 4 + level * (SIZE - 24)
            p.drawRoundedRect(QRectF(x, (SIZE - h) / 2, BAR_W, h), 2, 2)
            x += BAR_W + GAP

    def _on_audio(self):
        chunk = self.buffer.readAll().data()
        if not chunk:
            return
        rms = _rms(chunk)
        if self.recording:
            self.pcm.extend(chunk)
            self.levels.append(min(1.0, rms * WAVE_GAIN))
            self.update()
            if self.auto_stop:
                self._check_silence(len(chunk), rms)
        elif self.speaking:
            self.wake_pcm.clear()  # don't scan our own voice for the wake phrase
        elif self.listening:
            self.wake_pcm.extend(chunk)
            window, hop = int(WAKE_WINDOW_S * BYTES_PER_S), int(WAKE_HOP_S * BYTES_PER_S)
            if len(self.wake_pcm) >= window:
                audio = bytes(self.wake_pcm[:window])
                del self.wake_pcm[:hop]
                # skip quiet windows (most of the day) and windows arriving while whisper is still busy
                if not self.wake_busy and self.model is not None and _rms(audio) >= SPEECH_RMS:
                    self.wake_busy = True
                    threading.Thread(target=self._listen, args=(audio,), daemon=True).start()

    def _listen(self, audio: bytes):
        try:
            with self.model_lock:
                segments, _ = self.model.transcribe(
                    _float(audio), beam_size=1, vad_filter=True, condition_on_previous_text=False,
                    hotwords=self.phrase,  # nudges whisper towards spelling the phrase the way you set it
                )
                text = " ".join(s.text.strip() for s in segments)
            if text and self.wake.heard(text):
                self.woke.emit()
        finally:
            self.wake_busy = False

    def _check_silence(self, nbytes: int, rms: float):
        self.rec_bytes += nbytes
        if rms >= SPEECH_RMS:
            self.spoke, self.quiet = True, 0
        else:
            self.quiet += nbytes
        if ((self.spoke and self.quiet >= END_SILENCE_S * BYTES_PER_S)
                or (not self.spoke and self.rec_bytes >= self.no_speech * BYTES_PER_S)
                or self.rec_bytes >= MAX_RECORD_S * BYTES_PER_S):
            self._stop()

    def apply_settings(self):
        s = load_settings()
        self.phrase = s["wake_phrase"]
        self.wake = WakeQueue(self.phrase)
        self.listening = bool(s["wake"])
        self.speak = bool(s["speak"])
        tts.VOICE = s["voice"]
        if bool(s["gpu"]) != self.gpu:  # first run or toggled: (re)load both models on that device
            self.gpu = bool(s["gpu"])
            threading.Thread(target=self._load_model, args=(self.gpu,), daemon=True).start()
            tts._kokoro, self.tts_loading = None, False
        if self.speak and tts._kokoro is None and not self.tts_loading:
            self.tts_loading = True
            threading.Thread(target=tts.load, args=(self.gpu,), daemon=True).start()
        self._mic(self.listening or self.recording)
        self.update()

    def _mic(self, on: bool):
        """Open/close the audio device; it stays open while recording or listening."""
        if on and self.buffer is None:
            self.buffer = self.source.start()  # pull-mode QIODevice
            self.buffer.readyRead.connect(self._on_audio)
        elif not on and self.buffer is not None:
            self.source.stop()
            self.buffer = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("New conversation", lambda: (forget(), self.said.emit("[new conversation]")))
        menu.addAction("Settings…", lambda: Settings().exec() and self.apply_settings())
        menu.addAction("Quit", QApplication.quit)
        menu.exec(event.globalPos())

    def place(self):
        geo = self.screen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 20)

    def toggle(self):
        self._stop() if self.recording else self._start(auto_stop=False)

    def _start(self, auto_stop: bool, no_speech: float = NO_SPEECH_S):
        tts.stop()  # clicking the mic interrupts the reply being read out
        self.recording, self.auto_stop, self.no_speech = True, auto_stop, no_speech
        self.pcm = bytearray()
        self.rec_bytes = self.quiet = 0
        self.spoke = False
        self.levels.extend([0.0] * BARS)
        self._mic(True)
        self._animate_to(WIDE)

    def _stop(self):
        self.pcm.extend(self.buffer.readAll().data())
        self.recording = False
        self.wake_pcm.clear()  # don't scan the command itself for the wake phrase
        self._mic(self.listening)
        self._animate_to(SIZE)
        threading.Thread(target=self._transcribe, args=(_float(bytes(self.pcm)),), daemon=True).start()

    def _transcribe(self, audio):
        if self.model is None:
            self.said.emit("[model still loading, try again]")
            return
        with self.model_lock:
            segments, _ = self.model.transcribe(audio, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments)
        if not text:
            return
        self.said.emit(f"› {text}")
        if _goodbye(text):  # end the conversation turn: no agent call, no follow-up
            self.said.emit("bye 👋")
            self._say("Bye!")
            return
        try:
            reply = ask(text, self.said.emit)
        except Exception as e:  # ollama down, model missing, etc.
            self.said.emit(f"[agent error: {e}]")
            return
        self.said.emit(reply)
        self._say(reply)  # blocks, so the follow-up below doesn't record our own voice
        if self.listening and not _playing():
            self.replied.emit()

    def _say(self, text: str):
        if not self.speak:
            return
        self.speaking = True
        try:
            tts.speak(text)
        except Exception as e:  # pw-play missing, model failed to load, etc.
            print(f"tts failed: {e}", flush=True)
        finally:
            self.speaking = False


def _preload_cublas():
    """ctranslate2 (whisper) dlopens libcublas.so.12; the nvidia-cublas-cu12 wheel isn't on the library path."""
    import ctypes
    import nvidia.cublas

    lib = Path(nvidia.cublas.__path__[0], "lib")
    for name in ("libcublasLt.so.12", "libcublas.so.12"):
        ctypes.CDLL(str(lib / name), mode=ctypes.RTLD_GLOBAL)


def _goodbye(text: str) -> bool:
    # ponytail: exact match on the whole utterance, so "stop the music" still reaches the agent
    return "".join(map(_word, text.split())) in GOODBYES


def _playing() -> bool:
    """Something is playing: the reply started media, and it would drown out a follow-up anyway."""
    try:
        return "Playing" in desktop._players().values()
    except Exception:  # ponytail: no D-Bus/MPRIS -> assume silent
        return False


class Settings(QDialog):
    """Provider, model and per-provider API keys, saved to agent.SETTINGS."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(SETTINGS_TITLE)  # niri-setup.sh matches this title
        self.s = load_settings()
        form = QFormLayout(self)
        self.provider = QComboBox()
        self.provider.addItems(PROVIDERS)
        self.model = QComboBox()
        self.model.setEditable(True)  # any model name, not just the suggestions
        self.keys = {}
        form.addRow("Provider", self.provider)
        form.addRow("Model", self.model)
        for p in PROVIDERS:
            if p == "ollama":
                continue  # local, no key
            self.keys[p] = QLineEdit(self.s["keys"].get(p, ""))
            self.keys[p].setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(f"{p} API key", self.keys[p])
        self.wake = QCheckBox("Always listening (say the wake phrase instead of clicking)")
        self.wake.setChecked(self.s["wake"])
        self.phrase = QLineEdit(self.s["wake_phrase"])
        self.phrase.setPlaceholderText("hi bits")
        form.addRow(self.wake)
        form.addRow("Wake phrase", self.phrase)
        self.speak = QCheckBox("Read replies aloud (downloads a ~340MB voice model once)")
        self.speak.setChecked(self.s["speak"])
        form.addRow(self.speak)
        self.voice = QComboBox()
        self.voice.addItems(tts.VOICES)
        self.voice.setCurrentText(self.s["voice"])
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        row = QHBoxLayout()
        row.addWidget(self.voice, 1)
        row.addWidget(preview)
        form.addRow("Voice", row)
        self.gpu = QCheckBox("Run speech models on the GPU (NVIDIA/CUDA, falls back to CPU)")
        self.gpu.setChecked(self.s["gpu"])
        form.addRow(self.gpu)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.provider.currentTextChanged.connect(self._suggest)
        self.provider.setCurrentText(self.s["provider"])
        self._suggest(self.s["provider"])
        self.model.setCurrentText(self.s["model"])
        self.setMinimumWidth(380)

    def _preview(self):
        voice = self.voice.currentText()

        def say():
            saved, tts.VOICE = tts.VOICE, voice  # only for this sample; Save is what keeps it
            try:
                tts.speak(f"Hi, I'm {voice.split('_')[1].title()}. This is how I'll read your replies.")
            finally:
                tts.VOICE = saved

        tts.stop()
        if tts._kokoro is None:
            print("voice model not loaded yet (is Read replies aloud on?)", flush=True)
        threading.Thread(target=say, daemon=True).start()

    def _suggest(self, provider):
        self.model.clear()
        self.model.addItems(PROVIDERS[provider])

    def accept(self):
        self.s["provider"] = self.provider.currentText()
        self.s["model"] = self.model.currentText().strip() or PROVIDERS[self.s["provider"]][0]
        self.s["keys"] = {p: e.text().strip() for p, e in self.keys.items() if e.text().strip()}
        self.s["wake"] = self.wake.isChecked()
        self.s["wake_phrase"] = self.phrase.text().strip() or "hi bits"
        self.s["speak"] = self.speak.isChecked()
        self.s["voice"] = self.voice.currentText()
        self.s["gpu"] = self.gpu.isChecked()
        save_settings(self.s)
        super().accept()


class Bubble(QLabel):
    """Chat bubble above the mic: rolling log of the last few messages, auto-hides when idle."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(BUBBLE_TITLE)  # niri-setup.sh matches this title
        # ponytail: fixed size because niri anchors floating windows top-left, a growing window
        # would slide over the mic; long text is clipped at the top, swap for QTextEdit if that bites
        self.setFixedSize(BUBBLE_W, BUBBLE_H)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self.setContentsMargins(16, 12, 16, 12)
        self.setStyleSheet("color: white; font-size: 13px;")
        self.lines = deque(maxlen=BUBBLE_LINES)
        self.hider = QTimer(self)
        self.hider.setSingleShot(True)
        self.hider.setInterval(BUBBLE_HIDE_MS)
        self.hider.timeout.connect(self.hide)
        # ponytail: polls niri every FOLLOW_MS while visible; niri's event-stream if polling ever shows up in top
        self.follower = QTimer(self)
        self.follower.setInterval(FOLLOW_MS)
        self.follower.timeout.connect(self._follow_mic)
        self.hider.timeout.connect(self.follower.stop)

    def say(self, text):
        print(text, flush=True)
        self.lines.append(text)
        self.setText("\n\n".join(self.lines))
        self.show()
        self.hider.start()
        self.follower.start()  # first tick also catches the window once niri has mapped it

    def _follow_mic(self):
        # Wayland hides window positions from clients, so ask niri over its IPC
        try:
            out = subprocess.run(["niri", "msg", "--json", "windows"], capture_output=True, timeout=1).stdout
            ours = {w["title"]: w for w in json.loads(out) if w["app_id"] == "desktop-agent"}
        except (OSError, subprocess.SubprocessError, ValueError):
            return  # not on niri: stay where the window rule put it
        mic, bubble = ours.get(MIC_TITLE), ours.get(BUBBLE_TITLE)
        if not mic or not bubble or not bubble["is_floating"]:
            return
        mic_pos = mic["layout"]["tile_pos_in_workspace_view"]
        if mic_pos is None:  # mic tiled or on another workspace
            return
        (mx, my), (mw, _) = mic_pos, mic["layout"]["tile_size"]
        (bx, by), (bw, bh) = bubble["layout"]["tile_pos_in_workspace_view"], bubble["layout"]["tile_size"]
        # relative move: absolute move coords exclude bars (working area), window-list coords don't
        dx, dy = round(mx + (mw - bw) / 2 - bx), round(my - bh - BUBBLE_GAP - by)
        if dx or dy:
            subprocess.run(
                ["niri", "msg", "action", "move-floating-window", "--id", str(bubble["id"]), f"-x={dx:+}", f"-y={dy:+}"],
                capture_output=True, timeout=1,
            )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)  # wipe last frame
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(40, 40, 40, 235))
        p.drawRoundedRect(QRectF(self.rect()), 16, 16)
        p.end()
        super().paintEvent(event)


def follow_workspaces(app_id: str = desktop.OURS) -> None:
    """Pull our windows onto whichever workspace gets focused: niri has no sticky windows."""
    # ponytail: one long-lived event stream; if niri restarts the loop ends and windows stop following
    stream = subprocess.Popen(["niri", "msg", "--json", "event-stream"], stdout=subprocess.PIPE, text=True)
    for line in stream.stdout or []:
        ev = json.loads(line).get("WorkspaceActivated")
        if not ev or not ev["focused"]:
            continue
        try:
            spaces = {w["id"]: w for w in json.loads(desktop._niri("--json", "workspaces"))}
            target = spaces[ev["id"]]
            for win in json.loads(desktop._niri("--json", "windows")):
                if win["app_id"] != app_id or win["workspace_id"] == target["id"]:
                    continue
                if spaces[win["workspace_id"]]["output"] != target["output"]:
                    # lands on that monitor's active workspace, which is the one just focused
                    desktop._niri("action", "move-window-to-monitor", "--id", str(win["id"]), target["output"])
                else:
                    desktop._niri("action", "move-window-to-workspace", "--window-id", str(win["id"]),
                                  "--focus", "false", str(target["idx"]))
        except (subprocess.SubprocessError, KeyError, ValueError) as e:
            print(f"workspace follow failed: {e}", flush=True)  # skip this switch, keep listening


def main() -> None:
    app = QApplication(sys.argv)
    #Wayland ignores move(); on niri a window-rule on this app-id floats it bottom-center.
    app.setDesktopFileName("desktop-agent")
    desktop.enable_accessibility()  # apps launched from now on expose their UI to list_elements
    button = MicButton()
    bubble = Bubble()
    button.said.connect(bubble.say)
    button.show()
    button.place()
    threading.Thread(target=follow_workspaces, daemon=True).start()
    print("UI initialized", flush=True)
    sys.exit(app.exec())
