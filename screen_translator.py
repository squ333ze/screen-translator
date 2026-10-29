"""
Экранный переводчик EN -> RU поверх любой игры (Roblox и др.).
Работает только на Windows 10/11. Ничего не внедряет в игру:
делает снимок экрана, распознаёт текст встроенным OCR Windows,
переводит и рисует перевод в прозрачном окне поверх экрана.

Установка:
    pip install mss pillow winocr deep-translator keyboard

Запуск:
    python screen_translator.py

Горячие клавиши:
    F8 - включить / выключить перевод
    F9 - выйти
"""

import ctypes
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import keyboard
import mss
import winocr
from deep_translator import GoogleTranslator
from PIL import Image

# ---------------- Настройки ----------------
MONITOR = 1            # номер монитора: 1 = основной
OCR_LANG = "en"        # язык распознавания
SCAN_INTERVAL = 0.8    # пауза между сканированиями, сек
GAP_FACTOR = 1.6       # во сколько высот строки пробел делит текст на отдельные блоки
BG_COLOR = "#141414"   # фон плашки с переводом
FG_COLOR = "#ffffff"   # цвет текста перевода
FONT_FAMILY = "Segoe UI"
# -------------------------------------------

KEY_COLOR = "#ff00ff"  # цвет прозрачности окна-оверлея

# Чтобы координаты совпадали с пикселями экрана при масштабировании Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

user32 = ctypes.windll.user32

running = True
enabled = True
result_q = queue.Queue()


def has_latin(text):
    return re.search(r"[A-Za-z]", text) is not None


def mostly_cyrillic(text):
    return len(re.findall(r"[А-Яа-яЁё]", text)) > len(text) / 3


def split_line(words):
    """Делит строку OCR на блоки, если между словами большой разрыв
    (например, две кнопки в одном ряду)."""
    groups, current = [], []
    for w in words:
        r = w["bounding_rect"]
        if current:
            prev = current[-1]["bounding_rect"]
            gap = r["x"] - (prev["x"] + prev["width"])
            if gap > GAP_FACTOR * max(r["height"], prev["height"]):
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def group_to_item(group):
    text = " ".join(w["text"] for w in group).strip()
    x1 = min(w["bounding_rect"]["x"] for w in group)
    y1 = min(w["bounding_rect"]["y"] for w in group)
    x2 = max(w["bounding_rect"]["x"] + w["bounding_rect"]["width"] for w in group)
    y2 = max(w["bounding_rect"]["y"] + w["bounding_rect"]["height"] for w in group)
    return x1, y1, x2, y2, text


def worker():
    translator = GoogleTranslator(source="en", target="ru")
    cache = {}
    was_enabled = True

    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR]
        while running:
            if not enabled:
                if was_enabled:
                    result_q.put([])
                    was_enabled = False
                time.sleep(0.2)
                continue
            was_enabled = True

            try:
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                res = winocr.recognize_pil_sync(img, OCR_LANG)
            except Exception as e:
                print("Ошибка OCR:", e)
                time.sleep(2)
                continue

            items = []
            for line in res.get("lines", []):
                for group in split_line(line.get("words", [])):
                    x1, y1, x2, y2, text = group_to_item(group)
                    if len(text) < 2 or not has_latin(text) or mostly_cyrillic(text):
                        continue
                    items.append((x1, y1, x2, y2, text))

            out = []
            for x1, y1, x2, y2, text in items:
                if text not in cache:
                    try:
                        cache[text] = translator.translate(text) or text
                    except Exception as e:
                        print("Ошибка перевода:", e)
                        cache[text] = None
                if cache[text]:
                    out.append((x1, y1, x2, y2, cache[text]))

            result_q.put(out)
            time.sleep(SCAN_INTERVAL)


def make_overlay(monitor):
    root = tk.Tk()
    root.title("screen_translator_overlay")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY_COLOR)
    root.geometry(f"{monitor['width']}x{monitor['height']}+{monitor['left']}+{monitor['top']}")

    canvas = tk.Canvas(root, bg=KEY_COLOR, highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()

    # Окно не принимает клики (клики проходят в игру) и не появляется в Alt+Tab
    GWL_EXSTYLE = -20
    WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    # Окно не попадает в снимки экрана, поэтому OCR не читает собственный перевод
    WDA_EXCLUDEFROMCAPTURE = 0x11
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        print("Предупреждение: не удалось скрыть оверлей от захвата (нужна Windows 10 версии 2004+).")

    return root, canvas


def draw(canvas, items):
    canvas.delete("all")
    for x1, y1, x2, y2, text in items:
        h = max(y2 - y1, 10)
        w = x2 - x1
        size = max(9, int(h * 0.75))
        font = tkfont.Font(family=FONT_FAMILY, size=-size, weight="bold")
        # уменьшаем шрифт, если русский текст сильно шире исходного
        while size > 9 and font.measure(text) > w * 1.35:
            size -= 1
            font.configure(size=-size)
        tw = font.measure(text)
        pad = 3
        canvas.create_rectangle(x1 - pad, y1 - pad, x1 + max(w, tw) + pad, y1 + h + pad,
                                fill=BG_COLOR, outline="")
        canvas.create_text(x1, y1 + h / 2, text=text, fill=FG_COLOR, font=font, anchor="w")


def main():
    global running, enabled

    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR]

    root, canvas = make_overlay(monitor)

    def toggle():
        global enabled
        enabled = not enabled
        print("Перевод включён" if enabled else "Перевод выключен")

    def quit_app():
        global running
        running = False

    keyboard.add_hotkey("f8", toggle)
    keyboard.add_hotkey("f9", quit_app)

    threading.Thread(target=worker, daemon=True).start()
    print("Запущено. F8 - вкл/выкл, F9 - выход.")

    def poll():
        latest = None
        try:
            while True:
                latest = result_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            draw(canvas, latest)
        if not running:
            root.destroy()
            return
        root.after(100, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
