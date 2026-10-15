"""Refresh the screenshots in docs/ by capturing the app's own windows.

Run ``screenshot.bat``, or this file directly:

    py -3.13 docs\\capture_screenshots.py

It starts the UI with DISVBC_NO_AUTOSTART=1 so nothing gets signed in - that
keeps it from knocking your running sessions offline - waits for the windows to
finish drawing, then grabs the pixels and writes:

    docs/Dashboard.png    main window, Dashboard tab
    docs/Account.png      main window, Accounts tab
    docs/Join Server.png  main window, Join Server tab
    docs/Voice.png        main window, Voice tab
    docs/Live Listen.png  Live Listen & Members window
    docs/Routing.png      Routing window

Note that these overwrite the files of the same name, so if you press the
buttons in the captured window first you will get different pictures.

Everything here is standard library: ctypes for the Win32 drawing calls and
zlib to write the PNG. No Pillow, nothing to install.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import tempfile
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

# Never sign in from here: it would kick whatever sessions are already open,
# and the screenshots would be full of someone's real servers.
os.environ["DISVBC_NO_AUTOSTART"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Win32 calls - without the argtypes/restype below, ctypes truncates the 64-bit
# handles to 32 bits on 64-bit Python and everything falls apart.
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# Without this Windows reports the wrong window size on a scaled display and
# the capture comes out cropped.
for _mode in (2, 1):                       # 2 = per-monitor v2, 1 = system
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_mode)
        break
    except Exception:
        try:
            user32.SetProcessDPIAware()
            break
        except Exception:
            pass

HWND = ctypes.c_void_p
HANDLE = ctypes.c_void_p

user32.GetWindowDC.argtypes = [HWND]
user32.GetWindowDC.restype = HANDLE
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HANDLE
user32.ReleaseDC.argtypes = [HWND, HANDLE]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.PrintWindow.argtypes = [HWND, HANDLE, wt.UINT]
user32.PrintWindow.restype = wt.BOOL

gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateCompatibleDC.restype = HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.SelectObject.restype = HANDLE
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [HANDLE]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.BitBlt.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, HANDLE, ctypes.c_int, ctypes.c_int,
                         wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [HANDLE, HANDLE, wt.UINT, wt.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int

SRCCOPY = 0x00CC0020
PW_RENDERFULLCONTENT = 0x00000002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _pixels(mem, bmp, w, h):
    """Read a GDI bitmap back as top-down RGB rows."""
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h                      # negative means top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0                  # BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) == 0:
        raise RuntimeError("GetDIBits failed")

    raw = buf.raw
    rows = []
    for y in range(h):
        line = raw[y * w * 4:(y + 1) * w * 4]      # one pixel is B,G,R,pad
        row = bytearray(w * 3)
        row[0::3] = line[2::4]                     # keep R,G,B
        row[1::3] = line[1::4]
        row[2::3] = line[0::4]
        rows.append(bytes(row))
    return rows


def _blank(rows):
    return all(byte == 0 for row in rows for byte in row)


def capture(hwnd):
    """Return (width, height, rgb_rows) for a window handle."""
    rect = wt.RECT()
    if not user32.GetWindowRect(HWND(hwnd), ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w < 2 or h < 2:
        raise RuntimeError(f"window has no usable size ({w}x{h})")

    src = user32.GetWindowDC(HWND(hwnd))
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # PW_RENDERFULLCONTENT asks for the real pixels, even if another window
        # is sitting on top of ours.
        user32.PrintWindow(HWND(hwnd), mem, PW_RENDERFULLCONTENT)
        rows = _pixels(mem, bmp, w, h)

        if _blank(rows):
            # Some drivers just hand back an empty bitmap. Copy the screen
            # instead - the caller has already lifted the window to the front.
            print("   (PrintWindow returned an empty bitmap, copying screen)")
            screen = user32.GetDC(None)
            gdi32.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, SRCCOPY)
            user32.ReleaseDC(None, screen)
            rows = _pixels(mem, bmp, w, h)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(HWND(hwnd), src)
    return w, h, rows


def write_png(path, w, h, rows):
    """Write 8-bit RGB rows as a PNG. zlib does the hard part."""
    raw = bytearray()
    for row in rows:
        raw.append(0)                      # filter type: none
        raw += row

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8-bit truecolour

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        fh.write(chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# poking the app
# ---------------------------------------------------------------------------
def pump(win, seconds):
    """Let Tk breathe for a bit so the window actually paints."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            win.update()
        except Exception:
            return
        time.sleep(0.02)


def raise_window(win, settle=0.7):
    try:
        win.deiconify()
    except Exception:
        pass
    try:
        win.lift()
    except Exception:
        pass
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    pump(win, settle)
    try:
        win.attributes("-topmost", False)
    except Exception:
        pass


def ignore_close(win):
    """Swallow a WM_DELETE_WINDOW for the duration of the capture.

    Windows occasionally posts a close request to a window that nobody is
    touching, and App.on_close turns that into a full shut-down - which kills
    the run half way through and leaves the last few screenshots missing.
    """
    try:
        win.protocol("WM_DELETE_WINDOW", lambda: None)
    except Exception:
        pass


def shoot(win, name):
    raise_window(win)
    w, h, rows = capture(win.winfo_id())
    write_png(os.path.join(DOCS, name), w, h, rows)
    print(f"[shots] docs/{name}  {w}x{h}")


def main():
    import main as vh

    os.makedirs(DOCS, exist_ok=True)

    # These shots end up in a public repository, so point the app at an empty
    # account list instead of the real accounts.json. No account shows up in
    # the window and nothing gets written back to the real file.
    blank = os.path.join(tempfile.gettempdir(), "voicehive_shots_accounts.json")
    try:
        with open(blank, "w", encoding="utf-8") as fh:
            fh.write("[]")
        vh.ACCOUNTS_FILE = blank
    except Exception:
        pass

    app = vh.App()
    ignore_close(app)
    raise_window(app, 1.2)

    written = 0
    for attr, name in (
        ("tab_dash", "Dashboard.png"),
        ("tab_accounts", "Account.png"),
        ("tab_join", "Join Server.png"),
        ("tab_voice", "Voice.png"),
    ):
        frame = getattr(app, attr, None)
        if frame is None:
            continue
        try:
            app.nb.select(frame)
            pump(app, 0.6)
            shoot(app, name)
            written += 1
        except Exception as exc:
            print(f"[shots] skipped {name}: {exc}")
        ignore_close(app)

    for opener, attr, name in (
        ("_open_monitor_window", "_monitor_win", "Live Listen.png"),
        ("_open_routing_window", "_routing_win", "Routing.png"),
    ):
        try:
            getattr(app, opener)()
            pump(app, 1.4)
            win = getattr(app, attr, None)
            if win is not None:
                ignore_close(win)
                shoot(win, name)
                written += 1
        except Exception as exc:
            print(f"[shots] skipped {name}: {exc}")

    try:
        app.destroy()
    except Exception:
        pass
    print(f"[shots] {written} screenshot(s) written to docs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
