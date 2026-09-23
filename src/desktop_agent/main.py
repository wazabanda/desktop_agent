import sys
import threading
from collections import deque

import numpy as np
from PyQt6.QtCore import QByteArray, QEasingCurve, QRectF, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QPushButton

SAMPLE_RATE = 16000  # what whisper expects
MODEL_SIZE = "base.en"
SIZE = 56  # idle circle diameter / capsule height
WIDE = 220  # capsule width while recording
BAR_W, GAP = 4, 2
BARS = (WIDE - 32) // (BAR_W + GAP)
WAVE_GAIN = 8  # calibration knob: raise if bars barely move with your mic, lower if they max out

# Lucide "mic" icon (ISC license)
MIC_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white"
 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>"""


class MicButton(QPushButton):
    transcribed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
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
        self.transcribed.connect(self.on_text)

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
            self.transcribed.emit("[model still loading, try again]")
            return
        segments, _ = self.model.transcribe(audio, vad_filter=True)
        self.transcribed.emit(" ".join(s.text.strip() for s in segments))

    def on_text(self, text):
        print(text, flush=True)


def main() -> None:
    app = QApplication(sys.argv)
    #Wayland ignores move(); on niri a window-rule on this app-id floats it bottom-center.
    app.setDesktopFileName("desktop-agent")
    button = MicButton()
    button.show()
    button.place()
    print("UI initialized", flush=True)
    sys.exit(app.exec())
