"""scrape_application_vision — scrape окна через vision-LLM вместо OmniParser.

Для нестандартных интерфейсов (Spotify и прочие Electron/Chromium-приложения,
кастомная отрисовка) и для случаев, когда YOLO + EasyOCR не справляются: элементы
приходят безымянными `Icon_N` или нужного элемента в выводе нет вовсе.

Скриншот окна уходит vision-модели сообщением от пользователя, модель возвращает
JSON со списком элементов и их рамками. Результат кладётся в тот же ELEMENTS_CACHE
и отдаётся в том же формате <WindowVision>, что и у scrape_application, поэтому
агент дальше работает с этими id через обычный interact_with_element_by_id.

Модель задаётся в agent/models/openrouter_models.py -> `vision_scraper`.
"""

import base64
import json
import logging
import re
import time
from io import BytesIO

from PIL import Image, ImageGrab
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pywinauto.findwindows import ElementNotFoundError

from agent.models import openrouter_models
from agent.tools.pc_control_tools import _get_window_by_name, _focus_window, _register_elements

_MAX_W = 1280  # скриншот ужимается до этой ширины (токены картинки), координаты масштабируются обратно

_SYSTEM_PROMPT = """You are a precise UI parser. The user sends a screenshot of an application window.
List every interactive or informative UI element visible on it: buttons, icon buttons, links,
input fields, search boxes, tabs, menu items, list rows (tracks, playlists, files, messages),
checkboxes, toggles, sliders, and important text (titles, values, results, labels, status).

Return ONLY a JSON array — no prose, no markdown fences. Each item:
{"type": "<Button|Icon|Input|Link|Tab|MenuItem|ListItem|Checkbox|Slider|Text>",
 "name": "<visible text, or a short description for an icon, e.g. 'Play', 'Search icon', 'Next track'>",
 "box": [x1, y1, x2, y2]}

Rules:
- box = pixel coordinates IN THIS IMAGE (top-left origin), tight around the element.
- Order items top-to-bottom, then left-to-right.
- Only elements that are actually visible. Never invent anything.
- Keep names short and in the language shown on screen."""


def _extract_json_array(text: str):
    """Достаёт JSON-массив из ответа модели (терпимо к ```json-обёрткам и тексту вокруг)."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("в ответе модели нет JSON-массива")
    return json.loads(text[start:end + 1])


def _content_text(content) -> str:
    """content ответа бывает строкой или списком блоков — приводим к строке."""
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


@tool
async def scrape_application_vision(name: str) -> str:
    """
    Сканирует окно через vision-LLM (модель смотрит на скриншот), а не через OmniParser.
    Используй для НЕСТАНДАРТНЫХ интерфейсов (Spotify, Discord, браузеры, кастомная
    отрисовка) и когда scrape_application вернул в основном безымянные `Icon_N` или
    в нём нет нужного элемента. Возвращает тот же формат <WindowVision> с id —
    дальше работай с ними через interact_with_element_by_id как обычно.

    Args:
        name (str): Часть заголовка окна для поиска.
    """
    llm = openrouter_models.vision_scraper
    if llm is None:
        return ("Ошибка: vision-модель для scrape_application_vision не настроена "
                "(agent/models/openrouter_models.py -> vision_scraper). Используй scrape_application.")

    start_time = time.time()
    logging.info(f"🔍 [VisionScrape] Поиск окна: '{name}'...")
    try:
        main_win = _get_window_by_name(name)
    except ElementNotFoundError:
        return f"Ошибка: Окно с именем, содержащим '{name}', не найдено."

    _focus_window(main_win)
    rect = main_win.rectangle()
    if rect.width() <= 0 or rect.height() <= 0:
        return "Ошибка: Окно имеет нулевой размер или свернуто."

    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    native_w, native_h = img.size
    scale = 1.0
    if native_w > _MAX_W:
        scale = native_w / _MAX_W
        img = img.resize((_MAX_W, int(native_h / scale)), Image.Resampling.LANCZOS)
    shown_w, shown_h = img.size

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": f"Screenshot of the window '{name}' ({shown_w}x{shown_h} px). Return the JSON array."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]),
    ]

    logging.info(f"🧠 [VisionScrape] Отправка скриншота {shown_w}x{shown_h} в vision-модель...")
    try:
        t0 = time.time()
        response = await llm.ainvoke(messages)
        raw = _content_text(response.content)
        logging.info(f"⏱️ [VisionScrape] Ответ модели за {time.time() - t0:.2f}с")
    except Exception as e:
        logging.error(f"💥 [VisionScrape] Ошибка вызова vision-модели: {e}", exc_info=True)
        return f"Ошибка vision-скрейпа: {e}"

    try:
        items = _extract_json_array(raw)
    except Exception as e:
        logging.error(f"💥 [VisionScrape] Не удалось разобрать ответ: {e}. Ответ: {raw[:500]!r}")
        return f"Ошибка vision-скрейпа: модель вернула не JSON ({e})."

    # Координаты с картинки -> пиксели окна (обратно к исходному масштабу), с отсевом мусора
    elements = []
    for item in items:
        try:
            x1, y1, x2, y2 = (float(v) for v in item["box"])
        except Exception:
            continue
        x1, x2 = sorted((max(0.0, min(x1, shown_w)), max(0.0, min(x2, shown_w))))
        y1, y2 = sorted((max(0.0, min(y1, shown_h)), max(0.0, min(y2, shown_h))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        elements.append({
            "left": int(x1 * scale), "top": int(y1 * scale),
            "right": int(x2 * scale), "bottom": int(y2 * scale),
            "name": str(item.get("name", "")).strip() or "Unnamed",
            "control_type": re.sub(r"[^A-Za-z_]", "", str(item.get("type", "Element"))) or "Element",
        })

    if not elements:
        logging.warning(f"⚠️ [VisionScrape] Модель не вернула ни одного валидного элемента. Ответ: {raw[:300]!r}")
        return "Vision-модель не обнаружила элементов на скриншоте окна."

    xml_output = _register_elements(elements, rect)
    logging.info(f"⏱️ [VisionScrape] Окно '{name}' распознано за {time.time() - start_time:.2f}с. Найдено {len(elements)} элементов!")
    return xml_output
