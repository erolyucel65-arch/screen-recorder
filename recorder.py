"""
Ekranda seçtiğin bir bölgeyi, sistem sesiyle birlikte kaydeden araç (Windows).

Kısayollar (varsayılan):
  F8  - Kayıt bölgesini seç / değiştir (fareyle sürükle)
  F9  - Kaydı başlat (3-2-1 geri sayımdan sonra başlar)
  F10 - Kaydı durdur ve dosyayı kaydet
  F12 - Programdan çık

Çalıştırma:
    venv\\Scripts\\python.exe recorder.py
"""

import os
import time
import ctypes
import threading
import subprocess
import tkinter as tk
from datetime import datetime
from pathlib import Path

import numpy as np
import mss
import keyboard
import pyaudiowpatch as pyaudio
import soundfile as sf
import imageio
import imageio_ffmpeg

# Tkinter'ın piksel koordinatlarını mss/BitBlt'in kullandığı gerçek (fiziksel)
# ekran piksellerine eşitler; aksi halde DPI ölçeklendirmesinde (örn. %125/%150)
# seçilen bölge ekrandan taşar ve kayıt BitBlt hatasıyla çöker.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------- ayarlar
HOTKEY_SELECT = "f8"
HOTKEY_START = "f9"
HOTKEY_STOP = "f10"
HOTKEY_QUIT = "f12"

FPS = 20
COUNTDOWN_FROM = 3

OUTPUT_DIR = Path.home() / "Videos" / "ScreenRecordings"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ durum
class State:
    region = None
    recording = False
    stop_event = threading.Event()
    video_thread = None
    audio_thread = None
    video_path = None
    audio_path = None
    final_path = None
    indicator_win = None
    audio_ok = True


state = State()

root = tk.Tk()
root.withdraw()


# ---------------------------------------------------------- bölge seçimi
def select_region():
    if state.recording:
        print("Kayıt sürerken bölge değiştirilemez.")
        return

    with mss.mss() as sct:
        mon = sct.monitors[0]  # tüm monitörleri kapsayan sınır kutusu

    overlay = tk.Toplevel(root)
    overlay.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    overlay.attributes("-alpha", 0.25)
    overlay.configure(bg="black")
    overlay.config(cursor="cross")

    canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        mon["width"] // 2, 30,
        text="Kaydedilecek bölgeyi fareyle sürükleyerek seçin  (Esc: iptal)",
        fill="white", font=("Segoe UI", 14, "bold"),
    )

    sel = {"x0": 0, "y0": 0, "rect": None}

    def on_press(event):
        sel["x0"], sel["y0"] = event.x, event.y
        sel["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00ff66", width=2
        )

    def on_drag(event):
        if sel["rect"] is not None:
            canvas.coords(sel["rect"], sel["x0"], sel["y0"], event.x, event.y)

    def on_release(event):
        x0, y0 = sel["x0"], sel["y0"]
        x1, y1 = event.x, event.y
        left, top = min(x0, x1), min(y0, y1)
        width, height = abs(x1 - x0), abs(y1 - y0)
        overlay.destroy()
        if width < 10 or height < 10:
            print("Seçim çok küçük, bölge değiştirilmedi.")
            return
        width -= width % 2
        height -= height % 2
        state.region = {
            "left": mon["left"] + left,
            "top": mon["top"] + top,
            "width": max(2, width),
            "height": max(2, height),
        }
        print(f"Bölge seçildi: {state.region}")

    def on_escape(event):
        overlay.destroy()
        print("Bölge seçimi iptal edildi.")

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_escape)
    overlay.focus_force()


# ------------------------------------------------------------- geri sayım
def show_countdown(done_callback):
    region = state.region
    cx = region["left"] + region["width"] // 2
    cy = region["top"] + region["height"] // 2

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="black")
    size = 160
    win.geometry(f"{size}x{size}+{cx - size // 2}+{cy - size // 2}")
    try:
        win.attributes("-alpha", 0.75)
    except tk.TclError:
        pass

    label = tk.Label(
        win, text=str(COUNTDOWN_FROM), fg="white", bg="black",
        font=("Segoe UI", 64, "bold"),
    )
    label.pack(fill="both", expand=True)

    def tick(n):
        if n <= 0:
            win.destroy()
            done_callback()
            return
        label.config(text=str(n))
        root.after(1000, tick, n - 1)

    tick(COUNTDOWN_FROM)


# ---------------------------------------------------------------- gösterge
def show_indicator():
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="black")
    screen_w = root.winfo_screenwidth()
    win.geometry(f"110x34+{screen_w - 130}+10")
    tk.Label(
        win, text="● REC", fg="#ff3b30", bg="black",
        font=("Segoe UI", 14, "bold"),
    ).pack(fill="both", expand=True)
    state.indicator_win = win


def hide_indicator():
    if state.indicator_win is not None:
        state.indicator_win.destroy()
        state.indicator_win = None


# -------------------------------------------------------------- video işi
def video_worker(region, path, fps, stop_event):
    writer = imageio.get_writer(
        str(path), fps=fps, codec="libx264",
        quality=8, macro_block_size=2,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    interval = 1.0 / fps
    try:
        with mss.mss() as sct:
            while not stop_event.is_set():
                t0 = time.time()
                img = sct.grab(region)
                frame = np.ascontiguousarray(np.asarray(img)[:, :, [2, 1, 0]])
                writer.append_data(frame)
                dt = time.time() - t0
                time.sleep(max(0.0, interval - dt))
    finally:
        writer.close()


# -------------------------------------------------------------- ses işi
AUDIO_OPEN_TIMEOUT = 5  # saniye - cihaz açılmazsa sessiz devam et


def audio_worker(path, stop_event):
    pa = pyaudio.PyAudio()
    frames = []
    holder = {"stream": None, "error": None, "rate": None, "channels": None}

    def callback(in_data, frame_count, time_info, status):
        frames.append(in_data)
        return (in_data, pyaudio.paContinue)

    def opener():
        try:
            device = pa.get_default_wasapi_loopback()
        except Exception as exc:
            holder["error"] = exc
            return

        candidates = [
            (int(device["defaultSampleRate"]), device["maxInputChannels"]),
            (48000, device["maxInputChannels"]),
            (44100, device["maxInputChannels"]),
            (int(device["defaultSampleRate"]), 1),
        ]
        tried = set()
        for rate, channels in candidates:
            if (rate, channels) in tried:
                continue
            tried.add((rate, channels))
            try:
                holder["rate"] = rate
                holder["channels"] = channels
                holder["stream"] = pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=1024,
                    input=True,
                    input_device_index=device["index"],
                    stream_callback=callback,
                )
                return
            except Exception as exc:
                holder["error"] = exc
                time.sleep(0.3)

    opener_thread = threading.Thread(target=opener, daemon=True)
    opener_thread.start()
    opener_thread.join(timeout=AUDIO_OPEN_TIMEOUT)

    if holder["stream"] is None:
        reason = holder["error"] if holder["error"] else "zaman aşımı"
        print(f"Sistem sesi açılamadı ({reason}); bu kayıt sessiz olacak.")
        state.audio_ok = False
        if not opener_thread.is_alive():
            try:
                pa.terminate()
            except Exception:
                pass
        return

    stream = holder["stream"]
    stream.start_stream()
    stop_event.wait()
    stream.stop_stream()
    stream.close()
    pa.terminate()

    if frames:
        raw = b"".join(frames)
        data = np.frombuffer(raw, dtype=np.int16).reshape(-1, holder["channels"])
        sf.write(str(path), data, holder["rate"])
    else:
        state.audio_ok = False


# ------------------------------------------------------------------ kontrol
def begin_recording():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    state.video_path = OUTPUT_DIR / f"rec_{ts}_video.mp4"
    state.audio_path = OUTPUT_DIR / f"rec_{ts}_audio.wav"
    state.final_path = OUTPUT_DIR / f"rec_{ts}.mp4"
    state.stop_event = threading.Event()
    state.audio_ok = True

    state.video_thread = threading.Thread(
        target=video_worker,
        args=(state.region, state.video_path, FPS, state.stop_event),
        daemon=True,
    )
    state.audio_thread = threading.Thread(
        target=audio_worker,
        args=(state.audio_path, state.stop_event),
        daemon=True,
    )

    state.recording = True
    state.video_thread.start()
    state.audio_thread.start()
    show_indicator()
    print(f"Kayıt başladı -> {state.final_path.name}")


def start_recording():
    if state.recording:
        print("Zaten kayıt yapılıyor.")
        return
    if state.region is None:
        print("Önce bölge seçin (F8).")
        select_region()
        return
    show_countdown(begin_recording)


def stop_recording():
    if not state.recording:
        print("Kayıt zaten durmuş durumda.")
        return

    state.stop_event.set()
    hide_indicator()

    def finalize():
        state.video_thread.join()
        state.audio_thread.join()
        state.recording = False

        video_path = state.video_path
        audio_path = state.audio_path
        final_path = state.final_path

        if state.audio_ok and audio_path.exists() and audio_path.stat().st_size > 44:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe, "-y",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest", str(final_path),
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0:
                video_path.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)
            else:
                print("Ses/görüntü birleştirilemedi; video dosyası sessiz olarak kaldı:")
                print(video_path)
                print(result.stderr.decode(errors="ignore")[-800:])
                return
        else:
            video_path.replace(final_path)
            if audio_path.exists():
                audio_path.unlink(missing_ok=True)

        print(f"Kayıt kaydedildi: {final_path}")

    threading.Thread(target=finalize, daemon=True).start()


def quit_app():
    if state.recording:
        stop_recording()
        time.sleep(1.0)
    print("Çıkılıyor...")
    os._exit(0)


# -------------------------------------------------------------------- main
def main():
    print("Ekran bölgesi + ses kaydedici")
    print(f"  {HOTKEY_SELECT.upper()}  : Kayıt bölgesini seç")
    print(f"  {HOTKEY_START.upper()}  : Kaydı başlat (3-2-1 geri sayım ile)")
    print(f"  {HOTKEY_STOP.upper()} : Kaydı durdur ve kaydet")
    print(f"  {HOTKEY_QUIT.upper()} : Programdan çık")
    print(f"Kayıtlar şuraya yazılacak: {OUTPUT_DIR}")

    keyboard.add_hotkey(HOTKEY_SELECT, lambda: root.after(0, select_region))
    keyboard.add_hotkey(HOTKEY_START, lambda: root.after(0, start_recording))
    keyboard.add_hotkey(HOTKEY_STOP, lambda: root.after(0, stop_recording))
    keyboard.add_hotkey(HOTKEY_QUIT, lambda: root.after(0, quit_app))

    root.mainloop()


if __name__ == "__main__":
    main()
