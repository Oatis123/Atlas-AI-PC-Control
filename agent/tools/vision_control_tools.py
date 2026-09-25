"""Tools for the screenshot-driven (VLM) window agent.

The visual agent SEES a screenshot of the target window and acts on it by pixel
coordinates read straight from that image — no OmniParser, no element IDs. Good
for Chromium/Electron apps (Spotify, Discord), games, canvas UIs, and any window
where the structured agent can't map elements.
"""

import base64
import logging
import time
from io import BytesIO

from PIL import Image, ImageGrab
from langchain_core.tools import tool

import pyautogui

from agent.tools.pc_control_tools import _get_window_by_name, _focus_window

_MAX_W = 1280
# hwnd -> (scale, shown_w, shown_h) of the last capture_window for that window.
# Keyed by HWND, not by name: titles change (Spotify renames its window per track).
_CAPTURE_META: dict = {}


def _resolve_window(name: str):
    """Bring the window forward and return (hwnd, current screen rectangle)."""
    win = _get_window_by_name(name)
    _focus_window(win)
    r = win.rectangle()
    if r.width() <= 0 or r.height() <= 0:
        raise RuntimeError(f"Окно '{name}' свернуто или имеет нулевой размер.")
    return win.handle, r


def _to_screen(name: str, x, y):
    """Map an (x, y) read off the last screenshot to absolute screen coordinates.
    Refuses points outside that screenshot: such a click lands on whatever is
    beyond the window (e.g. the taskbar button, which minimizes the app)."""
    hwnd, r = _resolve_window(name)
    meta = _CAPTURE_META.get(hwnd)
    if meta is None:
        raise RuntimeError(f"Сначала сделай capture_window для окна '{name}'.")
    scale, w, h = meta
    x, y = float(x), float(y)
    if not (0 <= x < w and 0 <= y < h):
        raise RuntimeError(
            f"Точка ({x:.0f}, {y:.0f}) вне скриншота {w}x{h} px. "
            f"Бери координаты только внутри последней картинки из capture_window."
        )
    return r.left + int(x * scale), r.top + int(y * scale)


@tool
def capture_window(name: str) -> dict:
    """
    Скриншотит указанное окно и возвращает картинку для анализа. Это ВСЕГДА твоё
    первое действие в задаче. Возвращает изображение и его размер в пикселях —
    координаты для click_window / scroll_window бери прямо с этой картинки.

    Args:
        name (str): Часть заголовка окна (например 'Spotify Premium').
    """
    hwnd, r = _resolve_window(name)
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    native_w, native_h = img.size

    scale = 1.0
    if native_w > _MAX_W:
        scale = native_w / _MAX_W
        img = img.resize((_MAX_W, int(native_h / scale)), Image.Resampling.LANCZOS)
    _CAPTURE_META[hwnd] = (scale, img.size[0], img.size[1])

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    logging.info(
        f"[Visual Agent] capture_window '{name}': {img.size[0]}x{img.size[1]} px (scale {scale:.2f})"
    )
    return {
        "screenshot_data": b64,
        "mime_type": "image/jpeg",
        "width": img.size[0],
        "height": img.size[1],
    }


@tool
def click_window(name: str, x: int, y: int, button: str = "left") -> str:
    """
    Кликает по точке (x, y), СЧИТАННОЙ со скриншота из capture_window.
    Целься в ЦЕНТР нужного элемента (кнопки, ссылки, поля, пункта списка).

    Args:
        name (str): Часть заголовка окна.
        x (int): Координата X на последней картинке из capture_window.
        y (int): Координата Y на последней картинке из capture_window.
        button (str): 'left' (обычный клик), 'double' (двойной), 'right' (правый).
    """
    try:
        sx, sy = _to_screen(name, x, y)
    except Exception as e:
        return f"Ошибка: {e}"
    if button == "double":
        pyautogui.doubleClick(sx, sy)
    elif button == "right":
        pyautogui.rightClick(sx, sy)
    else:
        pyautogui.click(sx, sy)
    time.sleep(0.3)
    return f"Клик ({button}) по ({x}, {y}) выполнен."


@tool
def scroll_window(name: str, x: int, y: int, direction: str = "down", amount: int = 5) -> str:
    """
    Прокручивает колесом мыши в точке (x, y) со скриншота capture_window.

    Args:
        name (str): Часть заголовка окна.
        x (int): Координата X на картинке из capture_window (над чем крутить).
        y (int): Координата Y на картинке из capture_window.
        direction (str): 'up' / 'down' / 'left' / 'right'.
        amount (int): Сила прокрутки (кол-во «щелчков» колеса), по умолчанию 5.
    """
    try:
        sx, sy = _to_screen(name, x, y)
    except Exception as e:
        return f"Ошибка: {e}"
    pyautogui.moveTo(sx, sy)
    ticks = max(1, int(amount)) * 120
    if direction == "up":
        pyautogui.scroll(ticks)
    elif direction == "down":
        pyautogui.scroll(-ticks)
    elif direction == "left":
        pyautogui.hscroll(-ticks)
    elif direction == "right":
        pyautogui.hscroll(ticks)
    else:
        return f"Ошибка: неизвестное направление '{direction}'."
    time.sleep(0.3)
    return f"Прокрутка {direction} в точке ({x}, {y}) выполнена."
