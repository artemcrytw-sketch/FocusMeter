# FocusMeter 3.2 (+ Wrapped) — трекер экранного времени для Windows
# Живёт в трее и считает время в фоне; окно открывается по клику и полностью выгружается при закрытии.
# Нужны: Python 3.10+, pystray, pillow (.bat ставит сам).
import os, sys, time, csv, re, queue, sqlite3, ctypes, threading, hashlib, math, traceback
from ctypes import wintypes
from datetime import date, datetime, timedelta
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, filedialog
try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FocusMeter")
DB_PATH = os.path.join(APP_DIR, "usage.db")
ICON_DIR = os.path.join(APP_DIR, "icons")
LOG_PATH = os.path.join(APP_DIR, "error.log")
POLL = 2                 # как часто смотрим на активное окно (сек)
IDLE_LIMIT = 60          # без мыши/клавиатуры дольше — не считаем
BREAK_RESET = 120        # простой дольше — это настоящий перерыв
FLUSH_EVERY = 30         # запись в базу раз в N секунд
MIN_APP_SEC = 60         # приложения короче минуты в списках не показываем
DEFAULT_GOAL = 6.0
ICONFONT = "Segoe MDL2 Assets"
GRACE_SEC = 120          # после исчерпания лимита даём время сохраниться
WARN_BEFORE = 900        # предупреждение за 15 минут
HARD_AFTER = 15          # через сколько после мягкого закрытия завершаем принудительно
PROTECTED = {"explorer", "svchost", "csrss", "winlogon", "dwm", "lsass", "services", "wininit", "smss",
             "system", "sihost", "ctfmon", "fontdrvhost", "searchhost", "startmenuexperiencehost",
             "shellexperiencehost", "applicationframehost", "python", "pythonw", "py", "conhost"}

def log(msg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ------------------------------------------------------------------ Win32
user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
gdi32, shell32 = ctypes.windll.gdi32, ctypes.windll.shell32

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]
class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", wintypes.LPVOID)]
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260), ("szTypeName", wintypes.WCHAR * 80)]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                                   ctypes.POINTER(wintypes.HICON), wintypes.UINT]
shell32.ExtractIconExW.restype = wintypes.UINT
shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW),
                                   wintypes.UINT, wintypes.UINT]
shell32.SHGetFileInfoW.restype = ctypes.c_size_t
gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]


def get_idle_seconds():
    info = LASTINPUTINFO(); info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0



def get_foreground():
    hwnd = user32.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


def scan_running():
    """Все видимые окна сейчас -> {имя приложения: путь к exe}"""
    found = {}
    def cb(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd):
                info = window_info(hwnd)
                if info:
                    found.setdefault(friendly_name(info[0]), info[0])
        except Exception:
            pass
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found

def is_protected(path):
    """Системные процессы и сам трекер трогать нельзя"""
    path = path or ""
    name = os.path.splitext(os.path.basename(path))[0].lower()
    win = os.environ.get("WINDIR", r"C:\Windows").lower()
    return name in PROTECTED or path.lower().startswith(win + "\\")


def proc_path(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(1024)
        p = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, p, ctypes.byref(size)) and p.value:
            return p.value
    finally:
        kernel32.CloseHandle(h)
    return None

user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]


def resolve_hwnd(hwnd):
    """-> (pid, путь к exe). Для магазинных приложений подменяет ApplicationFrameHost на реальный процесс"""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    host = pid.value
    path = proc_path(host)
    if path and os.path.basename(path).lower() == "applicationframehost.exe":
        found = []
        def cb(ch, _):
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(ch, ctypes.byref(p))
            if p.value and p.value != host:
                found.append(p.value)
                return False
            return True
        user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        real = proc_path(found[0]) if found else None
        if real:
            return found[0], real
    return (host, path) if path else (None, None)


def window_info(hwnd):
    """-> (путь к exe, заголовок) или None"""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(n + 1, 2))
    user32.GetWindowTextW(hwnd, buf, len(buf))
    title = buf.value.strip()
    if not title:
        return None
    pid, path = resolve_hwnd(hwnd)
    if not pid or pid == os.getpid():
        return None
    return path, title[:180]


def app_windows(app):
    """Все видимые окна приложения -> [(hwnd, pid, путь)]"""
    res = []
    def cb(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd):
                pid, path = resolve_hwnd(hwnd)
                if path and pid != os.getpid() and friendly_name(path) == app:
                    res.append((hwnd, pid, path))
        except Exception:
            pass
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return res



def terminate_pid(pid):
    h = kernel32.OpenProcess(0x1001, False, pid)      # TERMINATE | QUERY_LIMITED
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
            return False                              # уже закрыт
        return bool(kernel32.TerminateProcess(h, 1))
    finally:
        kernel32.CloseHandle(h)


def is_protected(path):
    """Системные процессы и сам трекер трогать нельзя"""
    path = path or ""
    name = os.path.splitext(os.path.basename(path))[0].lower()
    win = os.environ.get("WINDIR", r"C:\Windows").lower()
    return name in PROTECTED or path.lower().startswith(win + "\\")

try:
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.LoadImageW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.SetClassLongPtrW.restype = ctypes.c_void_p
except Exception:
    pass


def set_window_icon(win, ico_path):
    """Иконка окна напрямую через Win32 (WM_SETICON): меняет и значок на панели задач"""
    try:
        win.update_idletasks()
        child = win.winfo_id()
        hwnds = {child, user32.GetParent(child) or child}
        big = user32.LoadImageW(None, ico_path, 1, user32.GetSystemMetrics(11), user32.GetSystemMetrics(11), 0x10)
        small = user32.LoadImageW(None, ico_path, 1, user32.GetSystemMetrics(49), user32.GetSystemMetrics(49), 0x10)
        if not big and not small:
            log("icon: Windows не смог прочитать " + str(ico_path))
            return
        for hwnd in hwnds:
            if small:
                user32.SendMessageW(hwnd, 0x0080, 0, small)      # WM_SETICON, ICON_SMALL
            if big:
                user32.SendMessageW(hwnd, 0x0080, 1, big)        # WM_SETICON, ICON_BIG
            try:
                if big: user32.SetClassLongPtrW(hwnd, -14, big)  # GCLP_HICON
                if small: user32.SetClassLongPtrW(hwnd, -34, small)  # GCLP_HICONSM
            except Exception:
                pass
    except Exception:
        log("set_window_icon: " + traceback.format_exc())


def hicon_to_image(h):
    ii = ICONINFO()
    if not user32.GetIconInfo(h, ctypes.byref(ii)) or not ii.hbmColor:
        return None
    try:
        bmp = BITMAP()
        gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))
        w, ht = bmp.bmWidth, bmp.bmHeight
        if w <= 0 or ht <= 0:
            return None

        def dib(hbm):
            bi = BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(bi); bi.biWidth = w; bi.biHeight = -ht
            bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * ht * 4)
            hdc = user32.GetDC(None)
            try:
                gdi32.GetDIBits(hdc, hbm, 0, ht, buf, ctypes.byref(bi), 0)
            finally:
                user32.ReleaseDC(None, hdc)
            return buf.raw

        img = Image.frombuffer("RGBA", (w, ht), dib(ii.hbmColor), "raw", "BGRA", 0, 1)
        if img.getchannel("A").getextrema()[1] == 0 and ii.hbmMask:      # иконка без альфа-канала
            mask = Image.frombuffer("RGBA", (w, ht), dib(ii.hbmMask), "raw", "BGRA", 0, 1).convert("L")
            img.putalpha(mask.point(lambda v: 0 if v > 127 else 255))
        if img.getchannel("A").getextrema()[1] == 0:
            return None
        return img
    finally:
        gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)


def extract_icon(path):
    """Иконка exe -> PIL.Image (RGBA) или None. Два способа: ExtractIconEx и SHGetFileInfo."""
    large, small = wintypes.HICON(), wintypes.HICON()
    try:
        shell32.ExtractIconExW(path, 0, ctypes.byref(large), ctypes.byref(small), 1)
        for h in (large.value, small.value):
            if h:
                img = hicon_to_image(h)
                if img:
                    return img
    except Exception:
        log("ExtractIconEx: " + traceback.format_exc())
    finally:
        if large.value: user32.DestroyIcon(large.value)
        if small.value: user32.DestroyIcon(small.value)
    try:
        info = SHFILEINFOW()
        shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info), 0x100)  # SHGFI_ICON
        if info.hIcon:
            try:
                return hicon_to_image(info.hIcon)
            finally:
                user32.DestroyIcon(info.hIcon)
    except Exception:
        log("SHGetFileInfo: " + traceback.format_exc())
    return None


def icon_file(app):
    return os.path.join(ICON_DIR, hashlib.md5(app.encode()).hexdigest() + ".png")


def ensure_icon(app, exe):
    fn = icon_file(app)
    if os.path.exists(fn) or not exe:
        return
    img = extract_icon(exe)
    if img:
        img.save(fn)
    else:
        log(f"icon: не удалось достать иконку для {app} ({exe})")


# ------------------------------------------------------------------ Категории
CAT_LIST = ["Работа и учёба", "Общение", "Развлечения", "Игры", "Другое"]
BROWSERS = {"Google Chrome", "Microsoft Edge", "Mozilla Firefox", "Opera", "Brave", "Vivaldi", "Яндекс Браузер"}
KNOWN = {
    "chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Mozilla Firefox",
    "opera": "Opera", "brave": "Brave", "vivaldi": "Vivaldi", "browser": "Яндекс Браузер",
    "discord": "Discord", "code": "Visual Studio Code", "devenv": "Visual Studio",
    "explorer": "Проводник", "telegram": "Telegram", "steam": "Steam", "spotify": "Spotify",
    "notepad": "Блокнот", "obsidian": "Obsidian", "slack": "Slack", "zoom": "Zoom",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "photoshop": "Photoshop",
    "figma": "Figma", "vlc": "VLC", "obs64": "OBS", "taskmgr": "Диспетчер задач",
    "riotclientservices": "Riot Client", "leagueclient": "League of Legends",
    "pycharm64": "PyCharm", "windowsterminal": "Терминал", "whatsapp": "WhatsApp",
    "notion": "Notion", "onenote": "OneNote", "teams": "Teams", "anki": "Anki",    "calculatorapp": "Калькулятор", "snippingtool": "Ножницы", "mspaint": "Paint",
}
W_, C_, E_, G_ = CAT_LIST[0], CAT_LIST[1], CAT_LIST[2], CAT_LIST[3]
DEFAULT_CAT = {
    "Visual Studio Code": W_, "Visual Studio": W_, "Word": W_, "Excel": W_, "PowerPoint": W_,
    "Obsidian": W_, "Notion": W_, "OneNote": W_, "Figma": W_, "Photoshop": W_, "Zoom": W_,
    "Slack": W_, "Teams": W_, "PyCharm": W_, "Терминал": W_, "Anki": W_, "Блокнот": W_,
    "Telegram": C_, "Discord": C_, "WhatsApp": C_, "Spotify": E_, "VLC": E_,
    "Steam": G_, "Riot Client": G_, "League of Legends": G_,
}
WORK_KW = ("github", "stack overflow", "docs", "notion", "coursera", "udemy", "leetcode", "claude",
           "chatgpt", "gmail", "moodle", "wikipedia", "scholar", "figma", "canva", "kaggle",
           "habr", "хабр", "лекци", "курс", "универ", "учеб")
ENT_KW = ("youtube", "netflix", "twitch", "кинопоиск", "kinopoisk", "rutube", "tiktok", "anime")
CHAT_KW = ("instagram", "вконтакте", "vk.com", "facebook", "twitter", "reddit", "telegram",
           "whatsapp", "linkedin", "x.com")


def friendly_name(path):
    n = os.path.splitext(os.path.basename(path))[0]
    return KNOWN.get(n.lower(), n[:1].upper() + n[1:])


BROWSER_TAIL = ("chrome", "edge", "firefox", "opera", "brave", "vivaldi", "яндекс браузер", "яндекс.браузер", "yandex")
SITES = [  # (слово в заголовке вкладки, название сайта) — можно дописывать свои
    ("youtube", "YouTube"), ("github", "GitHub"), ("stack overflow", "Stack Overflow"),
    ("telegram", "Telegram Web"), ("вконтакте", "ВКонтакте"), ("vk.com", "ВКонтакте"),
    ("instagram", "Instagram"), ("facebook", "Facebook"), ("twitter", "X / Twitter"), ("reddit", "Reddit"),
    ("twitch", "Twitch"), ("netflix", "Netflix"), ("кинопоиск", "Кинопоиск"), ("kinopoisk", "Кинопоиск"),
    ("rutube", "RuTube"), ("tiktok", "TikTok"), ("linkedin", "LinkedIn"), ("pinterest", "Pinterest"),
    ("claude", "Claude"), ("chatgpt", "ChatGPT"), ("gmail", "Gmail"), ("google docs", "Google Docs"),
    ("google таблицы", "Google Таблицы"), ("google sheets", "Google Таблицы"), ("google поиск", "Google Поиск"),
    ("google search", "Google Поиск"), ("wikipedia", "Википедия"), ("википедия", "Википедия"),
    ("habr", "Хабр"), ("хабр", "Хабр"), ("notion", "Notion"), ("figma", "Figma"), ("canva", "Canva"),
    ("kaggle", "Kaggle"), ("leetcode", "LeetCode"), ("coursera", "Coursera"), ("udemy", "Udemy"),
    ("moodle", "Moodle"), ("ozon", "Ozon"), ("wildberries", "Wildberries"), ("avito", "Авито"),
    ("aliexpress", "AliExpress"), ("booking", "Booking"), ("aviasales", "Авиасейлс"), ("яндекс", "Яндекс (поиск)"),
]


MIN_SITE_SEC = 300          # сайты короче 5 минут в списке не показываем
EXTRA_SITES = [             # конкретные сервисы проверяются раньше общих слов (можно дописывать свои)
    ("яндекс музыка", "Яндекс Музыка"), ("яндекс.музыка", "Яндекс Музыка"), ("яндекс почта", "Яндекс Почта"),
    ("яндекс диск", "Яндекс Диск"), ("яндекс карты", "Яндекс Карты"), ("яндекс переводчик", "Яндекс Переводчик"),
    ("яндекс практикум", "Яндекс Практикум"), ("яндекс маркет", "Яндекс Маркет"), ("яндекс учебник", "Яндекс Учебник"),
    ("алиса", "Алиса"), ("дзен", "Дзен"), ("пикабу", "Пикабу"), ("pikabu", "Пикабу"), ("hh.ru", "hh.ru"),
    ("headhunter", "hh.ru"), ("госуслуги", "Госуслуги"), ("сбербанк", "Сбер"), ("тинькофф", "Т-Банк"),
    ("т-банк", "Т-Банк"), ("lamoda", "Lamoda"), ("ситилинк", "Ситилинк"), ("citilink", "Ситилинк"),
    ("vc.ru", "VC.ru"), ("dtf", "DTF"), ("stepik", "Stepik"), ("skillbox", "Skillbox"),
    ("geekbrains", "GeekBrains"), ("duolingo", "Duolingo"), ("gemini", "Gemini"), ("deepseek", "DeepSeek"),
    ("perplexity", "Perplexity"), ("whatsapp", "WhatsApp Web"), ("google диск", "Google Диск"),
    ("google drive", "Google Диск"), ("google переводчик", "Google Переводчик"), ("google translate", "Google Переводчик"),
]
ALL_SITES = EXTRA_SITES + SITES


def title_parts(title):
    parts = [p.strip() for p in re.split(r"\s[-–—|·]\s", title) if p.strip()]
    if parts and any(b in parts[-1].lower() for b in BROWSER_TAIL):
        parts = parts[:-1]                  # убираем «— Яндекс Браузер» и т.п.
    return parts


def site_of(title, learned=None):
    """Сайт по заголовку вкладки. URL и содержимое страниц не читаются.
    learned — названия сайтов, которые программа сама заметила в хвостах твоих заголовков."""
    parts = title_parts(title)
    body = " ".join(parts).lower()
    for kw, name in ALL_SITES:
        if kw in body:
            return name
    if len(parts) >= 2:                     # многие сайты дописывают своё имя в конце заголовка
        tail = parts[-1]
        if (learned is None and len(tail) <= 20) or (learned is not None and tail in learned):
            return tail
    return "Другие страницы"


# ------------------------------------------------------------------ Хранилище
class Store:
    def __init__(self):
        os.makedirs(ICON_DIR, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, app, title))""")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS apps(app TEXT PRIMARY KEY, exe TEXT, category TEXT)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS hourly_usage(
            day TEXT NOT NULL, hour INTEGER NOT NULL, app TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, hour, app))""")
        self.db.execute("CREATE TABLE IF NOT EXISTS blacklist(app TEXT PRIMARY KEY, limit_min INTEGER NOT NULL)")        
        self.db.commit()
        self.buf, self.hbuf = {}, {}
        self.cache = dict(self.db.execute("SELECT key, value FROM settings").fetchall())
        self.over = dict(self.db.execute("SELECT app, category FROM apps WHERE category IS NOT NULL").fetchall())
        self.exe = dict(self.db.execute("SELECT app, exe FROM apps WHERE exe IS NOT NULL").fetchall())
        self.bl = dict(self.db.execute("SELECT app, limit_min FROM blacklist").fetchall())

    def get(self, key, default):
        return self.cache.get(key, default)

    def set(self, key, value):
        with self.lock:
            self.cache[key] = str(value)
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.db.commit()

    def goal_sec(self):
        try:
            return float(self.get("goal_hours", DEFAULT_GOAL)) * 3600
        except ValueError:
            return DEFAULT_GOAL * 3600

    def add(self, app, title, exe, sec):
        with self.lock:
            k = (date.today().isoformat(), app, title)
            self.buf[k] = self.buf.get(k, 0.0) + sec
            hk = (k[0], datetime.now().hour, app)
            self.hbuf[hk] = self.hbuf.get(hk, 0.0) + sec
            self.exe[app] = exe

    def set_exe(self, app, exe):
        with self.lock:
            self.exe[app] = exe
            self.db.execute("INSERT INTO apps(app,exe) VALUES(?,?) "
                            "ON CONFLICT(app) DO UPDATE SET exe=excluded.exe", (app, exe))
            self.db.commit()

    def flush(self):
        with self.lock:
            rows = [(*k, int(v)) for k, v in self.buf.items() if int(v)]
            hrows = [(*k, int(v)) for k, v in self.hbuf.items() if int(v)]
            try:
                with self.db:
                    self.db.executemany(
                        "INSERT INTO usage(day,app,title,seconds) VALUES(?,?,?,?) "
                        "ON CONFLICT(day,app,title) DO UPDATE SET seconds=seconds+excluded.seconds", rows)
                    self.db.executemany(
                        "INSERT INTO hourly_usage(day,hour,app,seconds) VALUES(?,?,?,?) "
                        "ON CONFLICT(day,hour,app) DO UPDATE SET seconds=seconds+excluded.seconds", hrows)
                    for app in {r[1] for r in rows}:
                        if self.exe.get(app):
                            self.db.execute("INSERT INTO apps(app,exe) VALUES(?,?) "
                                            "ON CONFLICT(app) DO UPDATE SET exe=excluded.exe", (app, self.exe[app]))
            except Exception:
                raise
            self.buf = {k: v-int(v) for k, v in self.buf.items() if v-int(v) > 0}
            self.hbuf = {k: v-int(v) for k, v in self.hbuf.items() if v-int(v) > 0}

    def q(self, sql, args=()):
        with self.lock:
            self.flush()
            return self.db.execute(sql, args).fetchall()

    def classify(self, app, title=""):
        if app in self.over:
            return self.over[app]
        if app in BROWSERS:
            t = title.lower()
            if any(k in t for k in ENT_KW): return E_
            if any(k in t for k in CHAT_KW): return C_
            if any(k in t for k in WORK_KW): return W_
            return CAT_LIST[4]
        return DEFAULT_CAT.get(app, CAT_LIST[4])

    def set_category(self, app, cat):
        with self.lock:
            self.over[app] = cat
            self.db.execute("INSERT INTO apps(app,category) VALUES(?,?) "
                            "ON CONFLICT(app) DO UPDATE SET category=excluded.category", (app, cat))
            self.db.commit()

    @staticmethod
    def _since(days):
        return (date.today() - timedelta(days=days - 1)).isoformat()

    def total(self, days=1):
        return self.q("SELECT COALESCE(SUM(seconds),0) FROM usage WHERE day>=?", (self._since(days),))[0][0]
    def total_day(self, offset=0):
        d = (date.today() - timedelta(days=offset)).isoformat()
        return self.q("SELECT COALESCE(SUM(seconds),0) FROM usage WHERE day=?", (d,))[0][0]
    def apps(self, days=1, limit=200):
        """Только приложения, в которых провели минуту и больше"""
        return self.q("SELECT app, SUM(seconds) s FROM usage WHERE day>=? GROUP BY app "
                      "HAVING SUM(seconds) >= ? ORDER BY s DESC LIMIT ?", (self._since(days), MIN_APP_SEC, limit))

    def daily(self, days=7):
        rows = dict(self.q("SELECT day, SUM(seconds) FROM usage WHERE day>=? GROUP BY day", (self._since(days),)))
        return [(date.today() - timedelta(days=i), rows.get((date.today() - timedelta(days=i)).isoformat(), 0))
                for i in range(days - 1, -1, -1)]
    def sites(self, days=1, limit=80):
        return self.sites_since(self._since(days), limit)

    def sites_since(self, since, limit=80):
        """Время по сайтам из заголовков вкладок (БД не меняется). Названия сайтов, которые часто
        повторяются в хвостах заголовков, программа запоминает сама. Меньше 5 минут — не показываем."""
        data = [(t, s) for app, t, s in self.q("SELECT app, title, SUM(seconds) FROM usage WHERE day>=? "
                                               "GROUP BY app, title", (since,)) if app in BROWSERS]
        tails = {}
        for t, s in data:
            p = title_parts(t)
            if len(p) >= 2 and len(p[-1]) <= 30:
                c = tails.setdefault(p[-1], [0, 0]); c[0] += 1; c[1] += s
        learned = {k for k, (n, sec) in tails.items() if n >= 2 or sec >= 900}
        res, other = {}, 0
        for t, s in data:
            k = site_of(t, learned)
            if k == "Другие страницы":
                other += s
            else:
                res[k] = res.get(k, 0) + s
        self.sites_other = other
        rows = sorted(((k, v) for k, v in res.items() if v >= MIN_SITE_SEC), key=lambda x: -x[1])
        return rows[:limit]

    def heatmap(self, days=7):
        """Сетка 7x24: сумма секунд по дню недели и часу за период"""
        grid = [[0] * 24 for _ in range(7)]
        for day, hour, s in self.q("SELECT day, hour, SUM(seconds) FROM hourly_usage WHERE day>=? "
                                   "GROUP BY day, hour", (self._since(days),)):
            grid[date.fromisoformat(day).weekday()][hour] += s
        return grid

    def app_today(self, app):
        return self.q("SELECT COALESCE(SUM(seconds),0) FROM usage WHERE day=? AND app=?",
                      (date.today().isoformat(), app))[0][0]

    def bl_set(self, app, limit_min):
        with self.lock:
            self.bl[app] = int(limit_min)
            self.db.execute("INSERT INTO blacklist(app,limit_min) VALUES(?,?) "
                            "ON CONFLICT(app) DO UPDATE SET limit_min=excluded.limit_min", (app, int(limit_min)))
            self.db.commit()

    def bl_del(self, app):
        with self.lock:
            self.bl.pop(app, None)
            self.db.execute("DELETE FROM blacklist WHERE app=?", (app,))
            self.db.commit()
    def goal_mode(self):
        return self.get("goal_mode", "work")        # "work" = набрать, "limit" = не больше

    def daily_goal(self, days=7):
        """[(дата, секунды)] по метрике цели: время работы/учёбы или всё экранное время"""
        if self.goal_mode() == "limit":
            return self.daily(days)
        rows = {}
        for day, app, title, s in self.q("SELECT day, app, title, seconds FROM usage WHERE day>=?",
                                         (self._since(days),)):
            if self.classify(app, title) == W_:
                rows[day] = rows.get(day, 0) + s
        return [(date.today() - timedelta(days=i), rows.get((date.today() - timedelta(days=i)).isoformat(), 0))
                for i in range(days - 1, -1, -1)]

    def goal_today(self):
        return self.daily_goal(1)[0][1]
    def categories(self, days=1):
        res = {c: 0 for c in CAT_LIST}
        for app, title, s in self.q("SELECT app, title, SUM(seconds) FROM usage WHERE day>=? "
                                    "GROUP BY app, title", (self._since(days),)):
            res[self.classify(app, title)] += s
        return res

    def streak(self):
        goal, limit = self.goal_sec(), self.goal_mode() == "limit"
        rows = {d.isoformat(): s for d, s in self.daily_goal(120)}
        d, n = date.today(), 0
        today_v = rows.get(d.isoformat(), 0)
        if limit:
            if today_v > goal:                 # лимит уже превышен — серия прервана
                return 0
            d -= timedelta(days=1)             # сегодняшний день ещё не закончен
        elif today_v < goal:
            d -= timedelta(days=1)
        while goal > 0:
            v = rows.get(d.isoformat(), 0)
            if not ((0 < v <= goal) if limit else v >= goal):
                break
            n += 1; d -= timedelta(days=1)
        return n

    def clear(self, today_only):
        with self.lock:
            self.buf.clear(); self.hbuf.clear()
            if today_only:
                d = date.today().isoformat()
                self.db.execute("DELETE FROM usage WHERE day=?", (d,))
                self.db.execute("DELETE FROM hourly_usage WHERE day=?", (d,))
            else:
                self.db.execute("DELETE FROM usage")
                self.db.execute("DELETE FROM hourly_usage")
            self.db.commit()
    def compact(self):
        """Старые заголовки окон -> только сайт (браузер) или пусто. Секунды сохраняются."""
        days = int(float(self.get("keep_days", 90)))
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self.lock:
            self.flush()
            rows = self.db.execute("SELECT day, app, title, seconds FROM usage "
                                   "WHERE day<? AND title<>''", (cutoff,)).fetchall()
            if not rows:
                return
            self.db.executemany("DELETE FROM usage WHERE day=? AND app=? AND title=?",
                                [(d, a, t) for d, a, t, _ in rows])
            merged = {}
            for d, a, t, s in rows:
                k = (d, a, site_of(t) if a in BROWSERS else "")
                merged[k] = merged.get(k, 0) + s
            self.db.executemany(
                "INSERT INTO usage(day,app,title,seconds) VALUES(?,?,?,?) "
                "ON CONFLICT(day,app,title) DO UPDATE SET seconds = seconds + excluded.seconds",
                [(*k, v) for k, v in merged.items()])
            self.db.commit()

# ------------------------------------------------------------------ Фоновый сборщик
class Tracker(threading.Thread):
    def __init__(self, store, notify, set_tooltip):
        super().__init__(daemon=True)
        self.store, self.notify, self.set_tooltip = store, notify, set_tooltip
        self.paused = False
        self.idle = False
        self.stop_ev = threading.Event()
        self.interval = POLL
        self.run_sec = 0.0
        self.seen = set()
        self.bl_state = {}
        self._last_enf = 0.0
        
    def _see(self, app, path):
        if app in self.seen:
            return
        self.seen.add(app)
        if self.store.exe.get(app) != path:
            self.store.set_exe(app, path)
        ensure_icon(app, path)

    def run(self):
        try:                                   # иконки и пути для программ, открытых прямо сейчас
            for app, path in scan_running().items():
                self._see(app, path)
        except Exception:
            log("scan: " + traceback.format_exc())
        last = last_flush = time.monotonic()
        while not self.stop_ev.wait(self.interval):
            now = time.monotonic()
            dt, last = now - last, now
            try:
                if now - last_flush >= FLUSH_EVERY:
                    self.store.flush()
                    last_flush = now
                if dt > 10:                    # компьютер спал
                    continue
                if self.paused:
                    self.interval = 3; continue
                if now - self._last_enf >= 10:
                    self._last_enf = now
                    self._enforce()
                idle = get_idle_seconds()
                idle_limit = int(float(self.store.get("idle_min", 1))) * 60
                self.idle = idle >= idle_limit
                if self.idle:
                    self.interval = 5
                    if idle >= max(BREAK_RESET, idle_limit + 60):
                        self.run_sec = 0
                    continue
                self.interval = POLL
                fg = get_foreground()
                if fg:
                    path, title = fg
                    app = friendly_name(path)
                    self._see(app, path)
                    self.store.add(app, title, path, dt)
                    self.run_sec += dt
                    self._check_break()
                if now - getattr(self, "_last_housekeeping", 0) >= FLUSH_EVERY:
                    self._housekeeping()
                    self._last_housekeeping = now
            except Exception:
                log("tracker: " + traceback.format_exc())
        self.store.flush()

    def _check_break(self):
        if self.store.get("break_on", "1") == "1":
            if self.run_sec >= float(self.store.get("break_min", 50)) * 60:
                self.run_sec = 0
                self.notify("Пора размяться", "Ты за компом уже давно. Встань, потянись, сделай 10 приседаний "
                            "или выйди подышать — голова скажет спасибо.")

    def _housekeeping(self):
        total = self.store.total(1)
        self.set_tooltip(f"FocusMeter — сегодня {fmt(total)}")
        today = date.today().isoformat()
        if self.store.get("compact_day", "") != today:
            self.store.set("compact_day", today)
            self.store.compact()        
        if self.store.get("goal_notify", "1") == "1" and self.store.get("goal_done", "") != today:
            val, goal = self.store.goal_today(), self.store.goal_sec()
            if self.store.goal_mode() == "limit":
                if val > goal:
                    self.store.set("goal_done", today)
                    self.notify("Лимит на день", f"Экранное время {fmt(val)} — лимит {fmt(goal)} превышен.")
            elif val >= goal:
                self.store.set("goal_done", today)
                self.notify("Цель дня выполнена", f"Работа и учёба: {fmt(val)}. Отличный результат!")
        if self.store.get("ent_on", "0") == "1":
            cats = self.store.categories(1)
            ent = cats[CAT_LIST[2]] + cats[CAT_LIST[3]]
            if (ent >= float(self.store.get("ent_min", 60)) * 60
                    and self.store.get("ent_done", "") != today):
                self.store.set("ent_done", today)
                self.notify("Лимит на развлечения", f"Развлечения и игры сегодня: {fmt(ent)}. Вернёмся к делу?")
    def _enforce(self):
        """Чёрный список: предупреждение -> 2 мин на сохранение -> мягкое закрытие -> принудительно"""
        bl = self.store.bl
        if not bl:
            return
        now, today = time.monotonic(), date.today().isoformat()
        for app, lim_min in list(bl.items()):
            try:
                st = self.bl_state.get(app)
                if not st or st["day"] != today:
                    st = self.bl_state[app] = {"day": today, "warned": False, "expired": None,
                                               "closing": None, "pids": [], "closed_once": False}
                limit = lim_min * 60
                used = self.store.app_today(app)
                if not st["warned"] and max(limit - WARN_BEFORE, limit * 0.5) <= used < limit:
                    st["warned"] = True
                    self.notify("Скоро лимит", f"{app}: осталось {fmt(limit - used)} из {lim_min} мин в день.")
                if used < limit:
                    continue
                if st["closing"]:                          # мягкое закрытие уже отправлено
                    if now - st["closing"] >= HARD_AFTER:
                        for pid, path in st["pids"]:
                            if (proc_path(pid) or "").lower() == path.lower() and not is_protected(path):
                                terminate_pid(pid)
                        st.update(closing=None, expired=None, pids=[], closed_once=True)
                        self.notify("Приложение закрыто", f"{app}: лимит превышен.")
                    continue
                wins = [w for w in app_windows(app) if not is_protected(w[2])]
                if not wins:                               # закрыл сам: повторный запуск получит только 20 сек
                    if st["expired"] is not None:
                        st["closed_once"] = True
                    st["expired"] = None
                    continue
                grace = 20 if st["closed_once"] else GRACE_SEC
                if st["expired"] is None:
                    st["expired"] = now
                    left = f"{grace // 60} мин" if grace >= 60 else f"{grace} сек"
                    self.notify("Лимит исчерпан", f"{app}: сохрани работу, закрою через {left}.")
                elif now - st["expired"] >= grace:
                    for hwnd, pid, path in wins:
                        user32.PostMessageW(hwnd, 0x0010, 0, 0)        # WM_CLOSE, как крестик
                    st["pids"] = list({(pid, path) for _, pid, path in wins})
                    st["closing"] = now
            except Exception:
                log("enforce: " + traceback.format_exc())
                
                
# ------------------------------------------------------------------ Утилиты
def csv_text(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def fmt(sec):
    sec = int(sec or 0); h, m = sec // 3600, sec % 3600 // 60
    if h:
        return f"{h} ч {m:02d} мин" if m else f"{h} ч"
    return f"{m} мин"

def fmt_short(sec):
    sec = int(sec or 0); h, m = sec // 3600, sec % 3600 // 60
    return f"{h}ч {m:02d}м" if h else (f"{m}м" if m else f"{sec}с")

def mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]; b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

def date_title():
    d = date.today()
    return f"{WD_FULL[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}"

def get_autostart():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run")
        try:
            winreg.QueryValueEx(k, "FocusMeter"); return True
        finally:
            winreg.CloseKey(k)
    except OSError:
        return False

def set_autostart(on):
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE)
        try:
            if on:
                exe = sys.executable
                if exe.lower().endswith("python.exe"):
                    exe = exe[:-10] + "pythonw.exe"
                winreg.SetValueEx(k, "FocusMeter", 0, winreg.REG_SZ,
                                  f'"{exe}" "{os.path.abspath(__file__)}" --background')
            else:
                try: winreg.DeleteValue(k, "FocusMeter")
                except FileNotFoundError: pass
        finally:
            winreg.CloseKey(k)
        return True
    except OSError as e:
        messagebox.showerror("Автозапуск", f"Не удалось изменить настройку:\n{e}")
        return False


# ------------------------------------------------------------------ Темы
class _Pal: pass
P = _Pal()
THEMES = {   # "light" = «Бумага», "dark" = «Кофе»
    "light": dict(name="light", BG="#F6F3EE", SIDE="#FDFBF7", CARD="#FFFFFF", FILL="#EFEAE3", TRACK="#E2D4BE",
                  LINE="#E9DDCA", TEXT="#2A1F18", MUTED="#706254", ACCENT="#B74730", ACCENT2="#EAA83A",
                  TINT="#F8E3D5", GREEN="#5E8B57", WARN="#D9962B", DANGER="#BF3B2B",
                  SHADOW=(10, 3, "#8B5A2B", 38),
                  CATS={CAT_LIST[0]: "#5E8B57", CAT_LIST[1]: "#3F6E8C", CAT_LIST[2]: "#C0405A",
                        CAT_LIST[3]: "#D6892B", CAT_LIST[4]: "#A89A89"}),
    "dark": dict(name="dark", BG="#1B1512", SIDE="#211A16", CARD="#292019", FILL="#352A22", TRACK="#4B3C31",
                 LINE="#3A2E26", TEXT="#F4EBDD", MUTED="#AD9C88", ACCENT="#E0714B", ACCENT2="#F3BC52",
                 TINT="#4A2D21", GREEN="#93C48B", WARN="#F3BC52", DANGER="#EC7B70",
                 SHADOW=(10, 3, "#000000", 110),
                 CATS={CAT_LIST[0]: "#93C48B", CAT_LIST[1]: "#7DB1D1", CAT_LIST[2]: "#E9809A",
                       CAT_LIST[3]: "#F3BC52", CAT_LIST[4]: "#8A7B6B"}),
}

def apply_theme(name):
    P.__dict__.update(THEMES.get(name, THEMES["light"]))
apply_theme("light")

K = 1.0
def S(v):
    return int(round(v * K))

def fnt(size, semi=False):
    return ("Segoe UI Semibold" if semi else "Segoe UI", size)

SERIF = "Georgia"        # шрифт заголовков «как в журнале»; можно заменить на "Cambria"
def fnt_serif(size):
    return (SERIF, size, "bold")


# ------------------------------------------------------------------ Картинки (PIL)
def rr_image(w, h, r, fill, bg, shadow=None, margin=0):
    """Антиалиасное скруглённое прямоугольное с мягкой тенью — как картинка (RGB)"""
    w, h = max(2, int(w)), max(2, int(h))
    r = max(1, min(int(r), w // 2, h // 2))
    base = Image.new("RGB", (w, h), bg)
    if shadow:
        blur, dy, col, alpha = shadow
        sh = Image.new("L", (w, h), 0)
        ImageDraw.Draw(sh).rounded_rectangle((margin, margin + dy, w - margin - 1, h - margin - 1 + dy), r, fill=alpha)
        sh = sh.filter(ImageFilter.GaussianBlur(blur))
        base.paste(Image.new("RGB", (w, h), col), (0, 0), sh)
    ss = 3
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((margin * ss, margin * ss, (w - margin) * ss - 1, (h - margin) * ss - 1),
                                           r * ss, fill=255)
    base.paste(Image.new("RGB", (w, h), fill), (0, 0), mask.resize((w, h), Image.LANCZOS))
    return base

_CACHE = {}
def rr_photo(w, h, r, fill, bg, cache=True):
    key = (w, h, r, fill, bg)
    if cache and key in _CACHE:
        return _CACHE[key]
    ph = ImageTk.PhotoImage(rr_image(w, h, r, fill, bg))
    if cache:
        _CACHE[key] = ph
    return ph

def vgrad(w, h, top, bottom):
    m = Image.linear_gradient("L").resize((max(1, w), max(1, h)))
    return Image.composite(Image.new("RGB", m.size, bottom), Image.new("RGB", m.size, top), m)

def ring_image(d, frac, thick, c1, c2, track, bg):
    ss = 3; D = d * ss; t = thick * ss
    im = Image.new("RGB", (D, D), bg); dr = ImageDraw.Draw(im)
    m = 4 * ss
    box = (m, m, D - m, D - m)
    dr.arc(box, 0, 360, fill=track, width=t)
    cl = (D - 2 * m) / 2 - t / 2                      # радиус средней линии кольца
    cx = cy = D / 2
    if frac > 0.003:
        total = 360 * min(frac, 1.0)
        steps = max(24, int(total / 3))
        for i in range(steps):
            a0 = -90 + total * i / steps
            dr.arc(box, a0, a0 + total / steps + 1.5, fill=mix(c1, c2, i / max(1, steps - 1)), width=t)
        for ang, col in ((-90, c1), (-90 + total, c2)):          # круглые края
            x = cx + cl * math.cos(math.radians(ang)); y = cy + cl * math.sin(math.radians(ang))
            dr.ellipse((x - t / 2, y - t / 2, x + t / 2, y + t / 2), fill=col)
    return im.resize((d, d), Image.LANCZOS)

def segbar_image(w, h, segs, track, bg):
    """Полоса категорий со скруглёнными краями. segs = [(цвет, доля)]"""
    ss = 3
    inner = Image.new("RGB", (w * ss, h * ss), track)
    d = ImageDraw.Draw(inner); x = 0.0
    for col, fr in segs:
        x2 = x + w * ss * fr
        if fr > 0:
            d.rectangle((int(x), 0, max(int(x) + 1, int(x2) - 2 * ss), h * ss), fill=col)
        x = x2
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), h * ss // 2, fill=255)
    out = Image.new("RGB", (w * ss, h * ss), bg); out.paste(inner, (0, 0), mask)
    return out.resize((w, h), Image.LANCZOS)

def chart_image(w, h, data, goal, geo):
    """Столбики-пилюли. geo = (pl, pr, pt, pb) в px. -> (image, список (x_центр, y_низ, y_верх))"""
    ss = 2; pl, pr, pt, pb = geo
    im = Image.new("RGB", (w * ss, h * ss), P.CARD); d = ImageDraw.Draw(im)
    n = len(data); cw, ch = w - pl - pr, h - pt - pb
    base = h - pb
    mx = max([s for _, s in data] + [goal if goal else 0, 3600])
    slot = cw / n; bw = min(S(56), slot * 0.6)
    pos = []
    if goal and goal <= mx:
        gy = (base - ch * goal / mx) * ss
        x = pl * ss
        while x < (w - pr) * ss:
            d.line((x, gy, min(x + 8 * ss, (w - pr) * ss), gy), fill=P.WARN, width=ss)
            x += 16 * ss
    for i, (day, sec) in enumerate(data):
        x0 = pl + slot * i + (slot - bw) / 2
        box = (x0 * ss, pt * ss, (x0 + bw) * ss, base * ss)
        rad = bw * ss / 2
        d.rounded_rectangle(box, rad, fill=P.FILL)
        if sec:
            bh = max(bw, sec / mx * ch)
            fb = (x0 * ss, (base - bh) * ss, (x0 + bw) * ss, base * ss)
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(fb, rad, fill=255)
            if day == date.today():
                col = vgrad(int(bw * ss) + 2, int(bh * ss) + 2, P.ACCENT2, P.ACCENT)
                layer = Image.new("RGB", im.size, P.FILL); layer.paste(col, (int(x0 * ss), int((base - bh) * ss)))
            else:
                layer = Image.new("RGB", im.size, mix(P.ACCENT, P.CARD, 0.45))
            im.paste(layer, (0, 0), mask)
        pos.append((x0 + bw / 2, base, pt))
    return im.resize((w, h), Image.LANCZOS), pos, mx
def line_chart_image(w, h, data, goal, geo):
    """Плавная кривая с градиентной заливкой. Возвращает то же, что chart_image."""
    ss = 3; pl, pr, pt, pb = geo
    W, H = w * ss, h * ss
    im = Image.new("RGB", (W, H), P.CARD); d = ImageDraw.Draw(im)
    n = len(data); ch = h - pt - pb; base = h - pb
    mx = max([s for _, s in data] + [goal or 0, 3600]) * 1.1
    slot = (w - pl - pr) / n
    xs = [(pl + slot * (i + 0.5)) * ss for i in range(n)]
    ys = [(base - ch * s / mx) * ss for _, s in data]
    bs = base * ss
    for k in range(0, 5):                                   # сетка
        y = (base - ch * k / 4) * ss
        d.line((pl * ss, y, (w - pr) * ss, y), fill=P.LINE, width=ss)
    if goal and goal <= mx:                                 # пунктир цели
        gy = (base - ch * goal / mx) * ss; x = pl * ss
        while x < (w - pr) * ss:
            d.line((x, gy, min(x + 8 * ss, (w - pr) * ss), gy), fill=P.WARN, width=ss)
            x += 16 * ss
    pts = list(zip(xs, ys)); curve = []                     # сглаживание Catmull-Rom
    for i in range(n - 1):
        p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, n - 1)]
        for j in range(12):
            t = j / 12
            def cr(a, b, c, e):
                return 0.5 * (2 * b + (c - a) * t + (2 * a - 5 * b + 4 * c - e) * t * t
                              + (3 * b - a - 3 * c + e) * t ** 3)
            curve.append((cr(p0[0], p1[0], p2[0], p3[0]), min(bs, cr(p0[1], p1[1], p2[1], p3[1]))))
    curve.append(pts[-1])
    mask = Image.new("L", (W, H), 0)                        # градиентная заливка под линией
    ImageDraw.Draw(mask).polygon(curve + [(xs[-1], bs), (xs[0], bs)], fill=255)
    im.paste(vgrad(W, H, mix(P.ACCENT, P.CARD, 0.45), P.CARD), (0, 0), mask)
    d.line(curve, fill=P.ACCENT, width=3 * ss, joint="curve")
    r0 = 3 * ss if n > 7 else 5 * ss
    for (day, sec), x, y in zip(data, xs, ys):              # точки
        if not sec:
            continue
        r = r0 + (2 * ss if day == date.today() else 0)
        d.ellipse((x - r - ss * 2, y - r - ss * 2, x + r + ss * 2, y + r + ss * 2), fill=P.CARD)
        d.ellipse((x - r, y - r, x + r, y + r), fill=P.ACCENT2 if day == date.today() else P.ACCENT)
    pos = [(x / ss, base, pt) for x in xs]
    return im.resize((w, h), Image.LANCZOS), pos, mx

def heatmap_image(w, h, grid, geo):
    """Сетка 7x24. geo = (pl, pr, pt, pb). -> (image, ширина ячейки, высота ячейки, максимум)"""
    ss = 2; pl, pr, pt, pb = geo
    im = Image.new("RGB", (w * ss, h * ss), P.CARD); d = ImageDraw.Draw(im)
    cw, chh = (w - pl - pr) / 24, (h - pt - pb) / 7
    mx = max([max(r) for r in grid] + [1]); gap = 3
    for r in range(7):
        for c in range(24):
            v = grid[r][c]
            col = mix(P.FILL, P.ACCENT, (v / mx) ** 0.6 if v else 0)
            x0, y0 = pl + c * cw + gap / 2, pt + r * chh + gap / 2
            d.rounded_rectangle((x0 * ss, y0 * ss, (x0 + cw - gap) * ss, (y0 + chh - gap) * ss),
                                4 * ss, fill=col)
    return im.resize((w, h), Image.LANCZOS), cw, chh, mx

def make_logo(size=64):
    ss = 4; N = size * ss
    gm = Image.linear_gradient("L").resize((N, N))
    diag = ImageChops.add(gm, gm.transpose(Image.ROTATE_90), scale=2)
    bgimg = Image.composite(Image.new("RGB", (N, N), "#F2B84B"), Image.new("RGB", (N, N), "#C9553A"), diag)
    rm = Image.new("L", (N, N), 0)
    ImageDraw.Draw(rm).rounded_rectangle((0, 0, N - 1, N - 1), N // 4, fill=255)
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0)); out.paste(bgimg, (0, 0), rm)
    d = ImageDraw.Draw(out)
    m = N * 0.25
    d.arc((m, m, N - m, N - m), -90, 215, fill="white", width=max(3, int(N * 0.10)))
    r = N * 0.075
    d.ellipse((N / 2 - r, N / 2 - r, N / 2 + r, N / 2 + r), fill="white")
    return out.resize((size, size), Image.LANCZOS)

def make_avatar(name, size):
    pal = ["#C9553A", "#5E8B57", "#3F6E8C", "#D6892B", "#B0446B", "#7A5C99", "#2F8F83"]
    col = pal[int(hashlib.md5(name.encode()).hexdigest()[:6], 16) % len(pal)]
    ss = 3; N = size * ss
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, N - 1, N - 1), N // 4, fill=col)
    try:
        f = ImageFont.truetype("segoeuib.ttf", int(N * 0.5))
    except Exception:
        try: f = ImageFont.truetype("DejaVuSans-Bold.ttf", int(N * 0.5))
        except Exception: f = ImageFont.load_default()
    ch = (name[:1] or "?").upper()
    b = d.textbbox((0, 0), ch, font=f)
    d.text(((N - (b[2] - b[0])) / 2 - b[0], (N - (b[3] - b[1])) / 2 - b[1]), ch, fill="white", font=f)
    return im.resize((size, size), Image.LANCZOS)


class IconCache:
    """Плитки с иконками приложений для списков"""
    def __init__(self):
        self.mem = {}

    def get(self, app, compact=False):
        fn = icon_file(app)
        real = os.path.exists(fn)
        key = (app, real, P.name, K, compact)
        if key in self.mem:
            return self.mem[key]
        size = S(24 if compact else 44)
        background = P.BG if compact else P.CARD
        try:
            if real:
                isz = S(22 if compact else 28)
                ic = Image.open(fn).convert("RGBA").resize((isz, isz), Image.LANCZOS)
                tile = rr_image(size, size, S(6 if compact else 12), background if compact else P.FILL, background)
                tile.paste(ic, ((size - isz) // 2, (size - isz) // 2), ic)
            else:
                av = make_avatar(app, size)
                tile = Image.new("RGB", (size, size), background); tile.paste(av, (0, 0), av)
        except Exception:
            log("icon tile: " + traceback.format_exc())
            av = make_avatar(app, size)
            tile = Image.new("RGB", (size, size), background); tile.paste(av, (0, 0), av)
        ph = ImageTk.PhotoImage(tile)
        self.mem[key] = ph
        return ph


# ------------------------------------------------------------------ Виджеты
class Card(tk.Canvas):
    """Белая карточка со скруглением и мягкой тенью. Содержимое — в self.body"""
    def __init__(self, parent, fill=None, bg=None, radius=18, margin=8, shadow=True, fit=False, **kw):
        self.fill, self.pbg = fill or P.CARD, bg or P.BG
        self.margin, self.radius, self.shadow, self.fit = S(margin), S(radius), shadow, fit
        self.inset = self.margin + int(self.radius * 0.3) + 1
        super().__init__(parent, bg=self.pbg, highlightthickness=0, bd=0, **({"height": 1} if fit else {}), **kw)
        self.body = tk.Frame(self, bg=self.fill)
        self._img = self.create_image(0, 0, anchor="nw")
        self._win = self.create_window(self.inset, self.inset, window=self.body, anchor="nw")
        self._ph, self._job, self._wh = None, None, (0, 0)
        self.bind("<Configure>", self._conf)
        if fit:
            self.body.bind("<Configure>", lambda e: self._sync_h())
            self.bind("<Map>", lambda e: self._sync_h())
            self.after(50, self._sync_h)

    def _conf(self, e):
        self._wh = (e.width, e.height)
        w = max(1, e.width - 2 * self.inset)
        if self.fit:
            self.itemconfigure(self._win, width=w)
        else:
            self.itemconfigure(self._win, width=w, height=max(1, e.height - 2 * self.inset))
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(25, self._render)

    def _sync_h(self):
        try:
            h = self.body.winfo_reqheight() + 2 * self.inset
            if h > 4 and h != int(float(self["height"])):
                self.configure(height=h)
        except tk.TclError:
            pass

    def _render(self):
        self._job = None
        w, h = self._wh
        if w < 4 or h < 4:
            return
        sh = (S(10), S(3), P.SHADOW[2], P.SHADOW[3]) if self.shadow else None
        im = rr_image(w, h, self.radius, self.fill, self.pbg, sh, self.margin)
        self._ph = ImageTk.PhotoImage(im)
        self.itemconfigure(self._img, image=self._ph)


class Pill(tk.Canvas):
    """Кнопка-пилюля (также используется как метка-чип)"""
    def __init__(self, parent, text, fg, fill, pbg, command=None, hover=None, size=9, semi=True,
                 padx=16, pady=8, min_w=0):
        self.f = tkfont.Font(family="Segoe UI Semibold" if semi else "Segoe UI", size=size)
        self.text, self.fg, self.fill, self.hv, self.pbg = text, fg, fill, hover, pbg
        self.padx, self.pady, self.min_w = S(padx), S(pady), S(min_w)
        super().__init__(parent, bg=pbg, highlightthickness=0, bd=0, cursor="hand2" if command else "")
        self.command = command
        self._render(fill)
        if command:
            self.configure(takefocus=True, highlightcolor=P.ACCENT)
            self.bind("<Button-1>", lambda e: self.command())
            self.bind("<Return>", lambda e: self.command())
            self.bind("<space>", lambda e: self.command())
            self.bind("<FocusIn>", lambda e: self.configure(highlightthickness=2))
            self.bind("<FocusOut>", lambda e: self.configure(highlightthickness=0))
        if hover:
            self.bind("<Enter>", lambda e: self._render(self.hv))
            self.bind("<Leave>", lambda e: self._render(self.fill))

    def _render(self, fill):
        w = max(self.min_w, self.f.measure(self.text) + 2 * self.padx)
        h = self.f.metrics("linespace") + 2 * self.pady
        self.configure(width=w, height=h)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=rr_photo(w, h, h // 2, fill, self.pbg))
        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg, font=self.f)

    def reconfig(self, text=None, fg=None, fill=None, hover=None):
        if text is not None: self.text = text
        if fg is not None: self.fg = fg
        if fill is not None: self.fill = fill
        if hover is not None: self.hv = hover
        self._render(self.fill)


class Seg(tk.Frame):
    """Переключатель вида [ Сегодня | 7 дней | 30 дней ]"""
    def __init__(self, parent, pbg, options, value, command):
        super().__init__(parent, bg=pbg)
        self.cmd, self.btns = command, {}
        for k, t in options:
            b = Pill(self, t, P.MUTED, P.FILL, pbg, command=lambda k=k: self.select(k, True), padx=15, pady=7)
            b.pack(side="left", padx=3); self.btns[k] = b
        self.select(value, False)

    def select(self, k, fire):
        for key, b in self.btns.items():
            on = key == k
            b.reconfig(fg="#FFFFFF" if on else P.MUTED, fill=P.ACCENT if on else P.FILL)
        if fire:
            self.cmd(k)


class Switch(tk.Canvas):
    def __init__(self, parent, value, command, pbg):
        self.w, self.h = S(46), S(26)
        super().__init__(parent, width=self.w, height=self.h, bg=pbg, highlightthickness=0, bd=0, cursor="hand2")
        self.on, self.cmd, self.pbg = bool(value), command, pbg
        self.bind("<Button-1>", self._tog)
        self._draw()

    def _tog(self, _):
        self.on = not self.on; self._draw(); self.cmd(self.on)

    def _draw(self):
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=rr_photo(self.w, self.h, self.h // 2,
                                                            P.ACCENT if self.on else P.TRACK, self.pbg))
        r = self.h - S(8); x = self.w - S(4) - r if self.on else S(4)
        self.create_oval(x, S(4), x + r, S(4) + r, fill="#FFFFFF", outline="")


class Stepper(tk.Frame):
    def __init__(self, parent, pbg, value, step, lo, hi, fmt_fn, on_change):
        super().__init__(parent, bg=pbg)
        self.v, self.step, self.lo, self.hi, self.fmt, self.cb = value, step, lo, hi, fmt_fn, on_change
        Pill(self, "−", P.TEXT, P.FILL, pbg, command=lambda: self._chg(-1), padx=13, pady=5, size=11).pack(side="left")
        self.lbl = tk.Label(self, text=fmt_fn(value), bg=pbg, fg=P.TEXT, font=fnt(11, True), width=9)
        self.lbl.pack(side="left")
        Pill(self, "+", P.TEXT, P.FILL, pbg, command=lambda: self._chg(1), padx=13, pady=5, size=11).pack(side="left")

    def _chg(self, d):
        self.v = max(self.lo, min(self.hi, self.v + d * self.step))
        self.lbl.configure(text=self.fmt(self.v)); self.cb(self.v)


class IconTile(tk.Canvas):
    def __init__(self, parent, glyph, color, pbg):
        s = S(40)
        super().__init__(parent, width=s, height=s, bg=pbg, highlightthickness=0, bd=0)
        self.create_image(0, 0, anchor="nw", image=rr_photo(s, s, S(12), mix(color, pbg, 0.86), pbg))
        self.create_text(s // 2, s // 2, text=glyph, fill=color, font=(ICONFONT, 14))


class NavItem(tk.Canvas):
    def __init__(self, parent, glyph, text, command):
        super().__init__(parent, height=S(46), bg=P.SIDE, highlightthickness=0, bd=0, cursor="hand2")
        self.glyph, self.text, self.active, self.hover = glyph, text, False, False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))

    def _set(self, hover=None, active=None):
        if hover is not None: self.hover = hover
        if active is not None: self.active = active
        self._draw()

    def set_active(self, a):
        self._set(active=a)

    def _draw(self):
        w, h = self.winfo_width(), S(46)
        if w < 10:
            return
        self.delete("all")
        fill = P.TINT if self.active else (P.FILL if self.hover else None)
        if fill:
            self.create_image(0, 0, anchor="nw", image=rr_photo(w, h, S(13), fill, P.SIDE))
        self.create_text(S(20), h // 2, text=self.glyph, font=(ICONFONT, 13), anchor="w",
                         fill=P.ACCENT if self.active else P.MUTED)
        self.create_text(S(54), h // 2, text=self.text, anchor="w",
                         font=fnt(10, self.active), fill=P.ACCENT if self.active else P.MUTED)


class ScrollFrame(tk.Frame):
    """Прокручиваемая область без уродливого скроллбара (колесо мыши)"""
    is_scroll = True
    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=S(28))
        self.inner = tk.Frame(self.cv, bg=bg)
        wid = self.cv.create_window((0, 0), window=self.inner, anchor="nw")
        self.cv.pack(fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(wid, width=e.width))

    def scroll(self, delta):
        if self.inner.winfo_reqheight() > self.cv.winfo_height():
            self.cv.yview_scroll(-2 if delta > 0 else 2, "units")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.cv.yview_moveto(0)


class Bar(tk.Canvas):
    def __init__(self, parent, ratio, color, bg):
        super().__init__(parent, height=S(8), bg=bg, highlightthickness=0, bd=0)
        self.ratio, self.color = ratio, color
        self.bind("<Configure>", lambda e: self._draw(e.width))

    def _draw(self, w):
        self.delete("all"); h = S(8); r = h / 2
        self.create_line(r, r, w - r, r, width=h, capstyle="round", fill=P.FILL)
        x2 = r + (w - 2 * r) * max(0.0, min(1.0, self.ratio))
        self.create_line(r, r, max(x2, r + 0.1), r, width=h, capstyle="round", fill=self.color)


class CatBar(tk.Frame):
    """Полоса категорий + легенда"""
    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.bg, self.data, self._sig, self._ph = bg, {}, None, None
        self.cv = tk.Canvas(self, height=S(10), bg=bg, highlightthickness=0, bd=0)
        self.cv.pack(fill="x")
        self.legend = tk.Frame(self, bg=bg); self.legend.pack(fill="x", pady=(S(12), 0))
        self.cv.bind("<Configure>", lambda e: self._draw())

    def update_data(self, data):
        self.data = data; self._draw()
        sig = tuple(data.values())
        if sig == self._sig:
            return
        self._sig = sig
        for w in self.legend.winfo_children():
            w.destroy()
        for i, c in enumerate(CAT_LIST):
            f = tk.Frame(self.legend, bg=self.bg); f.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, S(20)), pady=S(2))
            tk.Label(f, text="●", fg=P.CATS[c], bg=self.bg, font=fnt(9)).pack(side="left")
            tk.Label(f, text=c, fg=P.MUTED, bg=self.bg, font=fnt(9)).pack(side="left", padx=(S(4), 0))
            tk.Label(f, text=fmt_short(data.get(c, 0)), fg=P.TEXT, bg=self.bg, font=fnt(9, True)).pack(side="left", padx=(S(6), 0))

    def _draw(self):
        w, h = self.cv.winfo_width(), S(10)
        if w < 20:
            return
        total = sum(self.data.values())
        segs = [(P.CATS[c], self.data.get(c, 0) / total) for c in CAT_LIST] if total else []
        self._ph = ImageTk.PhotoImage(segbar_image(w, h, segs, P.FILL, self.bg))
        self.cv.delete("all"); self.cv.create_image(0, 0, anchor="nw", image=self._ph)


# ------------------------------------------------------------------ Окно
# ------------------------------------------------------------------ Wrapped (итоги периода)
WR_W, WR_H = 1080, 1920                       # размер слайда: формат сторис
WR_PERIODS = [("30", "30 дней"), ("year", "Этот год"), ("all", "Всё время")]
WR_TITLES = ["Итого", "Топ приложений", "Марафон", "Твой ритм", "Ночной режим", "Итоговая карточка"]
WR_MIN_SEC = 600                               # меньше 10 минут данных — итогов ещё нет
WR_PAL = {                                     # (верх, низ) градиента каждого слайда
    "intro": ("#D9603F", "#6E2616"), "apps": ("#3F6E8C", "#1B3447"),
    "marathon": ("#5E8B57", "#22391F"), "rhythm": ("#E0963A", "#7A3F0E"),
    "night": ("#352A6B", "#0E0A26"), "final": ("#E0714B", "#8E3420"),
}


def plural(n, one, few, many):
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _dstr(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def wr_since(store, period):
    """Первый день периода (ISO), но не раньше первого дня с данными"""
    today = date.today()
    if period == "30":
        start = today - timedelta(days=29)
    elif period == "year":
        start = date(today.year, 1, 1)
    else:
        start = date(2000, 1, 1)
    first = store.q("SELECT MIN(day) FROM usage")[0][0]
    if first and first > start.isoformat():
        return first
    return start.isoformat()


def wrapped_stats(store, since):
    """Все цифры для слайдов. Считается только из уже записанной базы."""
    day_tot = {d: s for d, s in store.q("SELECT day, SUM(seconds) FROM usage WHERE day>=? GROUP BY day", (since,))}
    total = sum(day_tot.values())
    if total < WR_MIN_SEC:
        return {"empty": True}
    st = {"empty": False, "total": total}
    first = date.fromisoformat(min(day_tot))
    today = date.today()
    st["period_txt"] = (_dstr(today) + f" {today.year}" if first == today
                        else f"{_dstr(first)} — {_dstr(today)} {today.year}")
    active = [d for d, s in day_tot.items() if s >= WR_MIN_SEC]
    st["days_active"] = len(active) or 1
    st["avg"] = total / st["days_active"]
    best_day = max(day_tot.items(), key=lambda x: x[1])
    st["best_day"] = (date.fromisoformat(best_day[0]), best_day[1])

    # серия дней подряд (день считается, если за ним минимум 10 минут)
    ds = sorted(date.fromisoformat(d) for d in active)
    best_run = run = 0
    prev = None
    for d in ds:
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best_run = max(best_run, run)
        prev = d
    st["streak"] = best_run

    # приложения, сайты, категории
    cats = {c: 0 for c in CAT_LIST}
    for app, title, s in store.q("SELECT app, title, SUM(seconds) FROM usage WHERE day>=? GROUP BY app, title", (since,)):
        cats[store.classify(app, title)] += s
    st["cats"] = cats
    st["apps"] = store.q("SELECT app, SUM(seconds) s FROM usage WHERE day>=? GROUP BY app HAVING s>=? "
                         "ORDER BY s DESC LIMIT 5", (since, MIN_APP_SEC))
    top_sites = store.sites_since(since, 1)
    st["site"] = top_sites[0] if top_sites else None
    st["top_share"] = st["apps"][0][1] / total if st["apps"] else 0

    # часы суток
    hours = [0] * 24
    per_day = {}
    for day, hour, s in store.q("SELECT day, hour, SUM(seconds) FROM hourly_usage WHERE day>=? GROUP BY day, hour", (since,)):
        hours[hour] += s
        per_day.setdefault(day, {})[hour] = s
    st["hours"] = hours
    st["peak_hour"] = max(range(24), key=lambda h: hours[h]) if sum(hours) else None
    st["night"] = sum(hours[0:5])
    st["morning"] = sum(hours[5:11])
    st["night_days"] = sum(1 for h in per_day.values() if sum(v for k, v in h.items() if k < 5) >= WR_MIN_SEC)

    # марафон: больше всего часов подряд, в каждом из которых минимум 20 минут
    best = (0, None, 0)
    for day, h in per_day.items():
        run, start = 0, 0
        for hr in range(25):
            if hr < 24 and h.get(hr, 0) >= 1200:
                if run == 0:
                    start = hr
                run += 1
            else:
                if run > best[0]:
                    best = (run, day, start)
                run = 0
    st["marathon"] = (best[0], date.fromisoformat(best[1]), best[2]) if best[1] else None

    # лучший день недели: в среднем за активный день
    wd_sum, wd_n = [0] * 7, [0] * 7
    for d, s in day_tot.items():
        if s >= WR_MIN_SEC:
            w = date.fromisoformat(d).weekday()
            wd_sum[w] += s; wd_n[w] += 1
    wd_avg = [wd_sum[i] / wd_n[i] if wd_n[i] else 0 for i in range(7)]
    st["best_wd"] = max(range(7), key=lambda i: wd_avg[i])
    st["personality"] = wr_personality(st)
    return st


def wr_personality(st):
    t = st["total"] or 1
    share = lambda c: st["cats"].get(c, 0) / t
    if st["night"] >= 3600 and st["night"] / t >= 0.15:
        return "Сова", "Лучшие идеи приходят после полуночи"
    if share(G_) >= 0.30:
        return "Геймер", "Игры — твоя главная территория"
    if share(E_) >= 0.30:
        return "Киноман", "Видео и стримы — твоя стихия"
    if share(W_) >= 0.50:
        return "Трудоголик", "Больше половины времени — работа и учёба"
    if share(C_) >= 0.30:
        return "Коммуникатор", "Ты всегда на связи"
    if st["morning"] / t >= 0.35:
        return "Жаворонок", "Ты начинаешь день раньше всех"
    if st["top_share"] >= 0.50:
        return "Снайпер", "Одно приложение — и полный фокус"
    return "Исследователь", "Много программ, много идей"


# ---------- рисование слайдов (Pillow, не зависит от темы окна)
_WF = {}
def wf(size, bold=True, serif=False):
    key = (size, bold, serif)
    if key in _WF:
        return _WF[key]
    if serif:
        names = ["georgiab.ttf", "Georgia Bold.ttf", "DejaVuSerif-Bold.ttf"]
    elif bold:
        names = ["segoeuib.ttf", "DejaVuSans-Bold.ttf"]
    else:
        names = ["segoeui.ttf", "DejaVuSans.ttf"]
    f = None
    for n in names:
        try:
            f = ImageFont.truetype(n, size); break
        except Exception:
            pass
    if f is None:
        try: f = ImageFont.load_default(size)
        except Exception: f = ImageFont.load_default()
    _WF[key] = f
    return f


def _fit(d, text, maxw, size, bold=True, serif=False, minsize=28):
    while size > minsize and d.textlength(text, font=wf(size, bold, serif)) > maxw:
        size -= 4
    return wf(size, bold, serif)


def _wrap(d, text, font, maxw):
    lines, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if d.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    return lines


def _wbase(key, idx, n, period_txt):
    top, bot = WR_PAL[key]
    im = vgrad(WR_W, WR_H, top, bot).convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
    od.ellipse((-260, -300, 620, 580), fill=(255, 255, 255, 22))
    od.ellipse((560, 1250, 1500, 2190), fill=(255, 255, 255, 18))
    od.ellipse((720, 360, 1200, 840), fill=(0, 0, 0, 20))
    im = Image.alpha_composite(im, ov).convert("RGB")
    d = ImageDraw.Draw(im)
    gap = 12; seg = (WR_W - 180 - gap * (n - 1)) / n
    for i in range(n):
        x0 = 90 + i * (seg + gap)
        d.rounded_rectangle((x0, 84, x0 + seg, 94), 5, fill="#FFFFFF" if i <= idx else mix(top, "#FFFFFF", 0.35))
    soft = mix(bot, "#FFFFFF", 0.62)
    d.text((90, 1790), "FocusMeter · Wrapped", font=wf(34), fill=soft)
    d.text((WR_W - 90, 1790), period_txt, font=_fit(d, period_txt, 480, 34), fill=soft, anchor="ra")
    return im, d


def _wr_icon(app, size):
    mask = Image.new("L", (size * 3, size * 3), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 3 - 1, size * 3 - 1), size * 3 // 4, fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    try:
        fn = icon_file(app)
        if os.path.exists(fn):
            tile = Image.new("RGBA", (size, size), (255, 252, 246, 255)); tile.putalpha(mask)
            isz = int(size * 0.62)
            ic = Image.open(fn).convert("RGBA").resize((isz, isz), Image.LANCZOS)
            tile.paste(ic, ((size - isz) // 2, (size - isz) // 2), ic)
            return tile
    except Exception:
        pass
    return make_avatar(app, size)


def _kicker(d, text):
    d.text((90, 240), text, font=wf(42), fill="#FFD9A0")


def _slide_intro(st, i, n):
    im, d = _wbase("intro", i, n, st["period_txt"])
    _kicker(d, "ТВОЙ WRAPPED")
    d.text((90, 520), "Ты провёл за компьютером", font=wf(58, False), fill="#FFF1DC")
    h, m = int(st["total"] // 3600), int(st["total"] % 3600 // 60)
    if h:
        big, unit = str(h), plural(h, "час", "часа", "часов")
    else:
        big, unit = str(m), plural(m, "минуту", "минуты", "минут")
    d.text((90, 600), big, font=_fit(d, big, 900, 400), fill="#FFFFFF")
    d.text((90, 1080), unit, font=wf(120), fill="#FFFFFF")
    days = f"{st['total'] / 86400:.1f}".replace(".", ",")
    films = int(st["total"] / 7200)
    lines = [f"Это {days} суток без перерыва на сон"]
    if films >= 1:
        lines.append(f"Или {films} {plural(films, 'полнометражный фильм', 'полнометражных фильма', 'полнометражных фильмов')}")
    lines.append(f"В среднем {fmt(st['avg'])} в день")
    for k, t in enumerate(lines):
        d.text((90, 1290 + k * 92), t, font=_fit(d, t, 900, 52, False), fill="#FFF1DC")
    return im


def _slide_apps(st, i, n):
    im, d = _wbase("apps", i, n, st["period_txt"])
    _kicker(d, "ТВОЙ ТОП")
    d.text((90, 316), "Где ты жил", font=_fit(d, "Где ты жил", 900, 110, serif=True, minsize=60), fill="#FFFFFF")
    top, bot = WR_PAL["apps"]
    for k, (app, s) in enumerate(st["apps"][:3]):
        y = 600 + k * 340
        d.text((90, y + 10), str(k + 1), font=wf(150), fill=mix(top, "#FFFFFF", 0.45))
        ic = _wr_icon(app, 150)
        im.paste(ic, (220, y), ic)
        d.text((410, y - 4), app, font=_fit(d, app, 580, 66), fill="#FFFFFF")
        share = s / st["total"]
        d.text((410, y + 84), f"{fmt(s)}  ·  {int(share * 100)}%", font=wf(46, False), fill="#FFF1DC")
        d.rounded_rectangle((410, y + 160, 990, y + 182), 11, fill=mix(top, "#FFFFFF", 0.22))
        d.rounded_rectangle((410, y + 160, 410 + max(22, 580 * min(1, share)), y + 182), 11, fill="#FFFFFF")
    if st["site"]:
        d.text((90, 1650), f"Любимый сайт: {st['site'][0]}  ·  {fmt(st['site'][1])}",
               font=_fit(d, f"Любимый сайт: {st['site'][0]}  ·  {fmt(st['site'][1])}", 900, 46, False), fill="#FFF1DC")
    return im


def _slide_marathon(st, i, n):
    im, d = _wbase("marathon", i, n, st["period_txt"])
    mar = st["marathon"]
    if mar and mar[0] >= 2:
        run, day, start = mar
        _kicker(d, "МАРАФОН")
        d.text((90, 520), "Дольше всего без пауз", font=wf(58, False), fill="#E8F3E2")
        d.text((90, 600), str(run), font=wf(420), fill="#FFFFFF")
        d.text((90, 1090), plural(run, "час подряд", "часа подряд", "часов подряд"), font=wf(110), fill="#FFFFFF")
        d.text((90, 1260), f"{WD_FULL[day.weekday()].capitalize()}, {_dstr(day)}", font=wf(60), fill="#E8F3E2")
        d.text((90, 1344), f"с {start:02d}:00 до {(start + run) % 24:02d}:00", font=wf(52, False), fill="#E8F3E2")
        shown = min(run, 14); gap = 12; bw = (900 - gap * (shown - 1)) / shown
        for k in range(shown):
            x0 = 90 + k * (bw + gap)
            d.rounded_rectangle((x0, 1500, x0 + bw, 1640), 18, fill="#FFFFFF")
    else:
        day, s = st["best_day"]
        _kicker(d, "РЕКОРДНЫЙ ДЕНЬ")
        d.text((90, 520), "Самый насыщенный день", font=wf(58, False), fill="#E8F3E2")
        big = fmt(s)
        d.text((90, 640), big, font=_fit(d, big, 900, 240), fill="#FFFFFF")
        d.text((90, 960), f"{WD_FULL[day.weekday()].capitalize()}, {_dstr(day)}", font=wf(70), fill="#FFFFFF")
        d.text((90, 1080), "Дольше всего ты сидел за ПК в этот день", font=_fit(d, "Дольше всего ты сидел за ПК в этот день", 900, 46, False), fill="#E8F3E2")
    return im


def _slide_rhythm(st, i, n):
    im, d = _wbase("rhythm", i, n, st["period_txt"])
    top, bot = WR_PAL["rhythm"]
    _kicker(d, "ТВОЙ РИТМ")
    d.text((90, 520), "Самый активный день недели", font=wf(58, False), fill="#FFF1DC")
    name = WD_FULL[st["best_wd"]].capitalize()
    d.text((90, 610), name, font=_fit(d, name, 900, 200, serif=True, minsize=90), fill="#FFFFFF")
    ph = st["peak_hour"]
    if ph is not None:
        pk = f"Пик активности: {ph:02d}:00–{(ph + 1) % 24:02d}:00"
        d.text((90, 880), pk, font=_fit(d, pk, 900, 60), fill="#FFFFFF")
        hrs = st["hours"]; mx = max(hrs) or 1
        base_y, hmax = 1560, 460
        slot = 900 / 24
        for hr in range(24):
            hh = max(8, hmax * hrs[hr] / mx) if hrs[hr] else 8
            x0 = 90 + hr * slot + 4
            col = "#FFFFFF" if hr == ph else mix(top, "#FFFFFF", 0.38)
            d.rounded_rectangle((x0, base_y - hh, x0 + slot - 8, base_y), 8, fill=col)
        for hr in (0, 6, 12, 18, 23):
            d.text((90 + hr * slot + slot / 2, base_y + 24), str(hr), font=wf(34, False), fill="#FFF1DC", anchor="ma")
    day, s = st["best_day"]
    d.text((90, 1700), f"Рекорд: {_dstr(day)} — {fmt(s)}", font=wf(42, False), fill="#FFF1DC")
    return im


def _slide_night(st, i, n):
    im, d = _wbase("night", i, n, st["period_txt"])
    night = st["night"]
    for (x, y, r) in ((140, 460, 5), (260, 330, 4), (880, 300, 6), (960, 520, 4), (760, 640, 5), (330, 640, 3)):
        d.ellipse((x - r, y - r, x + r, y + r), fill="#FFFFFF")
    ms = 360                                         # луна-полумесяц
    mask = Image.new("L", (ms * 2, ms * 2), 0); md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, ms * 2 - 1, ms * 2 - 1), fill=255)
    md.ellipse((ms * 0.55, -ms * 0.15, ms * 2.25, ms * 1.75), fill=0)
    mask = mask.resize((ms, ms), Image.LANCZOS)
    im.paste(Image.new("RGB", (ms, ms), "#FFE8B8"), (360, 360), mask)
    _kicker(d, "НОЧНОЙ РЕЖИМ")
    if night >= 1800:
        d.text((90, 840), "После полуночи ты просидел", font=_fit(d, "После полуночи ты просидел", 900, 58, False), fill="#D9D2F5")
        big = fmt(night)
        d.text((90, 930), big, font=_fit(d, big, 900, 230), fill="#FFFFFF")
        d.text((90, 1230), "между 00:00 и 05:00", font=wf(64), fill="#FFFFFF")
        nd = st["night_days"]
        if nd:
            nt = f"{nd} {plural(nd, 'ночь', 'ночи', 'ночей')} за компьютером"
            d.text((90, 1350), nt, font=_fit(d, nt, 900, 52, False), fill="#D9D2F5")
        d.text((90, 1430), f"{int(night / st['total'] * 100)}% всего времени", font=wf(52, False), fill="#D9D2F5")
    else:
        d.text((90, 840), "Ты бережёшь сон", font=_fit(d, "Ты бережёшь сон", 900, 110, serif=True, minsize=60), fill="#FFFFFF")
        d.text((90, 1010), "После полуночи за компьютером", font=wf(56, False), fill="#D9D2F5")
        d.text((90, 1090), f"почти не сидишь: всего {fmt(night)}" if night >= 60 else "почти не сидишь",
               font=wf(56, False), fill="#D9D2F5")
    return im


def _slide_final(st, i, n):
    im, d = _wbase("final", i, n, st["period_txt"])
    cx0, cy0, cx1, cy1 = 60, 150, WR_W - 60, 1730
    card = Image.new("L", ((cx1 - cx0) * 2, (cy1 - cy0) * 2), 0)
    ImageDraw.Draw(card).rounded_rectangle((0, 0, card.width - 1, card.height - 1), 110, fill=255)
    card = card.resize((cx1 - cx0, cy1 - cy0), Image.LANCZOS)
    im.paste(Image.new("RGB", card.size, "#FFFCF6"), (cx0, cy0), card)
    TEXT, MUTED, ACC = "#2A1F18", "#857360", "#C9553A"
    x = 120
    d.text((x, 220), "ТВОЙ ТИП", font=wf(40), fill=ACC)
    name, desc = st["personality"]
    d.text((x, 290), name, font=_fit(d, name, 840, 130, serif=True, minsize=70), fill=TEXT)
    for k, line in enumerate(_wrap(d, desc, wf(46, False), 840)[:2]):
        d.text((x, 450 + k * 62), line, font=wf(46, False), fill=MUTED)
    d.line((x, 600, WR_W - x, 600), fill="#E9DDCA", width=3)
    top_app = st["apps"][0][0] if st["apps"] else "—"
    peak = f"{st['peak_hour']:02d}:00" if st["peak_hour"] is not None else "—"
    cells = [("ВСЕГО", fmt(st["total"])), ("В СРЕДНЕМ В ДЕНЬ", fmt(st["avg"])),
             ("ТОП-ПРИЛОЖЕНИЕ", top_app), ("СЕРИЯ ДНЕЙ", f"{st['streak']} {plural(st['streak'], 'день', 'дня', 'дней')}"),
             ("РЕКОРДНЫЙ ДЕНЬ", fmt(st["best_day"][1])), ("ПИК АКТИВНОСТИ", peak)]
    for k, (lab, val) in enumerate(cells):
        cx, cy = x + (k % 2) * 450, 650 + (k // 2) * 175
        d.text((cx, cy), lab, font=wf(30), fill=MUTED)
        d.text((cx, cy + 46), val, font=_fit(d, val, 410, 62), fill=TEXT)
    # полоса категорий
    cats = {c: v for c, v in st["cats"].items() if v > 0}
    tot = sum(cats.values()) or 1
    cc = THEMES["light"]["CATS"]
    bx0, bx1, by = x, WR_W - x, 1230
    d.text((x, by - 62), "НА ЧТО УШЛО ВРЕМЯ", font=wf(30), fill=MUTED)
    bar = Image.new("RGB", (bx1 - bx0, 40), "#E2D4BE"); bd = ImageDraw.Draw(bar); px = 0
    for c in CAT_LIST:
        if c in cats:
            w = (bx1 - bx0) * cats[c] / tot
            bd.rectangle((px, 0, px + w, 40), fill=cc[c]); px += w
    bm = Image.new("L", (bar.width * 3, 120), 0)
    ImageDraw.Draw(bm).rounded_rectangle((0, 0, bm.width - 1, 119), 60, fill=255)
    im.paste(bar, (bx0, by), bm.resize(bar.size, Image.LANCZOS))
    legend = sorted(cats.items(), key=lambda kv: -kv[1])[:4]
    for k, (c, v) in enumerate(legend):
        lx, ly = x + (k % 2) * 450, by + 80 + (k // 2) * 64
        d.ellipse((lx, ly + 10, lx + 24, ly + 34), fill=cc[c])
        d.text((lx + 40, ly), f"{c} {int(v / tot * 100)}%", font=wf(38, False), fill=TEXT)
    d.text((WR_W // 2, 1640), "Всё посчитано на твоём компьютере", font=wf(34, False), fill=MUTED, anchor="ma")
    return im


def build_wrapped(st):
    """Список слайдов (PIL-картинки 1080×1920). Пустой список — данных пока мало."""
    if st.get("empty"):
        return []
    makers = [_slide_intro, _slide_apps, _slide_marathon, _slide_rhythm, _slide_night, _slide_final]
    return [mk(st, i, len(makers)) for i, mk in enumerate(makers)]


class Dashboard:
    def __init__(self, app):
        global K
        self.app, self.store, self.root = app, app.store, app.root
        self.win = tk.Toplevel(self.root)
        K = self.win.winfo_fpixels("1i") / 96
        apply_theme(self.store.get("theme", "light"))
        self.win.title("FocusMeter")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h = min(S(1200), sw - 80), min(S(800), sh - 100)
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        self.win.minsize(min(S(1000), sw - 80), min(S(680), sh - 100))
        self.win.configure(bg=P.BG)
        self.win.iconphoto(True, app.logo_tk)
        if app.ico_path:
            try: self.win.iconbitmap(app.ico_path)
            except tk.TclError: pass
            for ms in (100, 700, 2000):      # повторяем: Tk может перебить иконку при показе окна
                self.win.after(ms, lambda: set_window_icon(self.win, app.ico_path))
        self.logo_small = ImageTk.PhotoImage(make_logo(S(36)))
        self.page, self.sig, self._job = "today", {}, None
        self.apps_days, self.week_days, self.search, self.apps_mode = 1, 7, "", "apps"
        self.wr_period, self.wr_idx, self.wr_slides, self.wr_photo = self.store.get("wr_period", "all"), 0, [], None
        self.wr_stats = {}
        self.hour_seconds = [0] * 24

        self._topbar()
        self.content = tk.Frame(self.win, bg=P.BG); self.content.pack(side="left", fill="both", expand=True)
        self.pages = {k: self._safe(f) for k, f in [("today", self._today), ("week", self._week),
                                            ("apps", self._apps), ("wrapped", self._wrapped),
                                            ("settings", self._settings)]}
        self.win.bind_all("<MouseWheel>", self._wheel)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.show("today")
        self._job = self.win.after(3000, self._loop)
    def _safe(self, builder):
        try:
            return builder()
        except Exception:
            log("page: " + traceback.format_exc())
            pg = tk.Frame(self.content, bg=P.BG)
            tk.Label(pg, text="Эта страница не открылась.\nПодробности в error.log", bg=P.BG,
                     fg=P.DANGER, font=fnt(11)).pack(pady=S(40))
            return pg
    def close(self):
        if self._job:
            self.win.after_cancel(self._job)
        try: self.win.unbind_all("<MouseWheel>")
        except tk.TclError: pass
        self.win.destroy()
        self.app.dash = None

    def _loop(self):
        try:
            if self.win.state() != "iconic":
                self.refresh()
        except Exception:
            log("refresh: " + traceback.format_exc())
        self._job = self.win.after(3000, self._loop)

    def _wheel(self, e):
        try:
            w = self.win.winfo_containing(e.x_root, e.y_root)
            while w is not None:
                if getattr(w, "is_scroll", False):
                    w.scroll(e.delta); return
                w = w.master
        except Exception:
            pass

    # ---------- каркас
    def _topbar(self):
        bar = tk.Frame(self.win, bg=P.BG)
        bar.pack(fill="x", padx=S(32))
        bar.columnconfigure(1, weight=1)
        brand = tk.Frame(bar, bg=P.BG)
        brand.grid(row=0, column=0, sticky="w")
        tk.Label(brand, image=self.logo_small, bg=P.BG).pack(side="left")
        tk.Label(brand, text="FocusMeter", font=fnt_serif(16), bg=P.BG, fg=P.TEXT).pack(side="left", padx=S(8))
        tabs = tk.Frame(bar, bg=P.BG)
        tabs.grid(row=0, column=1)
        self.nav = {}
        for key, name in (("today", "День"), ("week", "Неделя"), ("apps", "Приложения"),
                          ("wrapped", "Wrapped"), ("settings", "Настройки")):
            tab = tk.Frame(tabs, bg=P.BG)
            tab.pack(side="left", padx=S(10))
            button = tk.Button(tab, text=name, command=lambda k=key: self.show(k),
                               font=fnt(11), relief="flat", bd=0, bg=P.BG, activebackground=P.BG,
                               activeforeground=P.TEXT, cursor="hand2")
            button.pack(pady=(S(15), S(12)))
            line = tk.Frame(tab, height=S(3), bg=P.BG)
            line.pack(fill="x")
            self.nav[key] = (button, line)
        actions = tk.Frame(bar, bg=P.BG)
        actions.grid(row=0, column=2)
        self.dot = tk.Label(actions, text="●", bg=P.BG, font=fnt(9))
        self.dot.pack(side="left")
        self.status = tk.Label(actions, bg=P.BG, fg=P.MUTED, font=fnt(10))
        self.status.pack(side="left")
        self.pause_button = tk.Button(actions, command=self.app.toggle_pause, font=fnt(10),
                                     bg=P.FILL, fg=P.TEXT, relief="flat", bd=0, cursor="hand2",
                                     activebackground=P.LINE, activeforeground=P.TEXT)
        self.pause_button.pack(side="left", padx=(S(8), 0))
        tk.Frame(self.win, height=1, bg=P.LINE).pack(fill="x")

    def sync_status(self):
        paused, idle = self.app.tracker.paused, self.app.tracker.idle
        self.dot.configure(fg=P.WARN if paused else P.MUTED if idle else P.GREEN)
        self.status.configure(text="На паузе" if paused else "Нет активности" if idle else "Учёт идёт")
        self.pause_button.configure(text="Продолжить" if paused else "Пауза")

    def show(self, name):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for k, (button, line) in self.nav.items():
            button.configure(fg=P.TEXT if k == name else P.MUTED)
            line.configure(bg=P.ACCENT if k == name else P.BG)
        self.page, self.sig = name, {}
        self.refresh()

    def refresh(self):
        self.sync_status()
        {"today": self._r_today, "week": self._r_week, "apps": self._r_apps, "wrapped": self._r_wrapped}.get(self.page, lambda: None)()

    def _header(self, parent, title, sub):
        h = tk.Frame(parent, bg=P.BG); h.pack(fill="x", padx=S(32), pady=(S(26), S(10)))
        left = tk.Frame(h, bg=P.BG); left.pack(side="left")
        d = date.today()
        tk.Label(left, text=f"ВЫПУСК № {d.isocalendar()[1]}  ·  {d.day} {MONTHS[d.month - 1].upper()}",
                 bg=P.BG, fg=P.ACCENT, font=fnt(8, True)).pack(anchor="w")
        tk.Label(left, text=title, bg=P.BG, fg=P.TEXT, font=fnt_serif(28)).pack(anchor="w", pady=(S(2), 0))
        tk.Label(left, text=sub, bg=P.BG, fg=P.MUTED, font=fnt(10)).pack(anchor="w", pady=(S(2), 0))
        right = tk.Frame(h, bg=P.BG); right.pack(side="right")
        tk.Frame(parent, bg=P.LINE, height=1).pack(fill="x", padx=S(32), pady=(0, S(14)))
        return right

    def _stat(self, parent, col, label, glyph, color):
        card = Card(parent, fit=True)
        card.grid(row=0, column=col, sticky="ew", padx=S(0))
        b = card.body
        tk.Label(b, text=label, bg=P.CARD, fg=P.MUTED, font=fnt(8, True)).pack(anchor="w", padx=S(18), pady=(S(16), 0))
        v = tk.Label(b, text="—", bg=P.CARD, fg=P.TEXT, font=fnt_serif(24)); v.pack(anchor="w", padx=S(18), pady=(S(2), 0))
        s = tk.Label(b, text=" ", bg=P.CARD, fg=P.MUTED, font=fnt(8)); s.pack(anchor="w", padx=S(18), pady=(0, S(14)))
        IconTile(b, glyph, color, P.CARD).place(relx=1.0, x=-S(16), y=S(14), anchor="ne")
        card.v, card.s = v, s
        return card

    def _stats_row(self, parent, n):
        row = tk.Frame(parent, bg=P.BG); row.pack(fill="x", padx=S(24), pady=(0, S(2)))
        for i in range(n):
            row.columnconfigure(i, weight=1, uniform="stat")
        return row

    def _title(self, parent, title, sub=""):
        h = tk.Frame(parent, bg=P.CARD); h.pack(fill="x", padx=S(22), pady=(S(18), 0))
        tk.Label(h, text=title, bg=P.CARD, fg=P.TEXT, font=fnt_serif(15)).pack(anchor="w")
        if sub:
            tk.Label(h, text=sub, bg=P.CARD, fg=P.MUTED, font=fnt(9)).pack(anchor="w", pady=(S(2), 0))

    def _app_row(self, parent, name, ratio, t1, t2="", chip=False):
        row = tk.Frame(parent, bg=P.CARD); row.pack(fill="x", padx=S(4), pady=S(7))
        row.columnconfigure(1, weight=1)
        tk.Label(row, image=self.app.icons.get(name), bg=P.CARD).grid(row=0, column=0, rowspan=2, padx=(0, S(12)))
        mid = tk.Frame(row, bg=P.CARD); mid.grid(row=0, column=1, sticky="ew")
        tk.Label(mid, text=name, bg=P.CARD, fg=P.TEXT, font=fnt(10, True)).pack(side="left")
        if chip:
            cat = self.store.classify(name)
            Pill(mid, cat, P.CATS[cat], mix(P.CATS[cat], P.CARD, 0.88), P.CARD, size=8, padx=10, pady=2,
                 command=lambda n=name, c=cat: self._cycle_cat(n, c)).pack(side="left", padx=S(10))
        Bar(row, ratio, P.ACCENT, P.CARD).grid(row=1, column=1, sticky="ew", pady=(S(7), 0))
        right = tk.Frame(row, bg=P.CARD); right.grid(row=0, column=2, rowspan=2, sticky="e", padx=(S(16), 0))
        tk.Label(right, text=t1, bg=P.CARD, fg=P.TEXT, font=fnt(10, True)).pack(anchor="e")
        if t2:
            tk.Label(right, text=t2, bg=P.CARD, fg=P.MUTED, font=fnt(8)).pack(anchor="e")

    def _cycle_cat(self, app, cur):
        self.store.set_category(app, CAT_LIST[(CAT_LIST.index(cur) + 1) % len(CAT_LIST)])
        self.sig = {}; self.refresh()

    # ---------- Сегодня
    def _today(self):
        pg = tk.Frame(self.content, bg=P.BG)
        body = tk.Frame(pg, bg=P.BG)
        body.place(relx=0.5, rely=0, anchor="n", relheight=1)
        pg.bind("<Configure>", lambda e: body.configure(width=min(S(860), max(1, e.width - S(64)))))
        body.pack_propagate(False)
        summary = tk.Frame(body, bg=P.BG)
        summary.pack(fill="x", pady=(S(28), S(22)))
        left = tk.Frame(summary, bg=P.BG)
        left.pack(side="left")
        self.journal_date = tk.Label(left, font=fnt(11), bg=P.BG, fg=P.MUTED)
        self.journal_date.pack(anchor="w")
        tk.Label(left, text="Журнал дня", font=(SERIF, 32), bg=P.BG, fg=P.TEXT).pack(anchor="w")
        self.journal_total = tk.Label(left, font=fnt(14), bg=P.BG, fg=P.TEXT)
        self.journal_total.pack(anchor="w", pady=(S(12), 0))
        self.journal_goal = tk.Label(left, font=fnt(10), bg=P.BG, fg=P.MUTED)
        self.journal_goal.pack(anchor="w")
        self.goal_track = tk.Canvas(left, height=S(4), width=S(275), bg=P.LINE, highlightthickness=0)
        self.goal_track.pack(anchor="w", pady=(S(6), 0))
        right = tk.Frame(summary, bg=P.BG)
        right.pack(side="right", anchor="s")
        tk.Label(right, text="Минуты за экраном по часам", font=fnt(10), bg=P.BG, fg=P.MUTED).pack(anchor="w")
        self.hours_chart = tk.Canvas(right, width=S(320), height=S(95), bg=P.BG, highlightthickness=0)
        self.hours_chart.pack()
        self.hours_chart.bind("<Configure>", lambda e: self._draw_hours())
        tk.Frame(body, bg=P.LINE, height=1).pack(fill="x")
        heading = tk.Frame(body, bg=P.BG)
        heading.pack(fill="x", pady=(S(13), S(9)))
        tk.Label(heading, text="По часам", bg=P.BG, fg=P.TEXT, font=fnt(16, True)).pack(side="left")
        tk.Label(heading, text="Длительность", bg=P.BG, fg=P.MUTED, font=fnt(10)).pack(side="right")
        self.timeline = ScrollFrame(body, P.BG)
        self.timeline.pack(fill="both", expand=True, pady=(0, S(12)))
        self.duration_labels = {}
        return pg

    def _draw_hours(self):
        chart = self.hours_chart
        chart.delete("all")
        width = chart.winfo_width()
        if width < 24:
            return
        active = [hour for hour, seconds in enumerate(self.hour_seconds) if seconds]
        if not active:
            chart.create_text(0, S(42), text="Пока нет почасовых данных", anchor="w",
                              font=fnt(10), fill=P.MUTED)
            return
        hours = range(active[0], active[-1] + 1)
        left, base, height = S(26), S(69), S(44)
        step = (width - left) / len(hours)
        for minutes in (0, 30, 60):
            y = base - height * minutes / 60
            chart.create_line(left, y, width, y, fill=P.LINE)
            chart.create_text(left-S(4), y, text=str(minutes), anchor="e", font=fnt(8), fill=P.MUTED)
        stride = max(1, int((S(37) + step - 1) // step))
        for index, hour in enumerate(hours):
            seconds = self.hour_seconds[hour]
            x = left + (index + 0.5) * step
            bar = min(S(26), step * 0.66) / 2
            if seconds:
                y = base - max(1, height * min(seconds / 3600, 1))
                chart.create_rectangle(x-bar, y, x+bar, base, fill=P.ACCENT, outline="")
                if stride == 1:
                    chart.create_text(x, y-S(2), text=str(seconds//60) if seconds >= 60 else "<1",
                                      anchor="s", font=fnt(8), fill=P.TEXT)
            if index % stride == 0:
                chart.create_text(x, base+S(4), text=f"{hour:02}:00", anchor="n", font=fnt(8), fill=P.MUTED)

    def _r_today(self):
        total = self.store.total_day()
        goal, value = self.store.goal_sec(), self.store.goal_today()
        limit = self.store.goal_mode() == "limit"
        self.journal_date.configure(text=date_title())
        self.journal_total.configure(text=f"{fmt(total)} за экраном")
        self.journal_goal.configure(text=f"Лимит · {fmt(goal)}" if limit else f"Работа и учёба · {fmt(value)} из {fmt(goal)}")
        self.goal_track.delete("all")
        if goal and value:
            self.goal_track.create_rectangle(0, 0, S(275) * min(value/goal, 1), S(4), outline="",
                                             fill=P.DANGER if limit and value > goal else P.ACCENT)
        rows = self.store.q("SELECT hour, app, seconds FROM hourly_usage WHERE day=? AND seconds>0 "
                            "ORDER BY hour DESC, seconds DESC, app", (date.today().isoformat(),))
        hours = [0] * 24
        entries = []
        for hour, app, seconds in rows:
            hours[hour] += seconds
            if seconds >= MIN_APP_SEC:
                entries.append((hour, app, self.store.classify(app)))
        if hours != self.hour_seconds:
            self.hour_seconds = hours
            self._draw_hours()
        signature = (entries, bool(total), bool(rows))
        if self.sig.get("journal") != signature:
            self.sig["journal"] = signature
            self._build_timeline(entries, total)
        for hour, app, seconds in rows:
            if seconds >= MIN_APP_SEC:
                self.duration_labels[hour, app].configure(text=fmt(seconds))

    def _build_timeline(self, entries, total):
        inner = self.timeline.inner
        position = self.timeline.cv.yview()[0]
        self.timeline.clear()
        self.duration_labels.clear()
        if not entries:
            message = "За сегодня сохранился только общий итог." if total else "Здесь появится активность за первый час."
            if any(self.hour_seconds):
                message = "Приложение появится здесь, когда за час наберётся минута."
            tk.Label(inner, text=message, bg=P.BG, fg=P.MUTED, font=fnt(11)).pack(anchor="w", pady=S(18))
            return
        inner.columnconfigure(1, weight=3, uniform="journal")
        inner.columnconfigure(2, weight=2, uniform="journal")
        previous = None
        for i, (hour, app, cat) in enumerate(entries):
            row = 2 * i + 1
            first_in_hour = hour != previous
            if first_in_hour:
                if i:
                    tk.Frame(inner, bg=P.LINE, height=1).grid(row=row-1, column=1, columnspan=3,
                                                              sticky="ew", pady=S(9))
                tk.Label(inner, text=f"{hour:02}:00", bg=P.BG, fg=P.TEXT, font=fnt(13, True)).grid(
                    row=row, column=0, padx=(0, S(46)), sticky="w")
                previous = hour
            rail = tk.Frame(inner, width=S(12), bg=P.BG)
            rail.grid(row=row, column=0, sticky="nse", padx=(0, S(14)))
            tk.Frame(rail, width=1, bg=P.LINE).place(relx=0.5, relheight=1)
            if first_in_hour:
                tk.Label(rail, text="●", bg=P.BG, fg=P.MUTED, font=fnt(7)).place(relx=0.5, rely=0.5, anchor="center")
            name = tk.Frame(inner, bg=P.BG)
            name.grid(row=row, column=1, sticky="ew", pady=S(4))
            tk.Label(name, image=self.app.icons.get(app, compact=True), bg=P.BG, bd=0).pack(side="left")
            tk.Label(name, text=app, bg=P.BG, fg=P.TEXT, font=fnt(12), anchor="w").pack(
                side="left", padx=S(8), fill="x", expand=True)
            category = tk.Frame(inner, bg=P.BG)
            category.grid(row=row, column=2, sticky="w")
            tk.Label(category, text="●", fg=P.CATS[cat], bg=P.BG, font=fnt(8)).pack(side="left")
            tk.Label(category, text=cat, fg=P.MUTED, bg=P.BG, font=fnt(10)).pack(side="left", padx=S(5))
            duration = tk.Label(inner, bg=P.BG, fg=P.TEXT, font=fnt(12), width=11, anchor="e")
            duration.grid(row=row, column=3, sticky="e")
            self.duration_labels[hour, app] = duration
        inner.update_idletasks()
        self.timeline.cv.yview_moveto(position)

    # ---------- Неделя и месяц
    def _week(self):
        pg = tk.Frame(self.content, bg=P.BG)
        right = self._header(pg, "Неделя и месяц", "Как менялась твоя активность")
        Seg(right, P.BG, [(7, "7 дней"), (30, "30 дней")], 7, self._set_week).pack(side="left")
        Seg(right, P.BG, [("line", "Линия"), ("bars", "Столбики"), ("heat", "Тепловая карта")],
            self.store.get("chart_style", "line"), self._set_style).pack(side="left", padx=(S(12), 0))
        row = self._stats_row(pg, 3)
        self.w_total = self._stat(row, 0, "ВСЕГО", "\uE121", P.ACCENT)
        self.w_avg = self._stat(row, 1, "В СРЕДНЕМ В ДЕНЬ", "\uE787", P.ACCENT2)
        self.w_best = self._stat(row, 2, "ЛУЧШИЙ ДЕНЬ", "\uE734", P.WARN)
        card = Card(pg); card.pack(fill="both", expand=True, padx=S(24), pady=(0, S(16)))
        lim = self.store.goal_mode() == "limit"
        self._title(card.body, "Экранное время по дням" if lim else "Работа и учёба по дням",
                    "Пунктир — твой лимит" if lim else "Пунктир — твоя цель")
        self.chart = tk.Canvas(card.body, bg=P.CARD, highlightthickness=0, bd=0)
        self.chart.pack(fill="both", expand=True, padx=S(20), pady=(S(8), S(4)))
        self.chart.bind("<Configure>", lambda e: self._draw_chart())
        self._hits = []
        self.chart.bind("<Motion>", self._tip)
        self.chart.bind("<Leave>", lambda e: self.chart.delete("tip"))
        self.cat_week = CatBar(card.body, P.CARD); self.cat_week.pack(fill="x", padx=S(24), pady=(0, S(22)))
        return pg

    def _set_week(self, n):
        self.week_days = n; self.sig = {}; self.refresh()
    def _set_style(self, k):
        self.store.set("chart_style", k); self.sig = {}; self._draw_chart()
    def _r_week(self):
        data = self.store.daily_goal(self.week_days)
        tot = sum(s for _, s in data)
        best = max(data, key=lambda x: x[1])
        self.w_total.v.configure(text=fmt(tot)); self.w_total.s.configure(text=f"за {self.week_days} дней")
        first = next((i for i, (_, s) in enumerate(data) if s), len(data) - 1)
        n_days = len(data) - first          # дни с момента первой записи
        self.w_avg.v.configure(text=fmt(tot // n_days))
        self.w_avg.s.configure(text=f"за {n_days} дн. с данными" if n_days < self.week_days else "активного времени")
        self.w_best.v.configure(text=f"{WD[best[0].weekday()]}, {best[0].strftime('%d.%m')}" if best[1] else "—")
        self.w_best.s.configure(text=fmt(best[1]) if best[1] else " ")
        self._draw_chart()
        self.cat_week.update_data(self.store.categories(self.week_days))

    def _draw_chart(self):
        c = self.chart
        w, h = c.winfo_width(), c.winfo_height()
        if w < 120 or h < 120:
            return
        style = self.store.get("chart_style", "line")
        if style == "heat":
            return self._draw_heat(w, h)
        data = self.store.daily_goal(self.week_days)
        n = len(data); goal = self.store.goal_sec()
        geo = (S(6), S(6), S(10), S(50) if n <= 7 else S(30))
        key = (tuple(data), w, h, goal, P.name, style)
        if self.sig.get("c") == key:
            return
        self.sig["c"] = key
        im, pos, mx = (line_chart_image if style == "line" else chart_image)(w, h, data, goal, geo)
        self._chart_ph = ImageTk.PhotoImage(im)
        c.delete("all"); c.create_image(0, 0, anchor="nw", image=self._chart_ph)
        slot = (w - geo[0] - geo[1]) / n
        self._hits = []
        for i, ((day, sec), (x, base, _)) in enumerate(zip(data, pos)):
            today = day == date.today()
            if n <= 7:
                c.create_text(x, base + S(16), text=WD[day.weekday()], fill=P.ACCENT if today else P.TEXT, font=fnt(10, True))
                c.create_text(x, base + S(34), text=fmt_short(sec) if sec else "—", fill=P.MUTED, font=fnt(8))
            elif i % 3 == 0 or today:
                c.create_text(x, base + S(14), text=str(day.day), fill=P.ACCENT if today else P.MUTED, font=fnt(8, today))
            self._hits.append((geo[0] + slot * i, 0, geo[0] + slot * (i + 1), h,
                               f"{WD[day.weekday()]}, {day:%d.%m} — {fmt(sec)}"))
        if goal and goal <= mx:
            gy = base - (base - geo[2]) * goal / mx
            c.create_text(w - S(8), gy - S(9), text=f"цель {goal / 3600:g} ч", fill=P.WARN, font=fnt(8, True), anchor="e")

    def _draw_heat(self, w, h):
        c = self.chart
        grid = self.store.heatmap(self.week_days)
        key = ("heat", tuple(map(tuple, grid)), w, h, P.name)
        if self.sig.get("c") == key:
            return
        self.sig["c"] = key
        geo = (S(34), S(6), S(6), S(28))
        im, cw, chh, mx = heatmap_image(w, h, grid, geo)
        self._chart_ph = ImageTk.PhotoImage(im)
        c.delete("all"); c.create_image(0, 0, anchor="nw", image=self._chart_ph)
        pl, pt = geo[0], geo[2]
        self._hits = []
        for r in range(7):
            c.create_text(pl - S(8), pt + chh * (r + 0.5), text=WD[r], anchor="e", fill=P.MUTED, font=fnt(9, True))
            for col in range(24):
                x0, y0 = pl + cw * col, pt + chh * r
                self._hits.append((x0, y0, x0 + cw, y0 + chh, f"{WD[r]}, {col:02d}:00 — {fmt(grid[r][col])}"))
        for hr in (0, 6, 12, 18, 23):
            c.create_text(pl + cw * (hr + 0.5), h - geo[3] + S(14), text=str(hr), fill=P.MUTED, font=fnt(9))

    def _tip(self, e):
        c = self.chart
        c.delete("tip")
        for x0, y0, x1, y1, text in self._hits:
            if x0 <= e.x < x1 and y0 <= e.y < y1:
                t = c.create_text(0, 0, text=text, font=fnt(9, True), anchor="nw")
                bb = c.bbox(t); c.delete(t)
                tw, th = bb[2] - bb[0] + S(24), bb[3] - bb[1] + S(12)
                x = min(max(e.x - tw // 2, S(4)), c.winfo_width() - tw - S(4))
                y = max(e.y - th - S(14), S(4))
                c.create_image(x, y, anchor="nw", image=rr_photo(tw, th, th // 2, P.ACCENT, P.CARD), tags="tip")
                c.create_text(x + tw // 2, y + th // 2, text=text, fill="#FFFFFF", font=fnt(9, True), tags="tip")
                break
            
    # ---------- Приложения
    def _apps(self):
        pg = tk.Frame(self.content, bg=P.BG)
        self._header(pg, "Приложения", "Программы и сайты · нажми на цветную метку, чтобы сменить категорию")
        bar = tk.Frame(pg, bg=P.BG); bar.pack(fill="x", padx=S(32), pady=(0, S(12)))
        Seg(bar, P.BG, [("apps", "Программы"), ("sites", "Сайты")], "apps", self._set_mode).pack(
            side="left", padx=(0, S(16)))
        Seg(bar, P.BG, [(1, "Сегодня"), (7, "7 дней"), (30, "30 дней")], 1, self._set_apps).pack(side="left")
        sc = Card(bar, radius=14, margin=0, shadow=False, fit=True, width=S(300), fill=P.CARD, bg=P.BG)
        sc.pack(side="left", padx=S(16))
        tk.Label(sc.body, text="\uE721", bg=P.CARD, fg=P.MUTED, font=(ICONFONT, 11)).pack(side="left", padx=(S(8), S(6)), pady=S(8))
        self.sv = tk.StringVar(); self.sv.trace_add("write", lambda *a: self._search())
        tk.Entry(sc.body, textvariable=self.sv, bg=P.CARD, fg=P.TEXT, bd=0, insertbackground=P.TEXT,
                 font=fnt(10), relief="flat").pack(side="left", fill="x", expand=True, padx=(0, S(8)), pady=S(8))
        card = Card(pg); card.pack(fill="both", expand=True, padx=S(24), pady=(0, S(16)))
        self.apps_list = ScrollFrame(card.body, P.CARD)
        self.apps_list.pack(fill="both", expand=True, padx=S(20), pady=S(16))
        return pg

    def _set_apps(self, d):
        self.apps_days = d; self.sig = {}; self.refresh()

    def _set_mode(self, m):
        self.apps_mode = m; self.sig = {}; self.refresh()

    def _search(self):
        self.search = self.sv.get().strip().lower(); self.sig = {}; self.refresh()

    def _r_apps(self):
        sites = self.apps_mode == "sites"
        rows = self.store.sites(self.apps_days) if sites else self.store.apps(self.apps_days, 80)
        if self.search:
            rows = [r for r in rows if self.search in r[0].lower()]
        sig = (sites, tuple(rows), tuple(sorted(self.store.over.items())))
        if self.sig.get("a") == sig:
            return
        self.sig["a"] = sig
        self.apps_list.clear()
        if not rows:
            msg = ("Пока нет данных по сайтам.\nСайт появится здесь, когда ты проведёшь на нём 5 минут.\n"
                   "Определяется по заголовку вкладки — адрес и содержимое страниц не читаются."
                   if sites else "Нет данных")
            tk.Label(self.apps_list.inner, text=msg, bg=P.CARD, fg=P.MUTED, font=fnt(10), justify="left").pack(
                pady=S(24), anchor="w")
            return
        tot = sum(s for _, s in rows) or 1
        for a, s in rows:
            self._app_row(self.apps_list.inner, a, s / rows[0][1], fmt(s), f"{int(s / tot * 100)}%", chip=not sites)
        other = getattr(self.store, "sites_other", 0)
        if sites and other >= 60:
            tk.Label(self.apps_list.inner, text=f"Сайт не определён: {fmt(other)} — в заголовках этих вкладок нет названия сайта",
                     bg=P.CARD, fg=P.MUTED, font=fnt(8)).pack(anchor="w", pady=(S(14), 0))

    # ---------- Wrapped
    def _wrapped(self):
        pg = tk.Frame(self.content, bg=P.BG)
        right = self._header(pg, "Wrapped", "Итоги твоего времени за компьютером · листай стрелками или кликом")
        Seg(right, P.BG, WR_PERIODS, self.wr_period, self._set_wr_period).pack(side="left")
        bar = tk.Frame(pg, bg=P.BG); bar.pack(side="bottom", fill="x", padx=S(32), pady=(S(4), S(18)))
        Pill(bar, "←", P.TEXT, P.FILL, P.BG, command=lambda: self._wr_step(-1), padx=18, pady=7, size=11).pack(side="left")
        self.wr_count = tk.Label(bar, text="", bg=P.BG, fg=P.MUTED, font=fnt(10, True), width=8)
        self.wr_count.pack(side="left")
        Pill(bar, "→", P.TEXT, P.FILL, P.BG, command=lambda: self._wr_step(1), padx=18, pady=7, size=11).pack(side="left")
        Pill(bar, "Сохранить все", P.TEXT, P.FILL, P.BG, command=self._wr_save_all, padx=16, pady=8).pack(side="right")
        Pill(bar, "Сохранить слайд", "#FFFFFF", P.ACCENT, P.BG, command=self._wr_save,
             hover=mix(P.ACCENT, "#000000", 0.12), padx=16, pady=8).pack(side="right", padx=(0, S(10)))
        mid = tk.Frame(pg, bg=P.BG); mid.pack(fill="both", expand=True, padx=S(24), pady=(0, S(6)))
        self.wr_mid = mid
        self.wr_left = Card(mid, width=S(250)); self.wr_left.pack(side="left", fill="y")
        self.wr_right = Card(mid, width=S(320)); self.wr_right.pack(side="right", fill="y")
        self.wr_shown = {"left": True, "right": True}
        self.wr_cv = tk.Canvas(mid, bg=P.BG, highlightthickness=0, bd=0)
        self.wr_cv.pack(side="left", fill="both", expand=True, padx=S(12))
        # левая панель: список слайдов
        self._title(self.wr_left.body, "Слайды")
        nav = tk.Frame(self.wr_left.body, bg=P.CARD); nav.pack(fill="x", padx=S(14), pady=(S(10), S(14)))
        self.wr_nav = []
        for i, t in enumerate(WR_TITLES):
            b = Pill(nav, f"{i + 1}  {t}", P.TEXT, P.FILL, P.CARD, command=lambda i=i: self._wr_goto(i),
                     padx=12, pady=7, size=9, min_w=180)
            b.pack(fill="x", pady=S(3)); self.wr_nav.append(b)
        # правая панель: коротко цифрами
        self._title(self.wr_right.body, "Коротко", "Главное за выбранный период")
        self.wr_info = ScrollFrame(self.wr_right.body, P.CARD)
        self.wr_info.pack(fill="both", expand=True, padx=S(14), pady=(S(10), S(14)))
        mid.bind("<Configure>", self._wr_layout)
        self.wr_cv.bind("<Configure>", lambda e: self._wr_draw())
        self.wr_cv.bind("<Button-1>", lambda e: self._wr_step(-1 if e.x < self.wr_cv.winfo_width() / 2 else 1))
        self.win.bind("<Left>", lambda e: self._wr_step(-1))
        self.win.bind("<Right>", lambda e: self._wr_step(1))
        return pg

    def _wr_layout(self, e=None):
        """Узкое окно: боковые панели прячутся, чтобы слайду хватило места"""
        w = self.wr_mid.winfo_width()
        for key, card, side, need in (("left", self.wr_left, "left", S(700)), ("right", self.wr_right, "right", S(1000))):
            want = w >= need
            if want != self.wr_shown[key]:
                if want:
                    card.pack(side=side, fill="y", before=self.wr_cv)
                else:
                    card.pack_forget()
                self.wr_shown[key] = want

    def _set_wr_period(self, k):
        self.wr_period = k
        self.store.set("wr_period", k)
        self.wr_idx = 0
        self.sig = {}
        self.refresh()

    def _r_wrapped(self):
        key = ("wr", self.wr_period, date.today().isoformat())
        if self.sig.get("w") == key:
            return
        self.sig["w"] = key
        try:
            self.wr_stats = wrapped_stats(self.store, wr_since(self.store, self.wr_period))
            self.wr_slides = build_wrapped(self.wr_stats)
        except Exception:
            log("wrapped: " + traceback.format_exc())
            self.wr_stats, self.wr_slides = {}, []
        self.wr_idx = max(0, min(self.wr_idx, len(self.wr_slides) - 1))
        self._wr_fill_info()
        self._wr_draw()

    def _wr_fill_info(self):
        box = self.wr_info
        box.clear()
        st = self.wr_stats
        inner = box.inner
        if not st or st.get("empty"):
            tk.Label(inner, text="Пока слишком мало данных", bg=P.CARD, fg=P.MUTED, font=fnt(10)).pack(anchor="w", pady=S(8))
            return
        name, desc = st["personality"]
        Pill(inner, name, "#FFFFFF", P.ACCENT, P.CARD, padx=16, pady=6, size=11).pack(anchor="w", pady=(S(4), S(4)))
        tk.Label(inner, text=desc, bg=P.CARD, fg=P.MUTED, font=fnt(9), justify="left", wraplength=S(250)).pack(anchor="w")
        day, best = st["best_day"]
        ph = st["peak_hour"]
        items = [("ВСЕГО", fmt(st["total"])), ("В СРЕДНЕМ В ДЕНЬ", fmt(st["avg"])),
                 ("СЕРИЯ ДНЕЙ", f"{st['streak']} {plural(st['streak'], 'день', 'дня', 'дней')}"),
                 ("ТОП-ПРИЛОЖЕНИЕ", st["apps"][0][0] if st["apps"] else "—"),
                 ("ЛЮБИМЫЙ САЙТ", st["site"][0] if st["site"] else "—"),
                 ("ПИК АКТИВНОСТИ", f"{ph:02d}:00–{(ph + 1) % 24:02d}:00" if ph is not None else "—"),
                 ("РЕКОРДНЫЙ ДЕНЬ", f"{_dstr(day)} · {fmt(best)}")]
        for lab, val in items:
            tk.Label(inner, text=lab, bg=P.CARD, fg=P.MUTED, font=fnt(8, True)).pack(anchor="w", pady=(S(12), 0))
            tk.Label(inner, text=val, bg=P.CARD, fg=P.TEXT, font=fnt(11, True), justify="left",
                     wraplength=S(250)).pack(anchor="w")
        tk.Label(inner, text="НА ЧТО УШЛО ВРЕМЯ", bg=P.CARD, fg=P.MUTED, font=fnt(8, True)).pack(anchor="w", pady=(S(16), S(4)))
        cats = sorted(((c, v) for c, v in st["cats"].items() if v > 0), key=lambda kv: -kv[1])
        tot = sum(v for _, v in cats) or 1
        for c, v in cats:
            r = tk.Frame(inner, bg=P.CARD); r.pack(fill="x", pady=S(4))
            tk.Label(r, text=c, bg=P.CARD, fg=P.TEXT, font=fnt(9)).pack(side="left")
            tk.Label(r, text=f"{int(v / tot * 100)}%  ·  {fmt_short(v)}", bg=P.CARD, fg=P.MUTED, font=fnt(9)).pack(side="right")
            Bar(inner, v / tot, P.CATS[c], P.CARD).pack(fill="x")

    def _wr_goto(self, i):
        if self.wr_slides:
            self.wr_idx = max(0, min(len(self.wr_slides) - 1, i))
            self._wr_draw()

    def _wr_step(self, d):
        if self.page != "wrapped" or not self.wr_slides:
            return
        self._wr_goto(self.wr_idx + d)

    def _wr_draw(self):
        for i, b in enumerate(self.wr_nav):
            on = i == self.wr_idx and bool(self.wr_slides)
            b.reconfig(fg="#FFFFFF" if on else P.TEXT, fill=P.ACCENT if on else P.FILL)
        cv = self.wr_cv
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 60 or h < 60:
            return
        cv.delete("all")
        if not self.wr_slides:
            self.wr_count.configure(text="")
            cv.create_text(w // 2, h // 2, fill=P.MUTED, font=fnt(12), justify="center",
                           text="Пока слишком мало данных.\nПоработай за компьютером хотя бы 10 минут — и итоги появятся.")
            return
        self.wr_count.configure(text=f"{self.wr_idx + 1} / {len(self.wr_slides)}")
        ph = h - S(8)
        pw = int(ph * WR_W / WR_H)
        if pw > w - S(8):
            pw = w - S(8); ph = int(pw * WR_H / WR_W)
        im = self.wr_slides[self.wr_idx].resize((pw, ph), Image.LANCZOS)
        mask = Image.new("L", (pw * 3, ph * 3), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw * 3 - 1, ph * 3 - 1), S(24) * 3, fill=255)
        out = Image.new("RGB", (pw, ph), P.BG)
        out.paste(im, (0, 0), mask.resize((pw, ph), Image.LANCZOS))
        self.wr_photo = ImageTk.PhotoImage(out)
        cv.create_image(w // 2, h // 2, image=self.wr_photo)

    def _wr_save(self):
        if not self.wr_slides:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")],
                                            initialfile=f"focusmeter_wrapped_{self.wr_idx + 1}.png")
        if not path:
            return
        try:
            self.wr_slides[self.wr_idx].save(path)
            messagebox.showinfo("Wrapped", f"Сохранено:\n{path}")
        except OSError as e:
            messagebox.showerror("Ошибка", str(e))

    def _wr_save_all(self):
        if not self.wr_slides:
            return
        folder = filedialog.askdirectory(title="Куда сохранить слайды?")
        if not folder:
            return
        try:
            for i, im in enumerate(self.wr_slides, 1):
                im.save(os.path.join(folder, f"focusmeter_wrapped_{i}.png"))
            messagebox.showinfo("Wrapped", f"Сохранено слайдов: {len(self.wr_slides)}\nПапка: {folder}")
        except OSError as e:
            messagebox.showerror("Ошибка", str(e))

    # ---------- Настройки
    def _settings(self):
        pg = tk.Frame(self.content, bg=P.BG)
        self._header(pg, "Настройки", "Настрой ритм работы, уведомления и хранение данных")
        sf = ScrollFrame(pg, P.BG); sf.pack(fill="both", expand=True)
        body = sf.inner

        def card(title, sub=""):
            c = Card(body, fit=True); c.pack(fill="x", padx=S(24), pady=S(5))
            self._title(c.body, title, sub)
            tk.Frame(c.body, bg=P.CARD, height=S(6)).pack()
            return c.body

        def row(parent, title, sub=""):
            r = tk.Frame(parent, bg=P.CARD); r.pack(fill="x", padx=S(22), pady=S(8))
            l = tk.Frame(r, bg=P.CARD); l.pack(side="left")
            tk.Label(l, text=title, bg=P.CARD, fg=P.TEXT, font=fnt(10, True)).pack(anchor="w")
            if sub:
                tk.Label(l, text=sub, bg=P.CARD, fg=P.MUTED, font=fnt(9)).pack(anchor="w")
            rt = tk.Frame(r, bg=P.CARD); rt.pack(side="right")
            return rt

        def end(b):
            tk.Frame(b, bg=P.CARD, height=S(8)).pack()

        s = self.store
        b = card("Общие")
        Seg(row(b, "Тема"), P.CARD, [("light", "Бумага"), ("dark", "Кофе")],
            s.get("theme", "light"), self._set_theme).pack()
        Seg(row(b, "Цель дня", "Набрать часы работы или не превысить лимит"), P.CARD,
            [("work", "Работа и учёба"), ("limit", "Лимит экрана")], s.goal_mode(), self._set_goal_mode).pack()
        Stepper(row(b, "Часов в день"), P.CARD, s.goal_sec() / 3600, 0.5, 1, 16,
                lambda v: f"{v:g} ч", lambda v: s.set("goal_hours", v)).pack()
        Stepper(row(b, "Простой через", "Без мыши и клавиатуры время не идёт"), P.CARD,
                int(float(s.get("idle_min", 1))), 1, 1, 10, lambda v: f"{v} мин",
                lambda v: s.set("idle_min", v)).pack()
        self.auto_sw = Switch(row(b, "Запускать с Windows"), get_autostart(), self._autostart, P.CARD)
        self.auto_sw.pack()
        end(b)

        b = card("Уведомления")
        Switch(row(b, "Напоминать о перерыве"), s.get("break_on", "1") == "1",
               lambda v: s.set("break_on", int(v)), P.CARD).pack()
        Stepper(row(b, "Перерыв через"), P.CARD, int(float(s.get("break_min", 50))), 5, 15, 180,
                lambda v: f"{v} мин", lambda v: s.set("break_min", v)).pack()
        Switch(row(b, "Уведомление о цели"), s.get("goal_notify", "1") == "1",
               lambda v: s.set("goal_notify", int(v)), P.CARD).pack()
        Switch(row(b, "Лимит на развлечения и игры"), s.get("ent_on", "0") == "1",
               lambda v: s.set("ent_on", int(v)), P.CARD).pack()
        Stepper(row(b, "Лимит в день"), P.CARD, int(float(s.get("ent_min", 60))), 15, 15, 480,
                lambda v: f"{v} мин", lambda v: s.set("ent_min", v)).pack()
        end(b)

        b = card("Чёрный список", "Дневной лимит · предупреждение за 15 мин · 2 мин на сохранение · затем закрытие")
        self._bl_form(b)
        self.bl_box = tk.Frame(b, bg=P.CARD); self.bl_box.pack(fill="x", padx=S(22), pady=(0, S(10)))
        self._bl_render()

        b = card("Данные", "Только на этом ПК")
        Stepper(row(b, "Хранить заголовки окон", "Старше останется только сайт или программа"), P.CARD,
                int(float(s.get("keep_days", 90))), 30, 30, 360, lambda v: f"{v} дн.",
                lambda v: s.set("keep_days", v)).pack()
        r = tk.Frame(b, bg=P.CARD); r.pack(fill="x", padx=S(22), pady=(S(6), S(16)))
        for text, cmd, danger in [("Открыть папку", self._open_dir, False), ("Экспорт в CSV", self._export, False),
                                  ("Сбросить сегодня", lambda: self._clear(True), True),
                                  ("Удалить всё", lambda: self._clear(False), True)]:
            Pill(r, text, P.DANGER if danger else P.TEXT, mix(P.DANGER, P.CARD, 0.88) if danger else P.FILL,
                 P.CARD, command=cmd).pack(side="left", padx=(0, S(8)))
        tk.Frame(body, bg=P.BG, height=S(16)).pack()
        return pg
    def _set_theme(self, name):
        self.store.set("theme", name)
        self.root.after(30, self.app.reopen)
    def _set_goal_mode(self, m):
        self.store.set("goal_mode", m)
        self.store.set("goal_done", "")
        self.root.after(30, self.app.reopen)

    def _bl_form(self, parent):
        f = tk.Frame(parent, bg=P.CARD); f.pack(fill="x", padx=S(22), pady=(S(6), S(4)))
        ec = Card(f, radius=14, margin=0, shadow=False, fit=True, width=S(240), fill=P.FILL, bg=P.CARD)
        ec.pack(side="left")
        self.bl_name = tk.StringVar()
        tk.Entry(ec.body, textvariable=self.bl_name, bg=P.FILL, fg=P.TEXT, bd=0, insertbackground=P.TEXT,
                 font=fnt(10), relief="flat").pack(fill="x", padx=S(12), pady=S(8))
        self.bl_min = 60
        Stepper(f, P.CARD, 60, 15, 15, 600, lambda v: f"{v} мин",
                lambda v: setattr(self, "bl_min", v)).pack(side="left", padx=S(14))
        Pill(f, "Добавить", "#FFFFFF", P.ACCENT, P.CARD, command=self._bl_add,
             hover=mix(P.ACCENT, "#000000", 0.12)).pack(side="left")
        r = tk.Frame(parent, bg=P.CARD); r.pack(fill="x", padx=S(22), pady=(S(8), 0))
        Pill(r, "Запущенные сейчас", P.TEXT, P.FILL, P.CARD, size=9, pady=6,
             command=lambda: self._bl_chips(True)).pack(side="left", padx=(0, S(8)))
        Pill(r, "Выбрать .exe…", P.TEXT, P.FILL, P.CARD, size=9, pady=6,
             command=self._bl_browse).pack(side="left")
        self.bl_chips = tk.Frame(parent, bg=P.CARD); self.bl_chips.pack(fill="x", padx=S(22), pady=(S(6), S(10)))
        self._bl_chips(False)
    def _bl_chips(self, scan):
        """Подсказки: запущенные сейчас + часто используемые программы"""
        for w in self.bl_chips.winfo_children():
            w.destroy()
        names = []
        if scan:
            try:
                for a, p in scan_running().items():
                    if not is_protected(p):
                        names.append(a); self.store.exe.setdefault(a, p)
            except Exception:
                log("bl scan: " + traceback.format_exc())
        for a, _ in self.store.apps(30, 20):
            if a not in names and not is_protected(self.store.exe.get(a)):
                names.append(a)
        if not names:
            tk.Label(self.bl_chips, text="Нажми «Запущенные сейчас».",
                     bg=P.CARD, fg=P.MUTED, font=fnt(9)).grid(row=0, column=0, sticky="w")
            return
        for i, a in enumerate(names[:15]):
            Pill(self.bl_chips, a, P.TEXT, P.FILL, P.CARD, size=8, padx=10, pady=3,
                 command=lambda n=a: self.bl_name.set(n)).grid(row=i // 5, column=i % 5, padx=S(3), pady=S(3), sticky="w")

    def _bl_browse(self):
        path = filedialog.askopenfilename(title="Выбери файл .exe программы или игры",
                                          filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")])
        if not path:
            return
        path = os.path.normpath(path)
        if is_protected(path):
            messagebox.showwarning("Нельзя", "Это системная программа, блокировать её нельзя.")
            return
        app = friendly_name(path)
        self.store.set_exe(app, path)
        self.bl_name.set(app)

    def _bl_add(self):
        name = self.bl_name.get().strip()
        if not name:
            return
        known = {a.lower(): a for a in self.store.exe}
        if name.lower() not in known:
            messagebox.showinfo("Не нашёл", "Нажми «Запущенные сейчас» или «Выбрать .exe».")
            return
        name = known[name.lower()]
        if is_protected(self.store.exe.get(name) or name + ".exe"):
            messagebox.showwarning("Нельзя", f"{name}: системный процесс, блокировать его нельзя.")
            return
        self.store.bl_set(name, self.bl_min)
        self.bl_name.set(""); self._bl_render()

    def _bl_render(self):
        for w in self.bl_box.winfo_children():
            w.destroy()
        if not self.store.bl:
            tk.Label(self.bl_box, text="Список пуст", bg=P.CARD, fg=P.MUTED, font=fnt(9)).pack(anchor="w", pady=S(4))
            return
        for app, lim in sorted(self.store.bl.items()):
            r = tk.Frame(self.bl_box, bg=P.CARD); r.pack(fill="x", pady=S(4))
            tk.Label(r, image=self.app.icons.get(app), bg=P.CARD).pack(side="left")
            t = tk.Frame(r, bg=P.CARD); t.pack(side="left", padx=S(12))
            tk.Label(t, text=app, bg=P.CARD, fg=P.TEXT, font=fnt(10, True)).pack(anchor="w")
            tk.Label(t, text=f"лимит {lim} мин · сегодня {fmt(self.store.app_today(app))}",
                     bg=P.CARD, fg=P.MUTED, font=fnt(9)).pack(anchor="w")
            Pill(r, "Убрать", P.DANGER, mix(P.DANGER, P.CARD, 0.88), P.CARD, size=8, pady=4,
                 command=lambda a=app: (self.store.bl_del(a), self._bl_render())).pack(side="right")

    def _autostart(self, on):
        if not set_autostart(on):
            self.auto_sw.on = not on; self.auto_sw._draw()

    def _open_dir(self):
        os.startfile(APP_DIR)

    def _export(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=f"focusmeter_{date.today().isoformat()}.csv")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f); w.writerow(["Дата", "Приложение", "Окно", "Категория", "Секунды"])
                for d, a, t, s in self.store.q("SELECT day,app,title,seconds FROM usage ORDER BY day DESC, seconds DESC"):
                    w.writerow([csv_text(d), csv_text(a), csv_text(t), csv_text(self.store.classify(a, t)), s])
            messagebox.showinfo("Экспорт", f"Сохранено:\n{path}")
        except OSError as e:
            messagebox.showerror("Ошибка", str(e))

    def _clear(self, today):
        if messagebox.askyesno("Подтверди", "Удалить данные за сегодня?" if today else "Удалить ВСЕ данные? Это необратимо."):
            self.store.clear(today); self.sig = {}; self.refresh()


# ------------------------------------------------------------------ Приложение + трей
class App:
    def __init__(self, root, background):
        import pystray
        self.root, self.dash = root, None
        self.store = Store()
        self.icons = IconCache()
        self.cmds = queue.Queue()
        self.logo_tk = ImageTk.PhotoImage(make_logo(64))
        self.ico_path = os.path.join(APP_DIR, "focusmeter.ico")
        try:
            make_logo(256).save(self.ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
                                bitmap_format="bmp")
        except Exception:
            log("ico: " + traceback.format_exc()); self.ico_path = None
        menu = pystray.Menu(
            pystray.MenuItem("Открыть FocusMeter", lambda: self.cmds.put("open"), default=True),
            pystray.MenuItem("Пауза", lambda: self.cmds.put("pause"), checked=lambda i: self.tracker.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выйти", lambda: self.cmds.put("quit")))
        self.tray = pystray.Icon("FocusMeter", make_logo(64), "FocusMeter", menu)
        self.tracker = Tracker(self.store, self.notify, self.set_tip)
        self.tray.run_detached()
        self.tracker.start()
        self.root.after(300, self._poll)
        try:
            if self.store.get("autostart_init", "") == "":
                self.store.set("autostart_init", "1")
            elif get_autostart():
                set_autostart(True)          # обновляет путь, если папку перенесли
        except Exception:
            log("autostart: " + traceback.format_exc())
        if not background:
            self.open()

    def notify(self, title, msg):
        try: self.tray.notify(msg, title)
        except Exception: pass

    def set_tip(self, text):
        try: self.tray.title = text
        except Exception: pass

    def open(self):
        if self.dash:
            self.dash.win.deiconify(); self.dash.win.lift(); self.dash.win.focus_force()
        else:
            try:
                self.dash = Dashboard(self)
            except Exception:
                log("dashboard: " + traceback.format_exc())
                self.dash = None
                messagebox.showerror("FocusMeter", "Не удалось открыть окно. Подробности в файле:\n" + LOG_PATH)

    def reopen(self):
        if self.dash:
            self.dash.close()
        self.root.after(60, self.open)

    def toggle_pause(self):
        self.tracker.paused = not self.tracker.paused
        if self.dash:
            self.dash.sync_status()
        try: self.tray.update_menu()
        except Exception: pass

    def _poll(self):
        try:
            while True:
                c = self.cmds.get_nowait()
                if c == "open": self.open()
                elif c == "pause": self.toggle_pause()
                elif c == "quit": return self.quit()
        except queue.Empty:
            pass
        self.root.after(300, self._poll)

    def quit(self):
        self.tracker.stop_ev.set()
        self.tracker.join(timeout=3)
        self.store.flush()
        try: self.tray.stop()
        except Exception: pass
        self.root.quit()


def already_running():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW(None, False, "Local\\FocusMeterSingleInstance")
    return ctypes.get_last_error() == 183


def main():
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception: pass
    try: shell32.SetCurrentProcessExplicitAppUserModelID("FocusMeter.App")
    except Exception: pass
    if already_running():
        user32.MessageBoxW(0, "FocusMeter уже запущен — ищи значок в трее (стрелка ^ справа внизу).", "FocusMeter", 0x40)
        return
    try:
        import pystray  # noqa
        if not HAVE_PIL:
            raise ImportError
    except ImportError:
        user32.MessageBoxW(0, "Не хватает библиотек. Открой командную строку и выполни:\n\n"
                           "python -m pip install pystray pillow", "FocusMeter", 0x10)
        return
    root = tk.Tk(); root.withdraw()
    root.report_callback_exception = lambda *a: log("tk: " + "".join(traceback.format_exception(*a)))
    App(root, background="--background" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("FocusMeter работает только в Windows 10/11."); sys.exit(1)
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        log("fatal: " + err)
        try:
            user32.MessageBoxW(0, err[:1500], "FocusMeter — ошибка", 0x10)
        except Exception:
            print(err)