"""Click-through Atlas voice overlay driven by the assistant's GUI queue."""

import queue
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QTextLayout, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QStyle,
    QVBoxLayout, QWidget,
)

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Dark palette from oatis-style v2.1
RAISED = "#1F2118"
LINE = "#2B2D23"
MUTED = "#8E8D80"
TEXT = "#EEECE0"
PROSE = "#DAD8CC"
MOSS = "#A6B96A"
OCHRE = "#E5A04B"
MOSS_WASH = "#272B1B"
OCHRE_WASH = "#312716"

STATUS = {
    "idle": ("В ожидании", MUTED, RAISED,
             '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    "listening": ("Слушаю", MOSS, MOSS_WASH,
                  '<path d="M12 19v3M19 10v2a7 7 0 0 1-14 0v-2"/>'
                  '<rect x="9" y="2" width="6" height="13" rx="3"/>'),
    "working": ("Выполняю", OCHRE, OCHRE_WASH,
                '<path d="M12 3a9 9 0 1 1-9 9"/>'),
    "answering": ("Ответ", MOSS, MOSS_WASH,
                  '<path d="M21 16a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14'
                  'a2 2 0 0 1 2 2zM8 8h8M8 13h5"/>'),
}


def _font(families, size, *, italic=False):
    font = QFont()
    font.setFamilies(families)
    font.setPixelSize(size)
    font.setItalic(italic)
    return font


class _StatusPill(QFrame):
    def __init__(self, parent, phase, animate):
        super().__init__(parent)
        self.setObjectName("statusPill")
        self._phase = None
        self._animate = animate
        self._svg = QSvgRenderer(self)

        self.label = QLabel()
        self.label.setFont(_font(["Commissioner SemiBold", "Commissioner", "Segoe UI"], 14))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(34, 5, 12, 5)
        layout.addWidget(self.label)
        self.setFixedHeight(34)

        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1000)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self.update)
        self.set_phase(phase)

    def set_phase(self, phase):
        if phase == self._phase:
            return
        self._phase = phase
        label, color, fill, icon = STATUS[phase]
        border = LINE if phase == "idle" else fill
        self.setStyleSheet(
            f"QFrame#statusPill {{ background: {fill}; border: 1px solid {border};"
            " border-radius: 16px; }"
            f" QLabel {{ color: {color}; background: transparent; }}"
        )
        self.label.setText(label)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
               f' stroke="{color}" stroke-width="1.75" stroke-linecap="round"'
               f' stroke-linejoin="round">{icon}</svg>')
        self._svg.load(QByteArray(svg.encode("utf-8")))
        self._spin.stop()
        if phase == "working" and self._animate and self.isVisible():
            self._spin.start()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self._phase == "working" and self._animate:
            self._spin.start()

    def hideEvent(self, event):
        self._spin.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(20, self.height() / 2)
        if self._phase == "working" and self._animate:
            painter.rotate(self._spin.currentValue())
        self._svg.render(painter, QRectF(-8, -8, 16, 16))


class _Caption(QWidget):
    """Wrap plain text to the available area, including long paths and URLs."""

    def __init__(self, font):
        super().__init__()
        self.setFont(font)
        self._content = None
        self._layouts = []
        self._lines = []

    def set_caption(self, text, color, width, height):
        if self._content == (text, color, width, height):
            return
        self._content = text, color, width, height
        self.setAccessibleName(text)
        self._color = QColor(color)
        self._lines.clear()
        self._layouts.clear()
        self._overflow = False
        metrics = self.fontMetrics()
        self._line_height = max(metrics.height(), round(self.font().pixelSize() * 1.55))
        limit = max(1, height // self._line_height)
        options = QTextOption()
        options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

        for paragraph in text.split("\n"):
            layout = QTextLayout(paragraph or " ", self.font(), self)
            layout.setTextOption(options)
            layout.setCacheEnabled(True)
            self._layouts.append(layout)
            layout.beginLayout()
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                if len(self._lines) == limit:
                    self._overflow = True
                    break
                line.setLineWidth(width)
                self._lines.append(line)
            layout.endLayout()
            if self._overflow:
                break
        self.setFixedHeight(len(self._lines) * self._line_height)
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self._color)
        lines = self._lines[:-1] if self._overflow else self._lines
        for index, line in enumerate(lines):
            line.draw(painter, QPointF(0, index * self._line_height))
        if self._overflow:
            baseline = len(lines) * self._line_height + self.fontMetrics().ascent()
            painter.drawText(0, baseline, "…")


class _Card(QFrame):
    def __init__(self, parent, animate):
        super().__init__(parent)
        self.setObjectName("voiceCard")
        self.setStyleSheet(
            f"QFrame#voiceCard {{ background: {RAISED}; border: 1px solid {LINE};"
            " border-radius: 20px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)

        brand = QLabel("ATLAS")
        brand.setFont(_font(["Silkscreen", "Consolas"], 16))
        brand.setStyleSheet(f"color: {TEXT};")
        self.status = _StatusPill(self, "listening", animate)
        header = QHBoxLayout()
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(self.status)
        layout.addLayout(header)

        self.speech = _Caption(_font(["Literata", "Georgia"], 17, italic=True))
        self.answer = _Caption(_font(["Literata", "Georgia"], 20))
        self.separator = QFrame()
        self.separator.setFixedHeight(1)
        self.separator.setStyleSheet(f"background: {LINE};")
        layout.addWidget(self.speech)
        layout.addWidget(self.separator)
        layout.addWidget(self.answer)

    def render(self, phase, speech, answer, width, height):
        self.setFixedWidth(width)
        self.status.set_phase(phase)
        listening = phase == "listening"
        answering = phase == "answering"
        if listening and not speech:
            speech = "Говорите…"
            speech_color = MUTED
        else:
            speech_color = TEXT if listening else MUTED

        body_width = width - 48
        # Budget for the header, padding, divider and spacing between both captions.
        body_height = height - 108
        if speech:
            speech_height = body_height // 4 if answering and answer else body_height
            self.speech.set_caption(speech, speech_color, body_width, speech_height)
            body_height -= self.speech.height()
        if answering and answer:
            self.answer.set_caption(answer, PROSE, body_width, body_height)

        self.speech.setVisible(bool(speech))
        self.answer.setVisible(answering and bool(answer))
        self.separator.setVisible(answering and bool(speech and answer))
        self.setFixedHeight(self.layout().sizeHint().height())


class _OverlayWindow(QWidget):
    def __init__(self, gui_queue, stop_event_callback, stop_event):
        super().__init__()
        self.gui_queue = gui_queue
        self.stop_event_callback = stop_event_callback
        self.stop_event = stop_event
        self._phase = "idle"
        self._speech_text = ""
        self._agent_text = ""

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        for name in ("Commissioner-SemiBold.ttf", "Literata-Italic.ttf",
                     "Literata-Regular.ttf", "Silkscreen-Regular.ttf"):
            QFontDatabase.addApplicationFont(str(FONTS_DIR / name))

        animate = bool(self.style().styleHint(QStyle.StyleHint.SH_Widget_Animate))
        self._idle = _StatusPill(self, "idle", animate)
        self.card = _Card(self, animate)
        self.card.hide()
        self._fade = None
        if animate:
            opacity = QGraphicsOpacityEffect(self.card)
            opacity.setOpacity(1.0)
            self.card.setGraphicsEffect(opacity)
            self._fade = QVariantAnimation(self)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.setDuration(170)
            curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
            curve.addCubicBezierSegment(QPointF(.2, .7), QPointF(.2, 1), QPointF(1, 1))
            self._fade.setEasingCurve(curve)
            self._fade.valueChanged.connect(opacity.setOpacity)

        screen = QApplication.primaryScreen()
        self.setGeometry(screen.availableGeometry())
        screen.availableGeometryChanged.connect(self.setGeometry)
        self._refresh()

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._drain)
        self._poll.start(40)

        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_stop)
        self._watch.start(250)

    def resizeEvent(self, _):
        self._refresh()

    def _refresh(self):
        bottom = self.height() - 24
        if self._phase == "idle":
            self._idle.adjustSize()
            self._idle.move((self.width() - self._idle.width()) // 2, bottom - self._idle.height())
        else:
            width = min(600, self.width() - 48)
            self.card.render(self._phase, self._speech_text, self._agent_text, width, bottom - 24)
            self.card.move((self.width() - width) // 2, bottom - self.card.height())

    def _drain(self):
        while True:
            try:
                msg = self.gui_queue.get_nowait()
            except queue.Empty:
                return
            self._handle(msg)

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "phase":
            name = msg.get("name", "idle")
            if msg.get("reset", name == "idle"):
                self._speech_text = ""
                self._agent_text = ""
            self._apply_phase(name)
        elif kind == "user_speech":
            self._speech_text = msg.get("text", "")
            self._apply_phase("listening" if self._phase == "idle" else self._phase)
        elif kind == "agent_response_chunk":
            self._agent_text = msg.get("text", "")
            self._apply_phase("answering")
        elif kind == "status":
            text = msg.get("text", "")
            if "Ожидание" in text or "Запуск" in text:
                self._apply_phase("idle")
            elif any(word in text for word in ("Слушаю", "Говорите")):
                self._apply_phase("listening")
            elif any(word in text for word in ("Дума", "Анализ", "Говорю")):
                self._apply_phase("working")

    def _apply_phase(self, name):
        if name not in STATUS:
            return
        was_idle = self._phase == "idle"
        self._phase = name
        self._refresh()
        if name == "idle":
            if self._fade is not None:
                self._fade.stop()
            self.card.hide()
            self._idle.show()
        else:
            self._idle.hide()
            if was_idle and self._fade is not None:
                self.card.graphicsEffect().setOpacity(0.0)
                self._fade.start()
            self.card.show()

    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            QApplication.quit()

    def closeEvent(self, event):
        try:
            self.stop_event_callback()
        except Exception:
            pass
        super().closeEvent(event)


class SubtitleOverlay:
    """Launch the Qt overlay from the main thread."""

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
