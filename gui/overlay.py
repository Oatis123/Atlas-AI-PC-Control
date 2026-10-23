"""Qt (PySide6) full-screen overlay for the voice assistant, in the oatis-style
(moss / bookish): flat card, soft glowing screen edge in the phase colour, stepped card animations.

Real per-pixel alpha, so the desktop stays visible under the edge and the card.

Phases:
  idle       - faint static edge glow (muted), no card
  listening  - card with the live dictated text (moss edge)
  working    - card: dictated command (muted) + "работает" (ochre edge)
  answering  - card: agent's response (moss edge)

Talks to main.py through the same thread-safe queue protocol:
  {'type': 'phase',                'name': 'idle'|'listening'|'working'|'answering', 'reset': bool}
  {'type': 'user_speech',          'text': <partial or final transcript>}
  {'type': 'agent_response_chunk', 'text': <answer text>}
  {'type': 'status', ...}          - legacy messages, still understood
"""

import sys
import html
import queue
from pathlib import Path

import math

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (QColor, QPainter, QPen, QFont, QFontDatabase,
                           QLinearGradient, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# palette (dark): bg / surface / line / dim / muted / text / prose / moss / ochre
SURFACE = QColor("#0F0F0C")
LINE = QColor("#20211C")
DIM = "#393A34"
MUTED = "#75746C"
TEXT = "#DFDDD3"
PROSE = "#CFCDC3"
MOSS = "#80856D"
OCHRE = "#95876E"

PHASES = ("idle", "listening", "working", "answering")
# edge colour per phase: moss = voice / answer, ochre = the agent is alive
EDGE_COLOR = {"idle": MUTED, "listening": MOSS, "working": OCHRE, "answering": MOSS}

EQ_FRAMES = ("▁▃▅▂▇▃▁▅", "▃▆▂▅▃▇▅▂", "▅▂▇▃▁▅▃▆")
SPIN_FRAMES = ("▖", "▘", "▝", "▗")

TICK_MS = 33     # edge glow pulses smoothly on this beat
CARD_STEP = 3    # card glyphs / caret are stepped: one change per 3 ticks (~100 ms)
CODE_FAMILY = "'JetBrains Mono', Consolas, monospace"

_fonts_loaded = False


def _load_fonts():
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    for ttf in FONTS_DIR.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(ttf))


def _font(families, px, italic=False):
    f = QFont()
    f.setFamilies(families)
    f.setPixelSize(px)
    f.setItalic(italic)
    return f


def _hex_rgb(h):
    c = QColor(h)
    return c.red(), c.green(), c.blue()


class _Check(QWidget):
    """9x9 pixel-grid check mark (the style's icon grid), drawn with 1px cells."""
    CELLS = ((0, 4), (1, 5), (2, 6), (3, 5), (4, 4), (5, 3), (6, 2), (7, 1), (8, 0))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(9, 9)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(MOSS))
        for x, y in self.CELLS:
            p.drawRect(x, y, 1, 1)


class _Card(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        _load_fonts()
        self.setAttribute(Qt.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 13, 18, 16)
        lay.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.brand = QLabel("ATLAS")
        self.brand.setFont(_font(["Silkscreen", "Martian Mono", "Consolas"], 13))
        self.brand.setStyleSheet(f"color: {TEXT}; background: transparent;")
        self.check = _Check()
        self.check.hide()
        self.state = QLabel("")
        self.state.setTextFormat(Qt.RichText)
        self.state.setFont(_font(["Martian Mono", "JetBrains Mono", "Consolas"], 11))
        self.state.setStyleSheet("background: transparent;")
        head.addWidget(self.brand)
        head.addStretch(1)
        head.addWidget(self.check)
        head.addWidget(self.state)
        lay.addLayout(head)
        lay.addSpacing(10)

        self.speech = QLabel("")
        self.speech.setTextFormat(Qt.RichText)
        self.speech.setWordWrap(True)
        self.speech.setFont(_font(["Literata", "Georgia"], 17, italic=True))
        self.speech.setStyleSheet("background: transparent;")
        lay.addWidget(self.speech)

        self.agent = QLabel("")
        self.agent.setTextFormat(Qt.PlainText)
        self.agent.setWordWrap(True)
        self.agent.setFont(_font(["Literata", "Georgia"], 18))
        self.agent.setStyleSheet(f"color: {PROSE}; background: transparent;")
        self.agent.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(self.agent)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), SURFACE)
        p.setPen(QPen(LINE, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))


class _OverlayWindow(QWidget):
    def __init__(self, gui_queue, stop_event_callback, stop_event):
        super().__init__()
        self.gui_queue = gui_queue
        self.stop_event_callback = stop_event_callback
        self.stop_event = stop_event

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(QApplication.primaryScreen().geometry())

        self._phase = "idle"
        self._speech_text = ""
        self._agent_text = ""
        self._frame = 0
        self._t = 0.0
        self._shown = {}  # last text pushed to each label, to skip no-op updates

        self.card = _Card(self)
        self.card.hide()

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(TICK_MS)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._drain)
        self._poll.start(40)

        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_stop)
        self._watch.start(250)

    # ------------------------------------------------------------------ helpers
    def _reposition_card(self):
        w = max(460, min(int(self.width() * 0.36), 720))
        self.card.setFixedWidth(w)
        lay = self.card.layout()
        lay.activate()
        # wrapped labels: sizeHint() ignores the width, heightForWidth does not
        self.card.setFixedHeight(lay.totalHeightForWidth(w))
        x = (self.width() - w) // 2
        y = self.height() - self.card.height() - max(56, int(self.height() * 0.07))
        self.card.move(x, max(0, y))

    # ------------------------------------------------------------------- render
    def _rgb(self):
        return _hex_rgb(EDGE_COLOR.get(self._phase, EDGE_COLOR["idle"]))

    def _pulse(self):
        return 0.5 + 0.5 * math.sin(self._t)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        r, g, b = self._rgb()
        gain = 0.42 if self._phase == "idle" else (0.72 + 0.28 * self._pulse())
        W, H = self.width(), self.height()
        depth = int(min(W, H) * 0.13)

        def col(a):
            return QColor(r, g, b, max(0, min(255, int(a * gain))))

        p.setPen(Qt.NoPen)

        stops = ((0.0, 132), (0.10, 70), (0.34, 18), (1.0, 0))

        gt = QLinearGradient(0, 0, 0, depth)
        gl = QLinearGradient(0, 0, depth, 0)
        gb = QLinearGradient(0, H, 0, H - depth)
        gr = QLinearGradient(W, 0, W - depth, 0)
        for pos, a in stops:
            c = col(a)
            gt.setColorAt(pos, c)
            gl.setColorAt(pos, c)
            gb.setColorAt(pos, c)
            gr.setColorAt(pos, c)
        p.fillRect(0, 0, W, depth, gt)
        p.fillRect(0, H - depth, W, depth, gb)
        p.fillRect(0, 0, depth, H, gl)
        p.fillRect(W - depth, 0, depth, H, gr)

        # corner light pooling
        cr = depth * 1.7
        for cx, cy in ((0, 0), (W, 0), (0, H), (W, H)):
            rg = QRadialGradient(cx, cy, cr)
            rg.setColorAt(0.0, col(52))
            rg.setColorAt(0.5, col(14))
            rg.setColorAt(1.0, col(0))
            p.fillRect(int(cx - cr), int(cy - cr), int(cr * 2), int(cr * 2), rg)

        # crisp glowing edge line
        edge = col(180)
        t = 2
        p.fillRect(0, 0, W, t, edge)
        p.fillRect(0, H - t, W, t, edge)
        p.fillRect(0, 0, t, H, edge)
        p.fillRect(W - t, 0, t, H, edge)

    def resizeEvent(self, _):
        if self._phase != "idle":
            self._reposition_card()

    # ---------------------------------------------------------------- animation
    def _tick(self):
        self._frame += 1
        self._t += 0.10
        if self._phase != "idle":
            if self._frame % CARD_STEP == 0:
                self._refresh_card()
            self.update()

    def _set(self, label, key, text):
        """Set label text only when it changed. True if it did."""
        if self._shown.get(key) == text:
            return False
        self._shown[key] = text
        label.setText(text)
        return True

    def _refresh_card(self):
        """Render the card for the current phase and animation frame."""
        c = self.card
        f = self._frame // CARD_STEP
        ph = self._phase
        relayout = False

        if ph == "listening":
            eq = EQ_FRAMES[(f // 3) % 3]
            state = (f'<span style="color:{MOSS}">слушаю</span>&nbsp;&nbsp;'
                     f'<span style="font-family:{CODE_FAMILY}; color:{MOSS}">{eq}</span>')
            c.check.hide()
        elif ph == "working":
            spin = SPIN_FRAMES[(f // 2) % 4]
            state = (f'<span style="color:{OCHRE}">работает</span>&nbsp;&nbsp;'
                     f'<span style="font-family:{CODE_FAMILY}; color:{OCHRE}">{spin}</span>')
            c.check.hide()
        else:  # answering
            state = f'<span style="color:{MUTED}">готово</span>'
            c.check.show()
        self._set(c.state, "state", state)

        if ph == "listening":
            if self._speech_text:
                caret = f'<span style="font-family:{CODE_FAMILY}; color:{MOSS}">█</span>' if (f // 5) % 2 == 0 else ""
                say = f'<span style="color:{TEXT}">{html.escape(self._speech_text)}</span>{caret}'
            else:
                say = f'<span style="color:{MUTED}">…</span>'
        else:
            say = f'<span style="color:{MUTED}">{html.escape(self._speech_text)}</span>'
        self._set(c.speech, "speech", say)
        if self._shown.get("speech_plain") != self._speech_text:  # caret blinks must not re-layout
            self._shown["speech_plain"] = self._speech_text
            relayout = True

        agent = self._agent_text if ph == "answering" else ""
        self._set(c.agent, "agent", agent)
        if self._shown.get("agent_plain") != agent:
            self._shown["agent_plain"] = agent
            relayout = True

        for widget, visible in ((c.speech, ph == "listening" or bool(self._speech_text)),
                                (c.agent, ph == "answering")):
            if widget.isVisible() != visible and c.isVisible():
                widget.setVisible(visible)
                relayout = True
        if relayout:
            self._reposition_card()

    # -------------------------------------------------------------------- queue
    def _drain(self):
        try:
            while True:
                self._handle(self.gui_queue.get_nowait())
        except queue.Empty:
            pass

    def _handle(self, msg):
        t = msg.get("type")

        if t == "phase":
            name = msg.get("name", "idle")
            if msg.get("reset", name == "idle"):
                self._speech_text = ""
                self._agent_text = ""
            self._apply_phase(name)

        elif t == "user_speech":
            self._speech_text = msg.get("text", "")
            if self._phase == "idle":
                self._apply_phase("listening")
            else:
                self._refresh_card()

        elif t == "agent_response_chunk":
            self._agent_text = msg.get("text", "")
            if self._phase != "answering":
                self._apply_phase("answering")
            else:
                self._refresh_card()

        elif t == "status":  # legacy compatibility
            txt = msg.get("text", "")
            if "Ожидание" in txt or "Запуск" in txt:
                self._apply_phase("idle")
            elif any(k in txt for k in ("Слушаю", "Говорите")):
                self._apply_phase("listening")
            elif any(k in txt for k in ("Дума", "Анализ", "Говорю")):
                self._apply_phase("working")

    # -------------------------------------------------------------------- phase
    def _apply_phase(self, name):
        if name not in PHASES:
            return
        self._phase = name
        c = self.card

        if name == "idle":
            c.hide()
            self.update()
            return

        self._shown.clear()
        c.show()
        c.speech.setVisible(name == "listening" or bool(self._speech_text))
        c.agent.setVisible(name == "answering")
        self._refresh_card()
        self._reposition_card()
        self.update()

    # -------------------------------------------------------------------- close
    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            QApplication.quit()

    def closeEvent(self, e):
        try:
            self.stop_event_callback()
        except Exception:
            pass
        super().closeEvent(e)


class SubtitleOverlay:
    """Kept API-compatible with the old tkinter class: construct, then mainloop()."""

    def __init__(self, gui_queue, stop_event_callback, stop_event=None):
        self.gui_queue = gui_queue
        self.stop_event_callback = stop_event_callback
        self.stop_event = stop_event
        self._win = None

    def mainloop(self):
        app = QApplication.instance() or QApplication(sys.argv)
        _load_fonts()
        self._win = _OverlayWindow(self.gui_queue, self.stop_event_callback, self.stop_event)
        self._win.show()
        self._win.raise_()
        app.exec()
