"""Tools for the screenshot-driven (VLM) window agent.

The window agent SEES a screenshot of the target window and acts on it by pixel
coordinates read straight from that image — no element tree, no element IDs. Works
for any window: native apps, Chromium/Electron apps (Spotify, Discord), games, canvas UIs.
"""

import base64
import logging
import time
from io import BytesIO

from PIL import Image, ImageChops, ImageGrab
from langchain_core.tools import tool

import pyautogui

from agent.tools.pc_control_tools import _get_window_by_name, _focus_window

_MAX_W = 1280

# Ожидание «устаканивания» экрана: два кадра с интервалом сравниваются по уменьшенным
# копиям (сравнение ~3 мс, дорогой только сам grab ~75 мс). Пока кадры отличаются —
# ждём дальше, но не дольше _SETTLE_TIMEOUT (видео/анимация никогда не замирают).
_SETTLE_INTERVAL = 0.5
_SETTLE_TIMEOUT = 4.0
_THUMB_REDUCE = 8          # во сколько раз уменьшаем кадр для сравнения
_PIXEL_DELTA = 16          # разница яркости (0..255), с которой пиксель считается изменённым
_CHANGED_FRACTION = 0.003  # доля изменённых пикселей, ниже которой экран считается тем же
                           # (мигающий курсор и шум не должны мешать)
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


def _grab(r):
    return ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)


def _thumb(img: Image.Image) -> Image.Image:
    return img.reduce(_THUMB_REDUCE).convert("L")


def _same_screen(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:  # окно изменило размер — точно не устоялось
        return False
    changed = sum(ImageChops.difference(a, b).histogram()[_PIXEL_DELTA:])
    return changed <= a.width * a.height * _CHANGED_FRACTION


def _grab_settled(r):
    """Кадр окна после того, как экран перестал меняться (два подряд одинаковых кадра
    с интервалом _SETTLE_INTERVAL). Возвращает (кадр, секунд ожидания, устоялся ли)."""
    t0 = time.monotonic()
    img = _grab(r)
    thumb = _thumb(img)
    while time.monotonic() - t0 < _SETTLE_TIMEOUT:
        time.sleep(_SETTLE_INTERVAL)
        nxt = _grab(r)
        nxt_thumb = _thumb(nxt)
        same = _same_screen(thumb, nxt_thumb)
        img, thumb = nxt, nxt_thumb
        if same:
            return img, time.monotonic() - t0, True
    return img, time.monotonic() - t0, False


@tool
def capture_window(name: str, settle: bool = True) -> dict:
    """
    Скриншотит указанное окно и возвращает картинку для анализа. Первый скриншот
    уже приложен к задаче; вызывай это после действий, чтобы увидеть результат.
    Сам дожидается, пока экран перестанет меняться (загрузка страницы, анимация),
    поэтому перед ним НЕ нужно звать waiting. Возвращает изображение и его размер
    в пикселях — координаты для click_window / scroll_window бери прямо с этой картинки.

    Args:
        name (str): Часть заголовка окна (например 'Spotify Premium').
        settle (bool): Ждать, пока экран устоится (по умолчанию True, до ~4 с).
    """
    hwnd, r = _resolve_window(name)
    if settle:
        img, waited, stable = _grab_settled(r)
        settle_note = f", устоялся за {waited:.2f}с" if stable else f", НЕ устоялся за {waited:.1f}с (анимация/видео?)"
    else:
        img, settle_note = _grab(r), ""
    native_w, native_h = img.size

    scale = 1.0
    if native_w > _MAX_W:
        scale = native_w / _MAX_W
        img = img.resize((_MAX_W, int(native_h / scale)), Image.Resampling.BILINEAR)
    _CAPTURE_META[hwnd] = (scale, img.size[0], img.size[1])

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    logging.info(
        f"[Window Agent] capture_window '{name}': {img.size[0]}x{img.size[1]} px (scale {scale:.2f}){settle_note}"
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
