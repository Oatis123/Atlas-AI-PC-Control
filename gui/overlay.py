"""Qt (PySide6) full-screen overlay for the voice assistant.

Real per-pixel alpha, so the screen-edge light actually glows into transparency
(impossible with tkinter's binary -transparentcolor key).

Phases:
  idle       - nothing shown, faint static edge glow
  listening  - card with the live dictated text
  working    - card: dictated command (dim) + "Atlas работает…"
  answering  - card: agent's response

Talks to main.py through the same thread-safe queue protocol:
  {'type': 'phase',                'name': 'idle'|'listening'|'working'|'answering', 'reset': bool}
  {'type': 'user_speech',          'text': <partial or final transcript>}
  {'type': 'agent_response_chunk', 'text': <answer text>}
  {'type': 'status', ...}          - legacy messages, still understood
"""

import sys
import math
import queue

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QLinearGradient, QRadialGradient, QFont
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout

PHASE_RGB = {
    "idle":      (110, 110, 110),
    "listening": (255, 255, 255),
    "working":   (255, 140, 0),
    "answering": (0, 231, 103),
}
PHASES = ("idle", "listening", "working", "answering")

SPEECH_FG = "#F2F2F5"
SPEECH_DIM_FG = "#83838A"
STATUS_FG = "#D8D8DC"
AGENT_FG = "#AFEEEE"


class _Card(QWidget):
    BG = QColor(22, 22, 23, 236)
    STROKE = QColor(255, 255, 255, 26)

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 15)
        lay.setSpacing(8)

        self.speech = QLabel("")
        self.status = QLabel("")
        self.agent = QLabel("")
        for lbl, size, col in (
            (self.speech, 14, SPEECH_FG),
            (self.status, 12, STATUS_FG),
            (self.agent, 15, AGENT_FG),
        ):
            lbl.setWordWrap(True)
            lbl.setFont(QFont("Segoe UI", size))
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet(f"color: {col}; background: transparent;")
            lay.addWidget(lbl)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(self.BG)
        p.setPen(self.STROKE)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)


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
        self._t = 0.0

        self.card = _Card(self)
        self.card.hide()

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(33)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._drain)
        self._poll.start(40)

        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_stop)
        self._watch.start(250)

    # ------------------------------------------------------------------ helpers
    def _rgb(self):
        return PHASE_RGB.get(self._phase, PHASE_RGB["idle"])

    def _pulse(self):
        return 0.5 + 0.5 * math.sin(self._t)

    def _reposition_card(self):
        w = max(340, min(int(self.width() * 0.40), 680))
        self.card.setFixedWidth(w)
        self.card.layout().activate()
        self.card.setFixedHeight(self.card.sizeHint().height())
        x = (self.width() - w) // 2
        y = self.height() - self.card.height() - 56
        self.card.move(x, max(0, y))

    # ------------------------------------------------------------------- render
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
        self._t += 0.10
        if self._phase == "working":
            r, g, b = self._rgb()
            dots = "." * (int(self._t * 1.4) % 4)
            self.card.status.setText(
                f'<span style="color:rgb({r},{g},{b})">&#9679;</span>'
                f'&nbsp;&nbsp;Atlas работает{dots}'
            )
        self.update()

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
            self._apply_phase(name)

        elif t == "user_speech":
            self._speech_text = msg.get("text", "")
            if self._phase == "idle":
                self._apply_phase("listening")
            elif self._phase == "listening":
                self.card.speech.setText(self._speech_text or "Слушаю…")
                self._reposition_card()
            elif self._speech_text:
                self._apply_phase(self._phase)  # refresh dim command line

        elif t == "agent_response_chunk":
            if self._phase != "answering":
                self._apply_phase("answering")
            self.card.agent.setText(msg.get("text", ""))
            self._reposition_card()

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
        s, st, ag = self.card.speech, self.card.status, self.card.agent

        if name == "idle":
            self.card.hide()
            self.update()
            return

        if name == "listening":
            s.setStyleSheet(f"color: {SPEECH_FG}; background: transparent;")
            s.setText(self._speech_text or "Слушаю…")
            s.show()
            st.hide()
            ag.hide()

        elif name == "working":
            s.setStyleSheet(f"color: {SPEECH_DIM_FG}; background: transparent;")
            s.setText(self._speech_text)
            s.setVisible(bool(self._speech_text))
            st.setText("Atlas работает")
            st.show()
            ag.hide()

        elif name == "answering":
            s.setStyleSheet(f"color: {SPEECH_DIM_FG}; background: transparent;")
            s.setText(self._speech_text)
            s.setVisible(bool(self._speech_text))
            st.hide()
            ag.show()

        self.card.show()
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
        self._win = _OverlayWindow(self.gui_queue, self.stop_event_callback, self.stop_event)
        self._win.show()
        self._win.raise_()
        app.exec()
