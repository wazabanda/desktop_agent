import json
import subprocess
import sys
import threading
from collections import deque

import numpy as np
from PyQt6.QtCore import QByteArray, QEasingCurve, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMenu, QPushButton,
)

from desktop_agent import desktop
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

# Lucide "mic" icon (ISC license)
MIC_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white"
 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>"""


class MicButton(QPushButton):
    said = pyqtSignal(str)  # transcript, progress updates and replies, for the bubble

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
        self.buffer = None
        self.model = None
        threading.Thread(target=self._load_model, daemon=True).start()

    def _load_model(self):
        from faster_whisper import WhisperModel

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
        p.setBrush(QColor("#d33" if self.buffer is not None else "#333"))
        pill = self._pill()
        p.drawRoundedRect(pill, SIZE / 2, SIZE / 2)
        if self.buffer is None:
            self.mic.render(p, QRectF((WIDE - SIZE) / 2 + 14, 14, SIZE - 28, SIZE - 28))
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
        self.pcm.extend(chunk)
        if chunk:
            samples = np.frombuffer(chunk, np.int16).astype(np.float32) / 32768
            self.levels.append(min(1.0, float(np.sqrt(np.mean(samples**2))) * WAVE_GAIN))
            self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("New conversation", lambda: (forget(), self.said.emit("[new conversation]")))
        menu.addAction("Settings…", lambda: Settings().exec())
        menu.addAction("Quit", QApplication.quit)
        menu.exec(event.globalPos())

    def place(self):
        geo = self.screen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 20)

    def toggle(self):
        if self.buffer is None:
            self.buffer = self.source.start()  # pull-mode QIODevice
            self.pcm = bytearray()
            self.levels.extend([0.0] * BARS)
            self.buffer.readyRead.connect(self._on_audio)
            self._animate_to(WIDE)
            return
        self.pcm.extend(self.buffer.readAll().data())
        self.source.stop()
        self.buffer = None
        self._animate_to(SIZE)
        audio = np.frombuffer(bytes(self.pcm), np.int16).astype(np.float32) / 32768
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio):
        if self.model is None:
            self.said.emit("[model still loading, try again]")
            return
        segments, _ = self.model.transcribe(audio, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments)
        if not text:
            return
        self.said.emit(f"› {text}")
        try:
            self.said.emit(ask(text, self.said.emit))
        except Exception as e:  # ollama down, model missing, etc.
            self.said.emit(f"[agent error: {e}]")


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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.provider.currentTextChanged.connect(self._suggest)
        self.provider.setCurrentText(self.s["provider"])
        self._suggest(self.s["provider"])
        self.model.setCurrentText(self.s["model"])
        self.setMinimumWidth(380)

    def _suggest(self, provider):
        self.model.clear()
        self.model.addItems(PROVIDERS[provider])

    def accept(self):
        self.s["provider"] = self.provider.currentText()
        self.s["model"] = self.model.currentText().strip() or PROVIDERS[self.s["provider"]][0]
        self.s["keys"] = {p: e.text().strip() for p, e in self.keys.items() if e.text().strip()}
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
