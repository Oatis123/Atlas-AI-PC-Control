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

from agent.tools.pc_control_tools import _get_window_by_name

_MAX_W = 1280
# window name -> scale factor (native_px / shown_px) from the last capture_window
_CAPTURE_SCALE: dict = {}


def _resolve_rect(name: str):
    """Bring the window forward and return its current screen rectangle."""
    win = _get_window_by_name(name)
    try:
        if not win.is_active():
            win.set_focus()
            time.sleep(0.2)
    except Exception:
        pass
    r = win.rectangle()
    if r.width() <= 0 or r.height() <= 0:
        raise RuntimeError(f"Окно '{name}' свернуто или имеет нулевой размер.")
    return r


def _to_screen(name: str, x, y):
    """Map an (x, y) read off the last screenshot to absolute screen coordinates."""
    scale = _CAPTURE_SCALE.get(name, 1.0)
    r = _resolve_rect(name)
    return r.left + int(float(x) * scale), r.top + int(float(y) * scale)


@tool
def capture_window(name: str) -> dict:
    """
    Скриншотит указанное окно и возвращает картинку для анализа. Это ВСЕГДА твоё
    первое действие в задаче. Возвращает изображение и его размер в пикселях —
    координаты для click_window / scroll_window бери прямо с этой картинки.

    Args:
        name (str): Часть заголовка окна (например 'Spotify Premium').
    """
    r = _resolve_rect(name)
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    native_w, native_h = img.size

    scale = 1.0
    if native_w > _MAX_W:
        scale = native_w / _MAX_W
        img = img.resize((_MAX_W, int(native_h / scale)), Image.Resampling.LANCZOS)
    _CAPTURE_SCALE[name] = scale

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
