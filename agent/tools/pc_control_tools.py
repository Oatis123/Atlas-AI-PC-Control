import subprocess
import winreg
import shlex
import psutil
import time
import ctypes
import logging
import re
from typing import List, Dict, Any, Union, Optional

from langchain_core.tools import tool

_SOFTWARE_CACHE = None
_SOFTWARE_CACHE_TIME = 0
_MODERN_APPS_CACHE = {}          # appx Name (lower) -> PackageFamilyName
_STARTAPPS_CACHE = {}            # friendly Start-menu name (lower) -> AppID
_CLASSIC_APP_PATHS_CACHE = None
_CLASSIC_APP_PATHS_CACHE_TIME = 0
from pywinauto import Desktop
from pywinauto.application import Application
from pywinauto.findwindows import ElementNotFoundError
from PIL import ImageGrab

import pyautogui
import pyperclip

ELEMENTS_CACHE = {}
CURRENT_ID = 0

def _type_unicode_text(text: str):
    """Reliable method for entering Unicode text (including non-ASCII) via the system clipboard."""
    
    if '\n' in text:
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if line:
                pyperclip.copy(line)
                time.sleep(0.1) # Wait for Windows to update clipboard
                pyautogui.hotkey('shift', 'insert') # Pastes layout-independently
                time.sleep(0.05)
            if i < len(lines) - 1:
                pyautogui.press('enter')
    else:
        pyperclip.copy(text)
        time.sleep(0.1) # Wait for Windows to update clipboard
        pyautogui.hotkey('shift', 'insert') # Pastes layout-independently
        time.sleep(0.05)

def _decamel(text: str) -> str:
    """`Microsoft.WindowsCalculator` -> `Windows Calculator` (last dotted segment, split on caps)."""
    segment = text.split('.')[-1]
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', segment)
    spaced = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', spaced)
    return spaced.strip()


def _get_installed_software():
    global _SOFTWARE_CACHE, _SOFTWARE_CACHE_TIME, _MODERN_APPS_CACHE, _STARTAPPS_CACHE
    if _SOFTWARE_CACHE is not None and (time.time() - _SOFTWARE_CACHE_TIME < 300):
        return _SOFTWARE_CACHE

    all_apps = set()

    # 1. Start-menu apps: friendly names + launchable AppIDs (Win32 shortcuts + UWP).
    command_startapps = r'Get-StartApps | ForEach-Object { "$($_.Name)|$($_.AppID)" }'
    result_startapps = subprocess.run(["powershell", "-Command", command_startapps], capture_output=True, text=True, encoding='utf-8', errors='ignore')
    if result_startapps.returncode == 0:
        for line in result_startapps.stdout.splitlines():
            line = line.strip()
            if '|' in line:
                name, app_id = line.split('|', 1)
                name = name.strip()
                if name:
                    _STARTAPPS_CACHE[name.lower()] = app_id.strip()
                    all_apps.add(name)

    # 2. Classic installed programs (registry Uninstall DisplayName).
    command_classic = r'''
    Get-ItemProperty HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*,
                     HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*,
                     HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* |
    Where-Object {$_.PSObject.Properties['DisplayName'] -and $_.DisplayName -ne $null} |
    Select-Object -ExpandProperty DisplayName
    '''

    result_classic = subprocess.run(["powershell", "-Command", command_classic], capture_output=True, text=True, encoding='utf-8', errors='ignore')

    if result_classic.returncode == 0:
        classic_apps = {line.strip() for line in result_classic.stdout.splitlines() if line.strip()}
        all_apps.update(classic_apps)

    # 3. UWP packages: keep PFN map for launching; add de-camelCased names as a fallback.
    command_modern = r'Get-AppxPackage | ForEach-Object { "$($_.Name)|$($_.PackageFamilyName)" }'
    result_modern = subprocess.run(["powershell", "-Command", command_modern], capture_output=True, text=True, encoding='utf-8', errors='ignore')

    if result_modern.returncode == 0:
        for line in result_modern.stdout.splitlines():
            line = line.strip()
            if '|' in line:
                name, pfn = line.split('|', 1)
                _MODERN_APPS_CACHE[name.lower()] = pfn
                friendly = _decamel(name)
                if friendly:
                    _MODERN_APPS_CACHE[friendly.lower()] = pfn  # launchable via de-camelCased name
                    if friendly.lower() not in _STARTAPPS_CACHE:
                        all_apps.add(friendly)

    full_list = sorted(list(all_apps))
    
    stop_words = [
        'sdk', 'driver', 'redistributable', 'runtime', 'update', 
        'package', 'microsoft .net', 'visual c++', 'prerequisites',
        'manifest', 'host', 'tools', 'amd', 'nvidia', 'intel', 
        'microsoft.windows', 'microsoft.vclibs', 'microsoft.ui',
        'microsoft.web', 'microsoft.aspnet', 'microsoft.testplatform',
        'vs_', 'windows sdk', 'debugger', 'targeting', 'interop'
    ]

    filtered_list = [
        app for app in full_list 
        if not any(stop_word.lower() in app.lower() for stop_word in stop_words)
    ]
    
    _SOFTWARE_CACHE = filtered_list
    _SOFTWARE_CACHE_TIME = time.time()
    return filtered_list


@tool
def get_installed_software():
    """Returns a filtered list of installed software, excluding system components. Takes no arguments."""
    return _get_installed_software()


def _match_score(app: str, term: str):
    """Relevance of `app` to query `term`. Lower tuple sorts first; None => no match."""
    name = app.lower()
    if name == term:
        return (0, len(app))
    if name.startswith(term):
        return (1, len(app))
    if re.search(r'\b' + re.escape(term) + r'\b', name):
        return (2, len(app))
    if term in name:
        return (3, len(app))
    return None


@tool
def find_application_name(approximate_name: str) -> str:
    """
    Finds exact names of installed applications by an approximate query string,
    ranked by relevance (best match first).

    Args:
        approximate_name (str): Approximate name of the application to search for (e.g. "chrome").
    """
    all_apps = _get_installed_software()
    term = approximate_name.strip().lower()

    scored = []
    for app in all_apps:
        score = _match_score(app, term)
        if score is not None:
            scored.append((score, app))

    if not scored:
        return f"Error: Application '{approximate_name}' was not found among installed software."

    scored.sort(key=lambda x: x[0])
    ranked = [app for _, app in scored][:10]
    return "Found matching applications (best match first):\n" + "\n".join(f"- {app}" for app in ranked)


def _get_classic_app_paths():
    global _CLASSIC_APP_PATHS_CACHE, _CLASSIC_APP_PATHS_CACHE_TIME
    if _CLASSIC_APP_PATHS_CACHE is not None and (time.time() - _CLASSIC_APP_PATHS_CACHE_TIME < 300):
        return _CLASSIC_APP_PATHS_CACHE

    app_paths = {}
    registry_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    ]

    for hkey in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
        for path in registry_paths:
            try:
                key = winreg.OpenKey(hkey, path, 0, winreg.KEY_READ)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        display_name, install_location, display_icon = None, None, None
                        try:
                            display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        except OSError:
                            pass
                        try:
                            display_icon = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                        except OSError:
                            pass
                        try:
                            install_location = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                        except OSError:
                            pass
                        
                        executable_path = None
                        if display_icon:
                            executable_path = display_icon.split(',')[0].strip('"')
                        elif install_location:
                            executable_path = install_location.strip('"')

                        if display_name and executable_path:
                            app_paths[display_name.lower()] = executable_path
            except FileNotFoundError:
                pass
    _CLASSIC_APP_PATHS_CACHE = app_paths
    _CLASSIC_APP_PATHS_CACHE_TIME = time.time()
    return app_paths


def _best_key(mapping, term: str):
    """Return the key of `mapping` most relevant to `term` (via _match_score), or None."""
    best, best_score = None, None
    for key in mapping:
        score = _match_score(key, term)
        if score is not None and (best_score is None or score < best_score):
            best, best_score = key, score
    return best


def _start_application_by_name(app_name: str) -> bool:
    term = app_name.strip().lower()

    if not _STARTAPPS_CACHE or not _MODERN_APPS_CACHE:
        _get_installed_software()

    # 1. Start-menu AppID — most reliable for both Win32 shortcuts and UWP apps.
    try:
        key = _best_key(_STARTAPPS_CACHE, term)
        if key:
            app_id = _STARTAPPS_CACHE[key]
            subprocess.Popen(f'explorer.exe shell:AppsFolder\\{app_id}', shell=True)
            time.sleep(2.0)
            return True
    except Exception as e:
        logging.warning(f"Start-menu launch failed for '{app_name}': {e}")

    # 2. Classic executable path from the registry.
    try:
        key = _best_key(_get_classic_app_paths(), term)
        if key:
            subprocess.Popen(shlex.split(f'"{_get_classic_app_paths()[key]}"'))
            time.sleep(2.0)
            return True
    except Exception as e:
        logging.warning(f"Registry-path launch failed for '{app_name}': {e}")

    # 3. UWP package family name fallback.
    try:
        key = _best_key(_MODERN_APPS_CACHE, term)
        if key:
            subprocess.Popen(f'explorer.exe shell:appsFolder\\{_MODERN_APPS_CACHE[key]}!App', shell=True)
            time.sleep(2.0)
            return True
    except Exception as e:
        logging.warning(f"UWP launch failed for '{app_name}': {e}")

    logging.error(f"Could not find/launch application: '{app_name}'")
    return False


@tool
def start_application(app_name: str)->bool:
    """Запускает приложение по его точному названию. Используйте `find_application_name`, чтобы найти его. Возвращает True при успехе."""
    return _start_application_by_name(app_name=app_name)


@tool
def get_open_windows():
    """Возвращает список заголовков всех открытых окон."""
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    
    window_titles = []
    def foreach_window(hwnd, lParam):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLength(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                GetWindowText(hwnd, buff, length + 1)
                title = buff.value
                if title:
                    window_titles.append(title)
        return True
        
    EnumWindows(EnumWindowsProc(foreach_window), 0)
    
    if not window_titles:
        return "не найдено открытых окон"
    
    return "\n".join(window_titles)

def _get_window_by_name(name: str):
    """Вспомогательная функция для получения объекта окна по имени через pywinauto."""
    app_name = name.split(" - ")[-1].strip() if " - " in name else name.strip()
    safe_name = re.escape(app_name)
    main_win_spec = Desktop(backend="win32").window(title_re=f".*{safe_name}.*", found_index=0)
    if not main_win_spec.exists(timeout=0.5):
        raise ElementNotFoundError(f"Окно '{name}' не найдено.")
    return main_win_spec.wrapper_object()

@tool
def scrape_application(name: str) -> str:
    """
    Сканирует окно приложения с помощью визуального нейросетевого парсера OmniParser.
    Делает скриншот окна приложения, детектует активные элементы (кнопки, иконки, поля ввода, текст)
    и присваивает каждому элементу уникальный 'id' для взаимодействия.

    Args:
        name (str): Часть заголовка окна для поиска.
    """
    start_time = time.time()
    global ELEMENTS_CACHE, CURRENT_ID
    ELEMENTS_CACHE.clear()
    CURRENT_ID = 0

    logging.info(f"🔍 [OmniParser] Поиск окна для скрапинга: '{name}'...")

    try:
        app_name = name.split(" - ")[-1].strip() if " - " in name else name.strip()
        safe_name = re.escape(app_name)
        
        main_win_spec = Desktop(backend="win32").window(title_re=f".*{safe_name}.*", found_index=0)
        if not main_win_spec.exists(timeout=0.5):
            main_win_spec = Desktop(backend="uia").window(title_re=f".*{safe_name}.*", found_index=0)
            if not main_win_spec.exists(timeout=0.5):
                elapsed = time.time() - start_time
                logging.info(f"❌ [OmniParser] Окно '{name}' не найдено ({elapsed:.4f} сек).")
                return f"Ошибка: Окно с именем, содержащим '{name}', не найдено."

        main_win = main_win_spec.wrapper_object()

        if not main_win.is_active():
            logging.info(f"↗️ [OmniParser] Фокусировка на окне '{app_name}'...")
            try:
                main_win.set_focus()
            except Exception:
                pass

        rect = main_win.rectangle()
        if rect.width() <= 0 or rect.height() <= 0:
            logging.warning(f"⚠️ [OmniParser] Окно '{app_name}' имеет нулевой размер {rect}.")
            return "Ошибка: Окно имеет нулевой размер или свернуто."

        # Capture window screenshot
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        logging.info(f"📸 [OmniParser] Захват скриншота области окна: X={rect.left}, Y={rect.top}, W={rect.width()}, H={rect.height()}...")
        img = ImageGrab.grab(bbox=bbox)

        # Parse screenshot with OmniParser engine
        logging.info(f"🧠 [OmniParser] Запуск распознавания элементов (YOLO + OCR)...")
        from agent.vision.omniparser_engine import OmniParserEngine
        engine = OmniParserEngine()
        elements = engine.parse_image(img)

        if not elements:
            logging.warning(f"⚠️ [OmniParser] Элементы не найдены на скриншоте '{app_name}'.")
            return "OmniParser не обнаружил интерактивных элементов на скриншоте окна."

        xml_lines = ["<WindowVision>"]
        for elem in elements:
            elem_id = CURRENT_ID
            # Map window-relative coordinates to absolute screen coordinates
            abs_left = rect.left + elem["left"]
            abs_top = rect.top + elem["top"]
            abs_right = rect.left + elem["right"]
            abs_bottom = rect.top + elem["bottom"]

            ELEMENTS_CACHE[elem_id] = {
                "left": abs_left, "top": abs_top,
                "right": abs_right, "bottom": abs_bottom,
                "name": elem["name"],
                "control_type": elem["control_type"]
            }
            CURRENT_ID += 1

            safe_name = elem["name"].replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
            tag = elem["control_type"].replace("/", "_")
            # Include spatial coordinates hint so LLM understands layout (row/column position)
            rel_cx = (elem["left"] + elem["right"]) // 2
            rel_cy = (elem["top"] + elem["bottom"]) // 2
            xml_lines.append(f'  <{tag} id="{elem_id}" name="{safe_name}" pos="x:{rel_cx},y:{rel_cy}" />')

        xml_lines.append("</WindowVision>")
        xml_output = "\n".join(xml_lines)

        elapsed = time.time() - start_time
        logging.info(f"⏱️ [OmniParser] Окно '{name}' успешно распознано за {elapsed:.4f} сек. Найдено {CURRENT_ID} элементов!")
        return xml_output

    except ElementNotFoundError:
        elapsed = time.time() - start_time
        logging.info(f"❌ [OmniParser] Окно '{name}' не найдено ({elapsed:.4f} сек).")
        return f"Произошла ошибка: Окно с именем '{name}' не найдено после ожидания."
    except Exception as e:
        logging.error(f"💥 [OmniParser] Ошибка при сканировании окна '{name}': {e}", exc_info=True)
        return f"Произошла ошибка скрапинга OmniParser: {e}"
    

@tool
def interact_with_element_by_id(
    name: str,
    element_id: int = -1,
    action: str = None,
    text_to_set: Optional[str] = None
) -> Union[str, Any]:
    """
    Находит UI-элемент по его точным координатам в указанном окне и выполняет над ним определённое действие.

    Args:
        name (str): Часть заголовка окна приложения для поиска. Например, 'Mozilla Firefox' или 'Калькулятор'.
        element_id (int): ID целевого элемента (число из атрибута id="..." в выводе `scrape_application`). Передавай именно параметр `element_id`.
        action (str): Действие, которое необходимо выполнить над элементом. Поддерживаемые действия:
                      - Клики: 'click', 'double_click', 'right_click'.
                      - Работа с текстом: 'set_text', 'get_text', 'press_enter'.
                      - Прокрутка: 'scroll_up', 'scroll_down', 'scroll_left', 'scroll_right'.
                      - Масштабирование (применяется ко всему окну): 'zoom_in', 'zoom_out'.
        text_to_set (Optional[str]): Текстовая строка для ввода. Является обязательным аргументом только для действия 'set_text'.

    Returns:
        Union[str, Any]:
            - В случае успеха для большинства действий — строка с сообщением об успехе (например, "Действие 'click' успешно выполнено.").
            - Для действия 'get_text' — текст, содержащийся в элементе.
            - В случае ошибки — строка с описанием ошибки (например, "Ошибка: Элемент с координатами ... не найден.").
    """
    try:
        main_win = _get_window_by_name(name)
        if element_id not in ELEMENTS_CACHE and 'zoom' not in action and action not in ["type_text_blind", "press_enter"]:
            return f"Ошибка: Элемент с id {element_id} не найден в кэше."

        if element_id in ELEMENTS_CACHE:
            el_data = ELEMENTS_CACHE[element_id]
            center_x = (el_data['left'] + el_data['right']) // 2
            center_y = (el_data['top'] + el_data['bottom']) // 2
        else:
            center_x, center_y = 0, 0

        action = action.lower()
        if action == 'click':
            pyautogui.click(center_x, center_y)
        elif action == 'double_click':
            pyautogui.doubleClick(center_x, center_y)
        elif action == 'right_click':
            pyautogui.rightClick(center_x, center_y)
        
        elif action == 'set_text':
            if text_to_set is None:
                return "Ошибка: для действия 'set_text' необходимо передать аргумент 'text_to_set'."
            
            # 1. Кликаем по элементу — это выведет окно на передний план
            pyautogui.click(center_x, center_y)
            time.sleep(0.15)

            # 2. Стираем старое содержимое
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.press('delete')
            time.sleep(0.1)

            # 3. Вводим текст через буфер обмена для поддержки любых языков
            _type_unicode_text(text_to_set)
            
            return f"Действие '{action}' успешно выполнено."
            
        elif action == "type_text_blind":
            if not text_to_set:
                return "Ошибка: нужен text_to_set."

            main_win.set_focus()
            _type_unicode_text(text_to_set)
            
        elif action == 'press_enter':
            # Вместо клика по элементу, жестко активируем само окно
            main_win.set_focus()
            time.sleep(0.1)
            
            # Вариант А: Отправляем Enter через встроенный синтаксис pywinauto
            main_win.type_keys('~')
            
        elif action == 'get_text':
            text = ELEMENTS_CACHE.get(element_id, {}).get("name", "") or ""
            # "Icon_N" — это ярлык-заглушка детектора иконок, а не распознанный текст.
            if not text.strip() or re.fullmatch(r"Icon_\d+", text.strip()):
                return (f"У элемента id={element_id} нет распознанного текста. "
                        f"Читай значения прямо из результата scrape_application.")
            return text

        elif action == 'scroll_up':
            pyautogui.moveTo(center_x, center_y)
            pyautogui.scroll(500)
        elif action == 'scroll_down':
            pyautogui.moveTo(center_x, center_y)
            pyautogui.scroll(-500)
        elif action == 'scroll_left':
            pyautogui.moveTo(center_x, center_y)
            pyautogui.hscroll(-500)
        elif action == 'scroll_right':
            pyautogui.moveTo(center_x, center_y)
            pyautogui.hscroll(500)

        elif action == 'zoom_in':
            main_win.type_keys('^{PLUS}')
        elif action == 'zoom_out':
            main_win.type_keys('^{MINUS}')
            
        else:
            return f"Ошибка: Неизвестное действие '{action}'."
        
        return f"Действие '{action}' успешно выполнено."

    except ElementNotFoundError:
        return f"Ошибка: Окно с именем '{name}' не найдено."
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {type(e).__name__}: {e}"
    

@tool
def simulate_keyboard(name: str, keys: str) -> str:
    """
    Симулирует нажатие клавиш клавиатуры или ввод текста в указанном окне.
    Окно сначала выводится на передний план.

    Args:
        name (str): Часть заголовка окна, в которое нужно отправить нажатия.
        keys (str): Строка для ввода или специальная клавиша/комбинация. 
                    Примеры: 'enter', 'tab', 'esc', 'ctrl+c', 'alt+tab', 'hello world'.
                    Поддерживаются имена клавиш из библиотеки pyautogui.
    """
    try:
        main_win = _get_window_by_name(name)
        main_win.set_focus()
        time.sleep(0.4)  # Даем окну время реально получить фокус (0.2 не хватало — терялся первый символ)

        _MODIFIERS = {
            'ctrl', 'control', 'alt', 'altleft', 'altright', 'shift', 'shiftleft',
            'shiftright', 'win', 'winleft', 'winright', 'command', 'cmd', 'option',
            'optionleft', 'optionright', 'fn', 'super', 'meta',
        }

        # Горячая клавиша — только если части разделены '+', каждая является
        # реальной клавишей и хотя бы одна из НЕ последних является модификатором.
        # Иначе '1+1=' и подобное — это обычный ввод текста, а не 'ctrl+c'.
        parts = [k.strip().lower() for k in keys.split('+')]
        is_hotkey = (
            '+' in keys
            and len(parts) >= 2
            and all(p in pyautogui.KEYBOARD_KEYS for p in parts)
            and any(p in _MODIFIERS for p in parts[:-1])
        )

        if is_hotkey:
            pyautogui.hotkey(*parts)
            return f"Выполнено нажатие комбинации клавиш: {keys}"

        # Проверяем, является ли это одиночной специальной клавишей
        elif keys.lower() in pyautogui.KEYBOARD_KEYS:
            pyautogui.press(keys.lower())
            return f"Выполнено нажатие специальной клавиши: {keys}"

        # Короткая ASCII-строка (например '1+1=') — шлём реальные нажатия клавиш.
        # Так ввод доходит до приложений, которые игнорируют вставку из буфера
        # (в частности UWP-Калькулятор не реагирует на Shift+Insert).
        elif keys.isascii() and '\n' not in keys and len(keys) <= 30:
            pyautogui.press('shift')   # «разбудить» ввод в окне, чтобы не потерять первый символ
            time.sleep(0.1)
            pyautogui.write(keys, interval=0.05)
            return f"Выполнен ввод символов: {keys}"

        # Иначе (длинный или не-ASCII текст) — быстрый и надёжный путь через буфер.
        else:
            _type_unicode_text(keys)
            return f"Выполнен ввод текста: {keys}"

    except ElementNotFoundError:
        return f"Ошибка: Окно с именем '{name}' не найдено."
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {e}"

@tool
def execute_bash_command(command: str) -> str:
    """
    Executes a shell command in PowerShell and returns its output.

    Args:
        command (str): PowerShell or shell command to execute.
    """
    timeout_seconds = 10
    
    try:
        full_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        result = subprocess.run(
            full_cmd, 
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace"
        )

        if result.returncode != 0:
            return f"Ошибка выполнения команды:\nСтатус код: {result.returncode}\nStderr: {result.stderr}"
        
        if not result.stdout.strip():
            return "Команда выполнена успешно, но не произвела вывода (stdout)."

        return f"Результат выполнения:\n{result.stdout}"

    except FileNotFoundError:
        return f"Ошибка: команда '{command.split()[0]}' не найдена. Убедись, что она установлена и доступна в PATH."
    except subprocess.TimeoutExpired:
        return f"Ошибка: выполнение команды превысило тайм-аут в {timeout_seconds} секунд."
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {str(e)}"