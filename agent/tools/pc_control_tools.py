import subprocess
import winreg
import shlex
import psutil
import time
import ctypes
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
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

import pyautogui
import pyperclip

# По умолчанию pyautogui спит 0.1с после КАЖДОГО вызова (press/click/moveTo...).
pyautogui.PAUSE = 0.02

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

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        # MOUSEINPUT нужен только ради правильного размера union (иначе SendInput отвергает структуру).
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send_unicode_char(code_unit: int):
    inputs = (_INPUT * 2)()
    for i, flags in enumerate((_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)):
        inputs[i].type = _INPUT_KEYBOARD
        inputs[i].ki = _KEYBDINPUT(0, code_unit, flags, 0, 0)
    ctypes.windll.user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))


def _type_text_layout_independent(text: str, interval: float = 0.05):
    """Печатает текст реальными нажатиями через SendInput (Unicode-пакеты).
    В отличие от pyautogui.write не зависит от раскладки: при русской раскладке
    pyautogui молча пропускает латинские буквы (VkKeyScan не находит их), и в поле
    остаются только символы, общие для обеих раскладок, например '-'."""
    for ch in text:
        raw = ch.encode('utf-16-le')
        for i in range(0, len(raw), 2):  # символы вне BMP уходят суррогатной парой
            _send_unicode_char(int.from_bytes(raw[i:i + 2], 'little'))
        time.sleep(interval)


def _decamel(text: str) -> str:
    """`Microsoft.WindowsCalculator` -> `Windows Calculator` (last dotted segment, split on caps)."""
    segment = text.split('.')[-1]
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', segment)
    spaced = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', spaced)
    return spaced.strip()


_SOFTWARE_CACHE_TTL = 1800  # установленные программы меняются редко
_SOFTWARE_CACHE_LOCK = threading.Lock()


def _run_powershell(command: str):
    return subprocess.run(["powershell", "-NoProfile", "-Command", command],
                          capture_output=True, text=True, encoding='utf-8', errors='ignore')


def warm_up_app_cache():
    """Заполняет кэш установленных приложений заранее (зовётся при старте системы),
    чтобы первый find_application_name не ждал PowerShell."""
    t0 = time.time()
    _get_installed_software()
    logging.info(f"Кэш приложений прогрет за {time.time() - t0:.2f}с.")


def _get_installed_software():
    # Лок: фоновый прогрев и первый реальный вызов не должны гонять PowerShell дважды.
    with _SOFTWARE_CACHE_LOCK:
        return _get_installed_software_locked()


def _get_installed_software_locked():
    global _SOFTWARE_CACHE, _SOFTWARE_CACHE_TIME, _MODERN_APPS_CACHE, _STARTAPPS_CACHE
    if _SOFTWARE_CACHE is not None and (time.time() - _SOFTWARE_CACHE_TIME < _SOFTWARE_CACHE_TTL):
        return _SOFTWARE_CACHE

    all_apps = set()

    command_startapps = r'Get-StartApps | ForEach-Object { "$($_.Name)|$($_.AppID)" }'
    command_classic = r'''
    Get-ItemProperty HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*,
                     HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*,
                     HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* |
    Where-Object {$_.PSObject.Properties['DisplayName'] -and $_.DisplayName -ne $null} |
    Select-Object -ExpandProperty DisplayName
    '''
    command_modern = r'Get-AppxPackage | ForEach-Object { "$($_.Name)|$($_.PackageFamilyName)" }'

    # Три независимых PowerShell-запроса — параллельно, а не друг за другом.
    with ThreadPoolExecutor(max_workers=3) as pool:
        result_startapps, result_classic, result_modern = pool.map(
            _run_powershell, (command_startapps, command_classic, command_modern))

    # 1. Start-menu apps: friendly names + launchable AppIDs (Win32 shortcuts + UWP).
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
    if result_classic.returncode == 0:
        classic_apps = {line.strip() for line in result_classic.stdout.splitlines() if line.strip()}
        all_apps.update(classic_apps)

    # 3. UWP packages: keep PFN map for launching; add de-camelCased names as a fallback.
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


def _launch_application(app_name: str) -> bool:
    """Отправляет команду запуска. True — если нашёлся способ запустить."""
    term = app_name.strip().lower()

    if not _STARTAPPS_CACHE or not _MODERN_APPS_CACHE:
        _get_installed_software()

    # 1. Start-menu AppID — most reliable for both Win32 shortcuts and UWP apps.
    try:
        key = _best_key(_STARTAPPS_CACHE, term)
        if key:
            app_id = _STARTAPPS_CACHE[key]
            subprocess.Popen(f'explorer.exe shell:AppsFolder\\{app_id}', shell=True)
            return True
    except Exception as e:
        logging.warning(f"Start-menu launch failed for '{app_name}': {e}")

    # 2. Classic executable path from the registry.
    try:
        key = _best_key(_get_classic_app_paths(), term)
        if key:
            subprocess.Popen(shlex.split(f'"{_get_classic_app_paths()[key]}"'))
            return True
    except Exception as e:
        logging.warning(f"Registry-path launch failed for '{app_name}': {e}")

    # 3. UWP package family name fallback.
    try:
        key = _best_key(_MODERN_APPS_CACHE, term)
        if key:
            subprocess.Popen(f'explorer.exe shell:appsFolder\\{_MODERN_APPS_CACHE[key]}!App', shell=True)
            return True
    except Exception as e:
        logging.warning(f"UWP launch failed for '{app_name}': {e}")

    return False


def _wait_for_app_window(before: set, fg_before, timeout: float = 8.0):
    """Ждёт окно запущенного приложения вместо слепого sleep. Сигналы: появилось новое
    видимое окно с заголовком, либо на передний план вышло другое окно (single-instance
    приложение вроде Spotify активирует уже открытое). Возвращает заголовок или None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.15)
        new = [hwnd for hwnd, _ in _visible_windows() if hwnd not in before]
        fg = _user32.GetForegroundWindow()
        hwnd = new[0] if new else (fg if fg and fg != fg_before and _window_title(fg) else None)
        if hwnd:
            time.sleep(0.3)  # дать окну дорисоваться и выставить финальный заголовок
            return _window_title(hwnd) or None
    return None


def _start_application_by_name(app_name: str) -> str:
    before = {hwnd for hwnd, _ in _visible_windows()}
    fg_before = _user32.GetForegroundWindow()

    if not _launch_application(app_name):
        logging.error(f"Could not find/launch application: '{app_name}'")
        return f"Ошибка: не удалось найти способ запустить приложение '{app_name}'."

    t0 = time.time()
    title = _wait_for_app_window(before, fg_before)
    logging.info(f"start_application '{app_name}': окно {title!r} за {time.time() - t0:.2f}с")
    if title:
        return f"Приложение запущено. Заголовок окна: '{title}'"
    return (f"Команда запуска '{app_name}' отправлена, но окно не появилось за 8 с. "
            f"Проверь get_open_windows.")


@tool
def start_application(app_name: str) -> str:
    """Запускает приложение по его точному названию (найди его через `find_application_name`).
    Ждёт появления окна и возвращает его точный заголовок — используй его как win_name
    для делегирования, отдельный get_open_windows после запуска не нужен."""
    return _start_application_by_name(app_name=app_name)


_user32 = ctypes.windll.user32
_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
_HWND_CACHE = {}  # requested window name -> hwnd


def _window_title(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value


def _visible_windows():
    """[(hwnd, title)] видимых окон верхнего уровня с заголовком, в z-порядке (верхнее первым)."""
    out = []

    def foreach_window(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if title:
                out.append((hwnd, title))
        return True

    _user32.EnumWindows(_EnumWindowsProc(foreach_window), 0)
    return out


_EXE_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]+\.exe\]\s*$", re.IGNORECASE)


def _process_name(hwnd) -> str:
    """Имя exe-файла процесса, которому принадлежит окно (например 'Spotify.exe')."""
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name()
    except Exception:
        return ""


@tool
def get_open_windows():
    """Возвращает список открытых окон в формате `Заголовок  [процесс.exe]`.
    Процесс помогает узнать приложение, у которого заголовок меняется (например, Spotify
    во время воспроизведения показывает в заголовке играющий трек).
    В win_name передавай только заголовок, без `[процесс.exe]`."""
    lines = []
    for hwnd, title in _visible_windows():
        exe = _process_name(hwnd)
        lines.append(f"{title}  [{exe}]" if exe else title)
    if not lines:
        return "не найдено открытых окон"
    return "\n".join(lines)


def _find_hwnd(name: str):
    """HWND окна по имени: точное совпадение заголовка, иначе первое (верхнее) окно,
    чей заголовок содержит имя приложения, иначе окно процесса с таким именем
    ('Spotify' -> Spotify.exe). Прямой EnumWindows — ~1 мс против ~2 с у pywinauto
    `title_re`, который на каждый вызов дважды перебирает все окна."""
    name = _EXE_SUFFIX_RE.sub("", name).strip()  # на случай, если передали строку из get_open_windows
    app_name = name.split(" - ")[-1].strip() if " - " in name else name.strip()

    # Однажды найденное окно держим по HWND, пока оно живо, даже если заголовок
    # сменился: Spotify переименовывает окно по треку при каждом play/pause.
    cached = _HWND_CACHE.get(name)
    if cached and _user32.IsWindow(cached) and _user32.IsWindowVisible(cached):
        return cached

    windows = _visible_windows()
    found = None
    for hwnd, title in windows:
        if title == name:
            found = hwnd
            break
        if found is None and app_name in title:
            found = hwnd

    if found is None:
        key = app_name.lower()
        for hwnd, _ in windows:
            exe_base = _process_name(hwnd).lower().removesuffix(".exe")
            if exe_base and (exe_base == key or exe_base in key.split()):
                found = hwnd
                break

    if found:
        _HWND_CACHE[name] = found
    return found


def _get_window_by_name(name: str, timeout: float = 0.5):
    """Вспомогательная функция для получения объекта окна по имени через pywinauto."""
    deadline = time.time() + timeout
    while True:
        hwnd = _find_hwnd(name)
        if hwnd:
            return Desktop(backend="win32").window(handle=hwnd).wrapper_object()
        if time.time() >= deadline:
            raise ElementNotFoundError(f"Окно '{name}' не найдено.")
        time.sleep(0.1)


def _focus_window(win, settle: float = 0.3) -> bool:
    """Выводит окно на передний план. Пауза `settle` — только если фокус реально
    пришлось переключать. Возвращает True, если фокус переключался."""
    try:
        if _user32.GetForegroundWindow() == win.handle:
            return False
        win.set_focus()
    except Exception:
        return False
    time.sleep(settle)
    return True

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
        # 0.4с — если фокус переключался (0.2 не хватало — терялся первый символ)
        _focus_window(main_win, settle=0.4)

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
            _type_text_layout_independent(keys, interval=0.05)
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