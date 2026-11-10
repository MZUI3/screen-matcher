import base64
import contextlib
import importlib.util
import ctypes
import datetime
import json
import os
import queue
import sys
import threading
import time
import winsound
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import mss
import numpy as np

try:
    import sv_ttk  # Windows 11 스타일 ttk 테마 (없어도 기본 테마로 동작)
except Exception:  # pragma: no cover
    sv_ttk = None

THEME = {
    "dark": dict(bg="#1c1c1c", card="#2b2b2b", fg="#f2f2f2", sub="#9a9a9a", log_bg="#171717", thumb="#1e1e1e", good="#1f6f3a",
                 accent="#60cdff", header="#0f172a", err="#ff7b7b", ok="#4ade80", iter="#7cb7ff",
                 start="#16a34a", start_hover="#15803d", stop="#dc2626", stop_hover="#b91c1c"),
    "light": dict(bg="#f3f3f3", card="#ffffff", fg="#1b1b1b", sub="#6b6b6b", log_bg="#ffffff", thumb="#e6e6e6", good="#bfe8c8",
                  accent="#005fb8", header="#0f172a", err="#b00020", ok="#1b7f2a", iter="#1a4d8f",
                  start="#16a34a", start_hover="#15803d", stop="#dc2626", stop_hover="#b91c1c"),
}
COL = THEME["dark"]

CATEGORIES = ["중요", "보통", "기타"]
VK_F9, VK_RETURN, VK_MENU = 0x78, 0x0D, 0x12

# ---- 모드 / 입력 / 캡처 선택지 ----
MODES = [("generic", "이미지 매칭 (기본 · 어떤 프로그램이든)")]  # presets/*.py 가 있으면 뒤에 추가된다
INPUTS = [("auto", "자동 (백그라운드 → 안 되면 포그라운드)"), ("bg", "백그라운드 (창에 직접 전달, 마우스 안 씀)"), ("fg", "포그라운드 (실제 마우스·키보드)")]
CAPS = [("auto", "자동 (보이면 화면, 가려지면 창 직접)"), ("window", "창 직접 (가려져도 됨, 깜빡일 수 있음)"), ("screen", "화면 (깜빡임 없음)")]
CONFIRMS = [("none", "없음"), ("enter", "Enter 키"), ("click", "위치 클릭")]

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
ASSETS = os.path.join(getattr(sys, "_MEIPASS", BASE), "assets")
ICON_DIR = os.path.join(BASE, "icons")
CAP_DIR = os.path.join(BASE, "captures")
SETTINGS = os.path.join(BASE, "settings.json")

DEFAULT_CFG = {
    "area": "주 모니터 전체", "area_exe": None, "rect": None,
    "mode": "generic", "input": "auto", "capture": "auto", "hold_mouse": True, "idle_wait": 1.0, "theme": "dark",
    "use_value": True, "value": 10, "use_cat_a": False, "cat_a": 1, "use_cat_b": False, "cat_b": 1,
    "cat_a_name": "중요", "cat_b_name": "보통",
    "logic": "OR", "max_iter": 500, "wait_after": 1.0,
    "g_action": None, "g_confirm": "enter", "g_confirm_pos": None, "g_confirm_delay": 0.6, "g_thr": 0.8,
}


def imread(path):
    try:
        return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite(path, img):
    cv2.imencode(".png", img)[1].tofile(path)


def label_of(pairs, key):
    return dict(pairs).get(key, pairs[0][1])


def key_of(pairs, label):
    for k, v in pairs:
        if v == label:
            return k
    return pairs[0][0]


# ---- Win32 ----
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


HWND = ctypes.c_void_p
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, HWND, ctypes.c_void_p)
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.argtypes = [HWND, ctypes.POINTER(POINT)]
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindow.argtypes = [HWND]
user32.GetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
GetWindowLongPtr = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
GetWindowLongPtr.argtypes = [HWND, ctypes.c_int]
GetWindowLongPtr.restype = ctypes.c_ssize_t
user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.SetForegroundWindow.argtypes = [HWND]
user32.BringWindowToTop.argtypes = [HWND]
user32.GetForegroundWindow.restype = HWND
user32.IsIconic.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.PostMessageW.argtypes = [HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.ChildWindowFromPointEx.argtypes = [HWND, POINT, ctypes.c_uint]
user32.ChildWindowFromPointEx.restype = HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = HWND
user32.GetAncestor.argtypes = [HWND, ctypes.c_uint]
user32.GetAncestor.restype = HWND
user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
user32.BlockInput.argtypes = [ctypes.c_int]
user32.MonitorFromWindow.argtypes = [HWND, ctypes.c_ulong]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [HWND, ctypes.c_void_p]
user32.PrintWindow.argtypes = [HWND, ctypes.c_void_p, ctypes.c_uint]
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

WM_KEYDOWN, WM_KEYUP, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x100, 0x101, 0x200, 0x201, 0x202
MK_LBUTTON = 1


def send_inputs(items):
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


def mouse_input(flags, dx=0, dy=0):
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    return inp


def key_input(vk, up=False):
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(vk, 0, 2 if up else 0, 0, None)
    return inp


def mouse_move(x, y):
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    nx = int((x - vx) * 65535 / max(1, vw - 1))
    ny = int((y - vy) * 65535 / max(1, vh - 1))
    send_inputs([mouse_input(0x0001 | 0x8000 | 0x4000, nx, ny)])
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    return cursor_pos()


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def cursor_at(target, pos, tol=6):
    return abs(pos[0] - target[0]) <= tol and abs(pos[1] - target[1]) <= tol


def mouse_click(x, y):
    pos = mouse_move(x, y)
    time.sleep(0.06)
    send_inputs([mouse_input(0x0002)])
    time.sleep(0.06)
    send_inputs([mouse_input(0x0004)])
    return pos


def press_key(vk):
    send_inputs([key_input(vk)])
    time.sleep(0.05)
    send_inputs([key_input(vk, True)])


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if FROZEN:
        exe, args = sys.executable, ""
    else:
        exe, args = sys.executable, f'"{os.path.abspath(__file__)}"'
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, BASE, 1) > 32


# ---- 백그라운드 입력: 대상 창에 직접 메시지 전달 (포커스·커서 불필요) ----
def lparam_xy(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def bg_target(hwnd, pt):
    """화면 좌표 pt 아래에 있는 (자식) 창과 그 창의 클라이언트 좌표."""
    p = POINT(int(pt[0]), int(pt[1]))
    user32.ScreenToClient(hwnd, ctypes.byref(p))
    child = user32.ChildWindowFromPointEx(hwnd, p, 1 | 2 | 4)
    h = child or hwnd
    if h != hwnd:
        p = POINT(int(pt[0]), int(pt[1]))
        user32.ScreenToClient(h, ctypes.byref(p))
    return h, p.x, p.y


def bg_move(hwnd, pt):
    h, x, y = bg_target(hwnd, pt)
    user32.PostMessageW(h, WM_MOUSEMOVE, 0, lparam_xy(x, y))


def bg_click(hwnd, pt):
    h, x, y = bg_target(hwnd, pt)
    lp = lparam_xy(x, y)
    user32.PostMessageW(h, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.03)
    user32.PostMessageW(h, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    user32.PostMessageW(h, WM_LBUTTONUP, 0, lp)


def bg_key(hwnd, vk):
    scan = user32.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16)
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, down)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, down | (1 << 30) | (1 << 31))


def force_foreground(hwnd):
    """다른 창이 포커스를 갖고 있어도 대상 창을 앞으로 가져온다."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    for attempt in range(2):
        if user32.GetForegroundWindow() == hwnd:
            return True
        if attempt == 1:
            send_inputs([key_input(VK_MENU), key_input(VK_MENU, True)])
        fg = user32.GetForegroundWindow()
        tid_me = kernel32.GetCurrentThreadId()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(tid_fg and tid_fg != tid_me and user32.AttachThreadInput(tid_me, tid_fg, 1))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid_me, tid_fg, 0)
        for _ in range(10):
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.03)
    return user32.GetForegroundWindow() == hwnd


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_exe(hwnd):
    h = kernel32.OpenProcess(0x1000, 0, window_pid(hwnd))
    if not h:
        return None
    try:
        buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return None


def client_rect(hwnd):
    r, p = RECT(), POINT(0, 0)
    if not hwnd or not user32.GetClientRect(hwnd, ctypes.byref(r)) or not user32.ClientToScreen(hwnd, ctypes.byref(p)):
        return None
    if r.right < 300 or r.bottom < 200:
        return None
    return (p.x, p.y, r.right, r.bottom)


def is_cloaked(hwnd):
    try:
        v = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(HWND(hwnd), 14, ctypes.byref(v), 4)
        return bool(v.value)
    except Exception:
        return False


def enum_windows():
    """보이는 일반 최상위 창 목록 (오버레이·도구 창·숨겨진 UWP 창·자기 자신 제외)."""
    found, me = [], kernel32.GetCurrentProcessId()

    @WNDENUMPROC
    def visit(hwnd, _):
        title = window_title(hwnd)
        if not (user32.IsWindowVisible(hwnd) and title and window_pid(hwnd) != me and client_rect(hwnd)):
            return True
        ex = GetWindowLongPtr(hwnd, -20)
        if ex & 0x20 or ex & 0x80 or is_cloaked(hwnd):
            return True
        found.append((hwnd, title))
        return True

    user32.EnumWindows(visit, None)
    return found


def list_windows():
    return [t for _, t in enum_windows()]


def find_window(title, exe=None):
    """제목 정확 일치 → 부분 일치 → 실행 파일명 순으로 대상 창을 찾는다."""
    if not title:
        return None
    cands = enum_windows()
    for h, t in cands:
        if t == title:
            return h
    low = title.lower()
    for h, t in cands:
        tl = t.lower()
        if len(low) >= 3 and (low in tl or (len(tl) >= 3 and tl in low)):
            return h
    if exe:
        best = None
        for h, t in cands:
            if (window_exe(h) or "").lower() == exe.lower():
                r = client_rect(h)
                if best is None or r[2] * r[3] > best[1]:
                    best = (h, r[2] * r[3])
        if best:
            return best[0]
    return None


def hwnd_at(rect):
    """영역 중앙에 있는 최상위 창 (바탕화면·자기 자신 제외)."""
    l, t, w, h = rect
    hw = user32.WindowFromPoint(POINT(int(l + w / 2), int(t + h / 2)))
    if not hw:
        return None
    root = user32.GetAncestor(hw, 2) or hw
    if window_pid(root) == kernel32.GetCurrentProcessId() or window_class(root) in ("Progman", "WorkerW"):
        return None
    return root


def is_fullscreen(hwnd):
    """클라이언트 영역이 모니터 전체를 덮는가(전체화면/테두리 없는 창). 이런 창은 포커스를 잃으면 최소화되거나 깜빡이기 쉽다."""
    r = client_rect(hwnd)
    if not r:
        return False
    mon = user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    mw, mh = mi.rcMonitor.right - mi.rcMonitor.left, mi.rcMonitor.bottom - mi.rcMonitor.top
    return abs(r[2] - mw) <= 2 and abs(r[3] - mh) <= 2


def window_unobscured(hwnd, rect):
    """대상 창이 화면에 그대로 드러나 있는가. 3x3 지점을 찍어 모두 대상 창(또는 그 자식)이면 True.
    True면 창을 건드리지 않는 화면 캡처를 써도 되므로 PrintWindow로 인한 깜빡임을 피할 수 있다."""
    if not hwnd or user32.IsIconic(hwnd):
        return False
    l, t, w, h = rect
    for fy in (0.06, 0.5, 0.94):
        for fx in (0.06, 0.5, 0.94):
            hw = user32.WindowFromPoint(POINT(int(l + w * fx), int(t + h * fy)))
            if not hw or (user32.GetAncestor(hw, 2) or hw) != hwnd:
                return False
    return True


_local = threading.local()


def sct():
    s = getattr(_local, "sct", None)
    if s is None:
        s = _local.sct = mss.MSS()
    return s


def grab(rect):
    l, t, w, h = rect
    img = np.asarray(sct().grab({"left": int(l), "top": int(t), "width": int(w), "height": int(h)}))
    return np.ascontiguousarray(img[:, :, :3])


def grab_window(hwnd):
    """PrintWindow로 창의 클라이언트 영역을 캡처한다. 다른 창에 가려져 있어도 된다."""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)
        time.sleep(0.2)
    wr, cr, origin = RECT(), RECT(), POINT(0, 0)
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)) or not user32.GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    cw, ch = cr.right, cr.bottom
    ox, oy = origin.x - wr.left, origin.y - wr.top
    if ww < 1 or wh < 1 or cw < 1 or ch < 1:
        return None
    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, wh)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        ok = user32.PrintWindow(hwnd, mdc, 2)
        bi = BITMAPINFOHEADER()
        bi.biSize, bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = ctypes.sizeof(bi), ww, -wh, 1, 32
        buf = (ctypes.c_ubyte * (ww * wh * 4))()
        got = gdi32.GetDIBits(mdc, bmp, 0, wh, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    if not ok or not got:
        return None
    img = np.frombuffer(buf, np.uint8).reshape(wh, ww, 4)[:, :, :3]
    ox, oy = max(0, ox), max(0, oy)
    crop = img[oy:oy + ch, ox:ox + cw]
    if crop.shape[0] != ch or crop.shape[1] != cw:
        return None
    return np.ascontiguousarray(crop)


def primary_rect():
    m = sct().monitors[1]
    return (m["left"], m["top"], m["width"], m["height"])


def frac_to_screen(fr, rect):
    """창 크기 비율로 저장한 위치 → 화면 좌표. fr이 [fx, fy, ref_w, ref_h]면 지정 당시 창 크기를 알고 있으므로,
    지금 창의 비율이 다를 때 높이 기준·가운데 정렬로 보정한다."""
    l, t, w, h = rect
    if len(fr) >= 4 and fr[2] and fr[3] and abs(w / h - fr[2] / fr[3]) > 0.01:
        k = h / fr[3]
        return int(round(l + w / 2 + (fr[0] - 0.5) * fr[2] * k)), int(round(t + fr[1] * h))
    return int(round(l + fr[0] * w)), int(round(t + fr[1] * h))


# ---- 입력/캡처 방식 컨트롤러 ----
class Ctl:
    """대상 프로그램에 입력을 보내고 화면을 캡처하는 방법(백그라운드/포그라운드)을 한곳에서 관리한다."""

    def __init__(self, app):
        self.app = app
        self.cur_input = "bg"
        self.auto = True
        self.cap_mode = "auto"
        self.hold = True
        self.depth = 0
        self.win_cap_failed = False
        self.warned = set()
        self.last_src = "screen"

    def setup(self, cfg):
        self.auto = cfg["input"] == "auto"
        self.cur_input = "fg" if cfg["input"] == "fg" else "bg"
        self.cap_mode = cfg["capture"]
        self.hold = bool(cfg["hold_mouse"])
        self.idle_wait = max(0.0, float(cfg.get("idle_wait", 1.0)))
        self.idle_time = 0.0
        self.win_cap_failed = False
        self.warned = set()

    def warn_once(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            self.app.log(msg)

    def describe(self):
        return ("백그라운드(창 메시지)" if self.cur_input == "bg" else "포그라운드(실제 입력" + (", 마우스·포커스 잠시 빌린 뒤 복원" if self.hold else "") + ")")

    def capture(self, hwnd, rect):
        """'자동'은 대상 창이 가려져 있지 않으면 화면 캡처(창을 건드리지 않아 깜빡임 없음),
        가려져 있을 때만 PrintWindow(창 직접)를 쓴다. PrintWindow는 대상 창을 다시 그리게 해서 깜빡일 수 있다."""
        if self.cap_mode != "screen" and not self.win_cap_failed:
            if hwnd:
                if self.cap_mode == "auto" and window_unobscured(hwnd, rect):
                    self.last_src = "screen"
                    return grab(rect)
                img = grab_window(hwnd)
                if img is not None and img.max() > 8:
                    if img.shape[1] != rect[2] or img.shape[0] != rect[3]:
                        img = cv2.resize(img, (rect[2], rect[3]), interpolation=cv2.INTER_AREA)
                    if self.last_src != "window":
                        self.last_src = "window"
                        self.warn_once("wincap_on", "대상 창이 가려져 있어 창 직접 캡처(PrintWindow)를 사용합니다. 이 방식은 대상 창이 깜빡일 수 있으니, 깜빡임이 싫으면 대상 창을 가리지 마세요.")
                    return img
                self.win_cap_failed = True
                self.warn_once("wincap", "창 직접 캡처가 검게 나와 화면 캡처로 전환합니다. (독점 전체화면이면 창 모드 권장)")
            elif self.cap_mode == "window":
                self.warn_once("nohwnd", "대상 창을 몰라 화면 캡처를 사용합니다.")
        self.last_src = "screen"
        return grab(rect)

    def poll(self, base=0.15):
        """화면 변화를 기다리는 폴링 간격. PrintWindow를 쓰는 동안은 깜빡임을 줄이려고 두 배로 늘린다."""
        return base * 2 if self.last_src == "window" else base

    def can_bg(self, hwnd):
        return self.cur_input == "bg" and bool(hwnd)

    def switch_to_fg(self, why):
        self.cur_input = "fg"
        self.app.log(f"{why} → 포그라운드 입력(실제 마우스·키보드)으로 전환합니다.")

    def move(self, hwnd, pt):
        if self.can_bg(hwnd):
            bg_move(hwnd, pt)
            return True
        return cursor_at(pt, mouse_move(*pt))

    def click(self, hwnd, pt):
        if self.can_bg(hwnd):
            bg_click(hwnd, pt)
            return True
        return cursor_at(pt, mouse_click(*pt))

    def key(self, hwnd, vk):
        if self.can_bg(hwnd):
            bg_key(hwnd, vk)
        else:
            press_key(vk)

    @contextlib.contextmanager
    def take(self, hwnd):
        """포그라운드 모드: 사용자가 마우스를 멈출 때까지 기다렸다가 입력을 잠시 잠그고 대상 창을 앞으로.
        블록이 끝나면 커서 위치와 이전 포커스 창을 되돌린다. 백그라운드 모드에서는 아무것도 하지 않는다."""
        if self.can_bg(hwnd) or self.depth > 0:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
            return
        self.depth += 1
        saved_cur, saved_fg = cursor_pos(), user32.GetForegroundWindow()
        blocked = False
        try:
            if self.hold:
                t0 = time.perf_counter()
                self.app.wait_user_idle(getattr(self, "idle_wait", 1.0))
                self.idle_time = getattr(self, "idle_time", 0.0) + time.perf_counter() - t0
                blocked = bool(user32.BlockInput(1))
                if not blocked:
                    self.warn_once("block", "입력 잠금(BlockInput)이 거부되어 마우스 위치만 복원합니다. (관리자 권한이면 잠금도 됩니다)")
            if hwnd and user32.GetForegroundWindow() != hwnd:
                if force_foreground(hwnd):
                    self.app.wait(0.12)
                else:
                    self.warn_once("fg", "대상 창을 앞으로 가져오지 못했습니다. 입력이 다른 창에 들어갈 수 있습니다.")
            yield
        finally:
            self.depth -= 1
            try:
                if self.hold:
                    user32.SetCursorPos(int(saved_cur[0]), int(saved_cur[1]))
                    if saved_fg and saved_fg != hwnd and user32.IsWindow(saved_fg) and not user32.IsIconic(saved_fg):
                        if hwnd and is_fullscreen(hwnd):
                            self.warn_once("fsfocus", "대상이 전체화면이라 포커스는 되돌리지 않습니다. (전체화면 프로그램은 포커스를 잃을 때마다 화면이 꺼졌다 켜지거나 최소화됩니다. 창 모드로 바꾸면 포커스도 복원합니다)")
                        else:
                            force_foreground(saved_fg)
            finally:
                if blocked:
                    user32.BlockInput(0)


class Hit:
    """범용 모드에서 템플릿이 일치한 위치."""
    good = True
    via = "템플릿"

    def __init__(self, idx, name, score, x, y, w, h):
        self.idx, self.name, self.score = idx, name, score
        self.x, self.y, self.w, self.h = x, y, w, h


class GenericAnalyzer:
    """범용 모드: 등록된 템플릿을 화면 전체에서 찾아 개수를 센다. 캡처 해상도가 달라지면 템플릿을 비율대로 맞춘다."""

    def __init__(self):
        self.templates = []
        self.cache = {}
        self.best = {}

    def set_items(self, items):
        self.templates, self.cache = [], {}
        for it in items:
            if not it.get("icon"):
                continue
            img = imread(os.path.join(ICON_DIR, it["icon"]))
            if img is not None:
                self.templates.append((it, img))

    def scaled(self, idx, tpl, s):
        if abs(s - 1.0) < 0.01:
            return tpl
        key = (idx, round(s, 3), tpl.shape)
        if key not in self.cache:
            inter = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
            self.cache[key] = cv2.resize(tpl, None, fx=s, fy=s, interpolation=inter)
        return self.cache[key]

    @staticmethod
    def scales_for(it, W, H):
        """등록 당시 창 크기 대비 지금 창 크기의 배율 후보. 비율이 같으면 하나, 다르면 폭 기준과 높이 기준 둘 다 시도한다."""
        ref_w, ref_h = it.get("ref_w"), it.get("ref_h")
        sw = W / float(ref_w) if ref_w else 1.0
        sh = H / float(ref_h) if ref_h else sw
        return [sw] if abs(sw - sh) < 0.02 else [sh, sw]

    @staticmethod
    def nms(r, thr, tw, th, limit=30):
        """일치 지도 r에서 thr 이상인 봉우리를 겹치지 않게 (점수, x, y)로 뽑는다."""
        out = []
        for _ in range(limit):
            _, mx, _, (x, y) = cv2.minMaxLoc(r)
            if mx < thr:
                break
            out.append((float(mx), x, y))
            r[max(0, y - th // 2):y + th // 2 + 1, max(0, x - tw // 2):x + tw // 2 + 1] = -1.0
        return out

    def locate(self, f, half, idx, ts, thr):
        """템플릿 ts의 일치 위치 [(점수, x, y)]와 최고 점수.
        화면이 크고 템플릿이 충분히 크면 절반 해상도에서 후보만 고른 뒤, 후보 주변만 원본 해상도로 다시 대조한다.
        최종 점수는 원본 해상도에서 계산하므로 전체를 대조했을 때와 같은 값이고, 시간은 몇 배 줄어든다."""
        H, W = f.shape[:2]
        th, tw = ts.shape[:2]
        if half is None or min(th, tw) < 24:
            r = cv2.matchTemplate(f, ts, cv2.TM_CCOEFF_NORMED)
            best = float(r.max())
            return self.nms(r, thr, tw, th), best
        key = ("half", idx, ts.shape)
        if key not in self.cache:
            self.cache[key] = cv2.resize(ts, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        tsh = self.cache[key]
        rc = cv2.matchTemplate(half, tsh, cv2.TM_CCOEFF_NORMED)
        cands = self.nms(rc, -2.0, tsh.shape[1], tsh.shape[0], limit=1) + self.nms(rc, max(0.3, thr - 0.2), tsh.shape[1], tsh.shape[0], limit=40)
        out, best = [], 0.0
        for _, cx, cy in cands:
            x0, y0 = max(0, 2 * cx - 4), max(0, 2 * cy - 4)
            roi = f[y0:min(H, 2 * cy + 5 + th), x0:min(W, 2 * cx + 5 + tw)]
            if roi.shape[0] < th or roi.shape[1] < tw:
                continue
            _, m, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(roi, ts, cv2.TM_CCOEFF_NORMED))
            best = max(best, float(m))
            if m >= thr and not any(abs(x0 + dx - x) <= tw // 2 and abs(y0 + dy - y) <= th // 2 for _, x, y in out):
                out.append((float(m), x0 + dx, y0 + dy))
        return out, best

    def analyze(self, f, default_thr):
        H, W = f.shape[:2]
        hits, self.best = [], {}
        half = cv2.resize(f, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA) if W * H >= 1000 * 600 else None
        for idx, (it, tpl) in enumerate(self.templates):
            thr = float(it.get("thr") or default_thr)
            found, best, size = [], -1.0, (0, 0)
            for sc in self.scales_for(it, W, H):
                ts = self.scaled(idx, tpl, sc)
                th, tw = ts.shape[:2]
                if th < 4 or tw < 4 or th > H or tw > W:
                    continue
                fs, bs = self.locate(f, half, idx, ts, thr)
                if bs > best:
                    found, best, size = fs, bs, (tw, th)
            if best < 0:
                continue
            self.best[it["name"]] = best
            for m, x, y in sorted(found, reverse=True):
                hits.append(Hit(len(hits), it["name"], m, x, y, size[0], size[1]))
        return hits


def gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def band_any(f):
    return cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (160, 90), interpolation=cv2.INTER_AREA).astype(np.int16)


def png_b64(img):
    return base64.b64encode(cv2.imencode(".png", img)[1].tobytes()).decode()


class Tip:
    """위젯에 마우스를 올리면 설명을 띄운다."""

    def __init__(self, w, text):
        self.w, self.text, self.tw = w, text, None
        w.bind("<Enter>", self.show, add="+")
        w.bind("<Leave>", self.hide, add="+")
        w.bind("<ButtonPress>", self.hide, add="+")

    def show(self, e):
        self.hide()
        tw = tk.Toplevel(self.w)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.geometry(f"+{e.x_root + 12}+{e.y_root + 18}")
        tk.Label(tw, text=self.text, bg=COL["card"], fg=COL["fg"], relief="solid", bd=1, font=("Malgun Gothic", 9), justify="left", wraplength=380, padx=8, pady=5).pack()
        self.tw = tw

    def hide(self, e=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


class Stopped(Exception):
    pass


def load_presets(app):
    """presets/*.py 의 Preset 클래스를 읽어 {key: 인스턴스}로 돌려준다. 폴더가 없으면 빈 dict.

    Preset 인터페이스(덕 타이핑): key, label, register_category, result_caption,
    default_items(), set_items(items), build_widgets(parent, row, pad) -> [widget], apply_cfg(cfg), collect_cfg(cfg),
    help_text(), normalize(raw), analyze(frame) -> [항목(idx, name, score, good, via)], band(frame), busy(frame),
    icon_crop(frame, idx), crop_for(frame, s), bonus(s), summary(found), debug_image(img, found),
    log_analysis(frame, found, rect, path), action_point(rect), before_action(hwnd, rect, frame) -> frame|None,
    post_analyze(hwnd, rect, frame, found), do_action(hwnd, rect) -> True|False."""
    out, core = {}, sys.modules[__name__]
    dirs = dict.fromkeys([os.path.join(getattr(sys, "_MEIPASS", BASE), "presets"), os.path.join(BASE, "presets")])
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location("preset_" + fn[:-3], os.path.join(d, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                pr = mod.Preset(app, core)
                out.setdefault(pr.key, pr)
            except Exception as e:
                app.log(f"프리셋 {fn} 을 불러오지 못했습니다: {e}")
    return out


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Screen Matcher")
        self.cfg = dict(DEFAULT_CFG)
        self.items, self.g_items = [], []
        self.q = queue.Queue()
        self.gen = GenericAnalyzer()
        self.ctl = Ctl(self)
        self.presets = load_presets(self)
        self.stop_evt = threading.Event()
        self.worker = None
        self.last_frame = None
        self.last_found = None
        self.thumbs = [None] * 10
        self.minimized = False
        os.makedirs(ICON_DIR, exist_ok=True)
        os.makedirs(CAP_DIR, exist_ok=True)
        self.load()
        self.build()
        self.apply_cfg()
        self.refresh_tree()
        for pr in self.presets.values():
            pr.set_items(self.items)
        self.gen.set_items(self.g_items)
        self.after(100, self.pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("준비됨. ① 대상 창을 고르고 ③에서 클릭할 버튼 위치, ②에서 찾을 이미지를 등록한 뒤 '현재 화면 분석'으로 인식을 확인하세요.")
        if is_admin():
            self.log("관리자 권한으로 실행 중입니다.")
        else:
            self.log("※ 관리자 권한이 아닙니다. 대상 프로그램이 관리자 권한이면 입력이 안 들어가고, 입력 잠금(마우스 홀드)도 안 됩니다. '관리자 권한으로 다시 실행'을 권장합니다.")
        self.log("※ 입력 방식 '자동'은 대상 창에 직접 메시지를 보내 마우스·포커스를 건드리지 않습니다. 반응이 없으면 실제 입력으로 자동 전환합니다.")
        self.log("※ 자동 입력은 대상 프로그램의 이용 약관에 어긋날 수 있습니다. 사용 책임은 본인에게 있습니다.")

    # ---------- 설정 ----------
    def load(self):
        items = None
        try:
            self.cfg.update(json.load(open(os.path.join(ASSETS, "default_cfg.json"), encoding="utf-8")))
        except Exception:
            pass
        if os.path.exists(SETTINGS):
            try:
                data = json.load(open(SETTINGS, encoding="utf-8"))
                self.cfg.update({k: v for k, v in data.items() if k not in ("items", "g_items")})
                items = data.get("items")
                self.g_items = data.get("g_items") or []
            except Exception:
                pass
        defaults = [d for pr in self.presets.values() for d in pr.default_items()]
        if not items:
            items = defaults
        else:
            by = {it["name"]: it for it in items}
            for d in defaults:
                it = by.get(d["name"])
                if it is None:
                    items.append(d)
                elif d.get("icon") and (not it.get("icon") or it.get("builtin")):
                    it["icon"], it["builtin"] = d["icon"], True
        self.items = items

    def save(self):
        self.collect_cfg()
        data = dict(self.cfg)
        data["items"], data["g_items"] = self.items, self.g_items
        try:
            json.dump(data, open(SETTINGS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            self.log(f"설정 저장 실패: {e}")

    def modes(self):
        return MODES + [(pr.key, pr.label) for pr in self.presets.values()]

    def mode(self):
        m = self.cfg.get("mode", "generic")
        return m if m in dict(self.modes()) else "generic"

    @property
    def preset(self):
        """활성 프리셋 인스턴스. 이미지 매칭 모드면 None."""
        return self.presets.get(self.mode())

    def generic(self):
        return self.preset is None

    def cur_items(self):
        return self.g_items if self.generic() else self.items

    def categories(self):
        cats = list(CATEGORIES)
        for it in self.items + self.g_items:
            if it.get("category") and it["category"] not in cats:
                cats.append(it["category"])
        return cats

    # ---------- 테마 ----------
    def card(self, parent, title, caption=None):
        """카드 한 장: 제목(굵게) + 설명(작게) + 내용 프레임을 돌려준다."""
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8, 12, 10))
        head = ttk.Frame(outer, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
        outer.caption = ttk.Label(head, text=caption or "", style="Sub.TLabel")
        outer.caption.pack(side="left", padx=(10, 0))
        body = ttk.Frame(outer, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        return outer, body

    def apply_theme(self):
        global COL
        name = self.cfg.get("theme") or "dark"
        if name not in THEME:
            name = "dark"
        COL = c = THEME[name]
        if sv_ttk:
            sv_ttk.set_theme(name)
        style = ttk.Style(self)
        style.configure(".", font=("Malgun Gothic", 9))
        for st in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox", "TSpinbox", "Treeview", "TLabelframe.Label"):
            style.configure(st, font=("Malgun Gothic", 9))
        style.configure("Treeview", rowheight=23)
        style.configure("Treeview.Heading", font=("Malgun Gothic", 9, "bold"))
        if not sv_ttk:
            style.configure("Card.TFrame", background=c["card"], relief="solid", borderwidth=1)
            for st in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton"):
                style.configure(st, background=c["card"])
        style.configure("CardTitle.TLabel", font=("Malgun Gothic", 10, "bold"), background=c["card"], foreground=c["fg"])
        style.configure("Sub.TLabel", foreground=c["sub"], background=c["card"])
        style.configure("Card.TLabel", background=c["card"], foreground=c["fg"])
        style.configure("Card.TCheckbutton", background=c["card"])
        style.configure("Card.TRadiobutton", background=c["card"])
        style.configure("Status.TLabel", foreground=c["sub"], background=c["bg"])
        self.configure(bg=c["bg"])
        self.header.configure(bg=c["header"])
        for w in self.header_widgets:
            w.configure(bg=c["header"])
        self.h_title.configure(fg="#ffffff")
        self.h_sub.configure(fg="#9fb3c8")
        self.state_label.configure(bg=c["header"])
        self.theme_btn.configure(text="☀ 라이트" if name == "dark" else "🌙 다크")
        self.log_box.configure(bg=c["log_bg"], fg=c["fg"], insertbackground=c["fg"], selectbackground=c["accent"])
        for tag, key in (("err", "err"), ("ok", "ok"), ("note", "sub"), ("iter", "iter")):
            self.log_box.tag_configure(tag, foreground=c[key])
        for img, txt in self.slot_labels:
            txt.configure(bg=c["card"], fg=c["sub"])
            if not getattr(img, "good", False):
                img.configure(bg=c["thumb"])
            else:
                img.configure(bg=c["good"])
        for b, kind in ((self.b_start, "start"), (self.b_stop, "stop")):
            b.configure(bg=c[kind], activebackground=c[kind + "_hover"], disabledforeground=c["sub"])
        self.status_bar.configure(bg=c["bg"])
        self.status_label.configure(bg=c["bg"], fg=c["sub"])
        self.admin_label.configure(bg=c["header"], fg="#4ade80" if is_admin() else "#fca5a5")
        self.update_area_info()

    def toggle_theme(self):
        self.cfg["theme"] = "light" if (self.cfg.get("theme") or "dark") == "dark" else "dark"
        self.apply_theme()
        self.save()

    def hover_btn(self, b, kind):
        b.bind("<Enter>", lambda e: b["state"] == "normal" and b.configure(bg=COL[kind + "_hover"]))
        b.bind("<Leave>", lambda e: b.configure(bg=COL[kind]))

    # ---------- GUI ----------
    def build(self):
        pad = {"padx": 4, "pady": 2}
        self.minsize(980, 680)
        self.header_widgets = []

        # 헤더
        hd = self.header = tk.Frame(self, height=54)
        hd.grid(row=0, column=0, columnspan=2, sticky="ew")
        hd.grid_propagate(False)
        self.h_title = tk.Label(hd, text="SCREEN MATCHER", font=("Segoe UI", 15, "bold"))
        self.h_title.place(x=16, y=8)
        self.h_sub = tk.Label(hd, text="화면에서 이미지를 찾아 조건을 만족할 때까지 클릭을 반복합니다", font=("Malgun Gothic", 9))
        self.h_sub.place(x=18, y=34)
        self.header_widgets += [self.h_title, self.h_sub]
        self.state_var = tk.StringVar(value="● 대기")
        self.state_label = tk.Label(hd, textvariable=self.state_var, font=("Malgun Gothic", 10, "bold"), fg="#9fb3c8")
        self.state_label.place(relx=1.0, x=-250, y=18, anchor="ne")
        self.admin_label = tk.Label(hd, text=("관리자 권한" if is_admin() else "일반 권한"), font=("Malgun Gothic", 9))
        self.admin_label.place(relx=1.0, x=-150, y=20, anchor="ne")
        self.header_widgets.append(self.admin_label)
        self.theme_btn = ttk.Button(hd, text="☀ 라이트", command=self.toggle_theme, width=9)
        self.theme_btn.place(relx=1.0, x=-14, y=12, anchor="ne")

        body = ttk.Frame(self, padding=(10, 8, 10, 4))
        body.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)
        body.grid_rowconfigure(4, weight=1)

        # ① 대상 창  ② 동작 방식 (한 카드, 두 줄)
        c1, top = self.card(body, "①  대상 창 · 동작 방식", "자동화할 프로그램과 입력 방법")
        c1.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.area_var = tk.StringVar()
        self.area_box = ttk.Combobox(top, textvariable=self.area_var, width=40)
        self.area_box.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.area_box.bind("<<ComboboxSelected>>", lambda e: self.on_area_selected())
        Tip(self.area_box, "자동화할 프로그램 창을 고르세요. 제목이 바뀌어도 부분 일치·실행 파일명으로 다시 찾습니다.\n창이 안 잡히면 '주 모니터 전체' 또는 '영역 드래그 지정'을 쓰세요.")
        ttk.Button(top, text="창 목록 새로고침", command=self.refresh_windows).grid(row=0, column=2, **pad)
        ttk.Button(top, text="영역 드래그 지정", command=self.drag_area).grid(row=0, column=3, **pad)
        self.area_info = ttk.Label(top, text="", style="Sub.TLabel")
        self.area_info.grid(row=0, column=4, columnspan=4, sticky="w", padx=10)
        top.grid_columnconfigure(7, weight=1)
        ttk.Label(top, text="모드", style="Card.TLabel").grid(row=1, column=0, sticky="e", **pad)
        self.v_mode = tk.StringVar()
        mb = ttk.Combobox(top, textvariable=self.v_mode, values=[v for _, v in self.modes()], width=30, state="readonly")
        mb.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        mb.bind("<<ComboboxSelected>>", lambda e: self.on_mode_change())
        Tip(mb, "이미지 매칭: 클릭할 버튼과 찾을 이미지를 직접 지정해 어떤 프로그램에서든 사용.\npresets 폴더에 플러그인이 있으면 전용 모드가 추가됩니다.")
        ttk.Label(top, text="입력", style="Card.TLabel").grid(row=1, column=4, sticky="e", **pad)
        self.v_input = tk.StringVar()
        ib = ttk.Combobox(top, textvariable=self.v_input, values=[v for _, v in INPUTS], width=36, state="readonly")
        ib.grid(row=1, column=5, sticky="w", **pad)
        Tip(ib, "백그라운드: 대상 창에 직접 메시지를 보내 커서·포커스를 건드리지 않음. 대상 창을 클릭해 둘 필요 없음.\n포그라운드: 실제 마우스·키보드. 하드웨어 입력만 받는 프로그램용.\n자동: 백그라운드로 시작, 반응 없으면 포그라운드로 전환.")
        ttk.Label(top, text="캡처", style="Card.TLabel").grid(row=1, column=6, sticky="e", **pad)
        self.v_cap = tk.StringVar()
        cb0 = ttk.Combobox(top, textvariable=self.v_cap, values=[v for _, v in CAPS], width=30, state="readonly")
        cb0.grid(row=1, column=7, sticky="w", **pad)
        Tip(cb0, "자동: 대상 창이 보이면 화면 캡처, 가려져 있을 때만 창 직접 캡처.\n창 직접: 가려져 있어도 캡처됨(PrintWindow). 대상 창이 깜빡일 수 있음.\n화면: 모니터 픽셀을 그대로 읽음. 깜빡임 없음, 가려지면 안 됨.")
        self.v_hold = tk.BooleanVar()
        hf = ttk.Frame(top, style="Card.TFrame")
        hf.grid(row=2, column=0, columnspan=8, sticky="w")
        hc = ttk.Checkbutton(hf, text="포그라운드일 때 내 마우스가 멈추면 잠깐 빌려 쓰고, 끝나면 커서 위치·포커스 창 복원", variable=self.v_hold, style="Card.TCheckbutton")
        hc.grid(row=0, column=0, sticky="w", **pad)
        Tip(hc, "마우스를 움직이는 중이면 오른쪽의 최대 대기 시간까지 기다립니다. 관리자 권한이면 그 사이 입력도 잠급니다(BlockInput).")
        ttk.Label(hf, text="최대 대기(초)", style="Card.TLabel").grid(row=0, column=1, sticky="e", padx=(14, 4))
        self.v_idle = tk.DoubleVar()
        isp = ttk.Spinbox(hf, from_=0.0, to=5.0, increment=0.5, textvariable=self.v_idle, width=5, format="%.1f")
        isp.grid(row=0, column=2, sticky="w", **pad)
        Tip(isp, "내 마우스가 멈추기를 기다리는 최대 시간. 길면 방해가 줄지만 그만큼 반복이 느려집니다. 0이면 기다리지 않고 바로 가져갑니다.")

        # ② 찾을 이미지
        c2, left = self.card(body, "②  찾을 이미지", "행을 고르면 아래에서 수정")
        c2.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        cols = ("name", "cat", "value", "min", "thr", "icon")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=8, selectmode="browse")
        for c, t, w in zip(cols, ("이름", "분류", "점수", "최소", "임계", "그림"), (230, 60, 50, 40, 45, 40)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c != "name" else "w", stretch=(c == "name"))
        self.tree.grid(row=0, column=0, columnspan=8, sticky="nsew")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=8, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(1, weight=1)
        ed = ttk.Frame(left, style="Card.TFrame")
        ed.grid(row=1, column=0, columnspan=9, sticky="ew", pady=(6, 0))
        ttk.Label(ed, text="이름", style="Card.TLabel").grid(row=0, column=0, sticky="e", **pad)
        self.e_name = ttk.Entry(ed, width=34)
        self.e_name.grid(row=0, column=1, columnspan=5, sticky="w", **pad)
        ttk.Label(ed, text="분류", style="Card.TLabel").grid(row=1, column=0, sticky="e", **pad)
        self.e_cat = ttk.Combobox(ed, values=CATEGORIES, width=8)
        self.e_cat.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(ed, text="점수", style="Card.TLabel").grid(row=1, column=2, sticky="e", **pad)
        self.e_val = ttk.Spinbox(ed, from_=0, to=999, width=5)
        self.e_val.grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(ed, text="최소", style="Card.TLabel").grid(row=1, column=4, sticky="e", **pad)
        self.e_min = ttk.Spinbox(ed, from_=0, to=99, width=4)
        self.e_min.grid(row=1, column=5, sticky="w", **pad)
        self.l_thr = ttk.Label(ed, text="임계", style="Card.TLabel")
        self.l_thr.grid(row=1, column=6, sticky="e", **pad)
        self.e_thr = ttk.Spinbox(ed, from_=0.5, to=0.99, increment=0.01, width=5, format="%.2f")
        self.e_thr.grid(row=1, column=7, sticky="w", **pad)
        Tip(self.e_thr, "이미지 일치 임계값(0.5~0.99). 엉뚱한 곳이 잡히면 올리고, 못 찾으면 내리세요.")
        bf = ttk.Frame(left, style="Card.TFrame")
        bf.grid(row=2, column=0, columnspan=9, sticky="w", pady=(4, 0))
        ttk.Button(bf, text="적용", command=self.apply_item).grid(row=0, column=0, **pad)
        self.b_add = ttk.Button(bf, text="새 항목 추가", command=self.add_item)
        self.b_add.grid(row=0, column=1, **pad)
        self.b_tpl = ttk.Button(bf, text="＋ 화면에서 드래그로 등록", command=self.pick_template, style="Accent.TButton")
        self.b_tpl.grid(row=0, column=2, **pad)
        ttk.Button(bf, text="삭제", command=self.delete_item).grid(row=0, column=3, **pad)
        ttk.Button(bf, text="기본값 복원", command=self.reset_items).grid(row=0, column=4, **pad)

        # ③ 조건  ④ 실행
        rightcol = ttk.Frame(body)
        rightcol.grid(row=2, column=1, sticky="nsew")
        rightcol.grid_columnconfigure(0, weight=1)
        c3, right = self.card(rightcol, "③  반복 동작 · 종료 조건")
        c3.grid(row=0, column=0, sticky="new", pady=(0, 8))
        r = 0
        gf = self.gen_frame = ttk.Frame(right, style="Card.TFrame")
        gf.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Label(gf, text="클릭할 버튼", style="Card.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.l_action = ttk.Label(gf, text="미지정", style="Card.TLabel")
        self.l_action.grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(gf, text="화면에서 클릭으로 지정", command=lambda: self.pick_point("g_action"), style="Accent.TButton").grid(row=0, column=2, columnspan=2, sticky="w", **pad)
        ttk.Label(gf, text="후속 동작", style="Card.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self.v_confirm = tk.StringVar()
        cb = ttk.Combobox(gf, textvariable=self.v_confirm, values=[v for _, v in CONFIRMS], width=10, state="readonly")
        cb.grid(row=1, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_gen_labels())
        Tip(cb, "버튼을 누른 뒤 확인창이 뜨는 프로그램이면 Enter 키 또는 확인 버튼 위치 클릭을 고르세요.")
        self.l_confirm = ttk.Label(gf, text="", style="Card.TLabel")
        self.l_confirm.grid(row=1, column=2, sticky="w", **pad)
        self.b_confirm = ttk.Button(gf, text="위치 지정", command=lambda: self.pick_point("g_confirm_pos"))
        self.b_confirm.grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(gf, text="후속 동작까지 지연(초)", style="Card.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self.v_confirm_delay = tk.DoubleVar()
        ttk.Spinbox(gf, from_=0.0, to=10.0, increment=0.1, textvariable=self.v_confirm_delay, width=6, format="%.1f").grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(gf, text="기본 임계값", style="Card.TLabel").grid(row=2, column=2, sticky="e", **pad)
        self.v_gthr = tk.DoubleVar()
        ttk.Spinbox(gf, from_=0.5, to=0.99, increment=0.01, textvariable=self.v_gthr, width=6, format="%.2f").grid(row=2, column=3, sticky="w", **pad)
        ttk.Separator(gf, orient="horizontal").grid(row=3, column=0, columnspan=4, sticky="ew", pady=6)
        r += 1

        self.v_use_value, self.v_use_cat_a, self.v_use_cat_b = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        self.v_value, self.v_cat_a, self.v_cat_b = tk.IntVar(), tk.IntVar(), tk.IntVar()
        self.v_logic, self.v_max, self.v_wait = tk.StringVar(), tk.IntVar(), tk.DoubleVar()
        self.v_cat_a_name, self.v_cat_b_name = tk.StringVar(), tk.StringVar()
        self.cat_boxes = []
        ttk.Checkbutton(right, text="점수 합 ≥", variable=self.v_use_value, style="Card.TCheckbutton").grid(row=r, column=0, columnspan=2, sticky="w", **pad)
        ttk.Spinbox(right, from_=0, to=999, textvariable=self.v_value, width=6).grid(row=r, column=2, sticky="w", **pad)
        r += 1
        for bv, cv_, iv in ((self.v_use_cat_a, self.v_cat_a_name, self.v_cat_a), (self.v_use_cat_b, self.v_cat_b_name, self.v_cat_b)):
            ttk.Checkbutton(right, text="분류", variable=bv, style="Card.TCheckbutton").grid(row=r, column=0, sticky="w", **pad)
            fr = ttk.Frame(right, style="Card.TFrame")
            fr.grid(row=r, column=1, sticky="w")
            box = ttk.Combobox(fr, textvariable=cv_, values=CATEGORIES, width=9)
            box.grid(row=0, column=0, **pad)
            self.cat_boxes.append(box)
            ttk.Label(fr, text="개수 ≥", style="Card.TLabel").grid(row=0, column=1, **pad)
            ttk.Spinbox(right, from_=0, to=999, textvariable=iv, width=6).grid(row=r, column=2, sticky="w", **pad)
            r += 1
        ttk.Label(right, text="이미지별 최소 개수는 표의 '최소' 칸 (0 = 사용 안 함)", style="Sub.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1
        lf = ttk.Frame(right, style="Card.TFrame")
        lf.grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Label(lf, text="조건 결합", style="Card.TLabel").grid(row=0, column=0, **pad)
        ttk.Radiobutton(lf, text="모두 만족(AND)", variable=self.v_logic, value="AND", style="Card.TRadiobutton").grid(row=0, column=1, **pad)
        ttk.Radiobutton(lf, text="하나라도(OR)", variable=self.v_logic, value="OR", style="Card.TRadiobutton").grid(row=0, column=2, **pad)
        r += 1
        ttk.Separator(right, orient="horizontal").grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        tf = ttk.Frame(right, style="Card.TFrame")
        tf.grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Label(tf, text="최대 반복 횟수", style="Card.TLabel").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(tf, from_=1, to=1000000, textvariable=self.v_max, width=8).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(tf, text="클릭 후 대기(초)", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(14, 4), pady=2)
        ttk.Spinbox(tf, from_=0.0, to=10.0, increment=0.1, textvariable=self.v_wait, width=6, format="%.1f").grid(row=0, column=3, sticky="w", **pad)
        r += 1
        self.preset_widgets = {}
        for pr in self.presets.values():
            ws = pr.build_widgets(right, r, pad)
            self.preset_widgets[pr.key] = ws
            r += max(1, len({w.grid_info().get("row") for w in ws}))
        self.help_label = ttk.Label(right, text="", style="Sub.TLabel", wraplength=470, justify="left")
        self.help_label.grid(row=r, column=0, columnspan=3, sticky="w", **pad)

        c4, run = self.card(rightcol, "④  실행")
        c4.grid(row=1, column=0, sticky="ew")
        self.b_start = tk.Button(run, text="▶  시작", command=self.start, fg="white", activeforeground="white", font=("Malgun Gothic", 11, "bold"), width=11, relief="flat", bd=0, cursor="hand2", pady=6)
        self.b_start.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=2, sticky="ns")
        self.b_stop = tk.Button(run, text="■  정지 (F9)", command=self.stop, fg="white", activeforeground="white", font=("Malgun Gothic", 11, "bold"), width=11, relief="flat", bd=0, cursor="hand2", state="disabled", pady=6)
        self.b_stop.grid(row=0, column=1, rowspan=2, padx=6, pady=2, sticky="ns")
        self.hover_btn(self.b_start, "start")
        self.hover_btn(self.b_stop, "stop")
        tb = ttk.Frame(run, style="Card.TFrame")
        tb.grid(row=0, column=2, sticky="w")
        b = ttk.Button(tb, text="현재 화면 분석", command=self.test_once); b.grid(row=0, column=0, **pad)
        Tip(b, "지금 화면을 한 번 읽어 판정 결과를 보여 줍니다. 클릭은 하지 않습니다.")
        b = ttk.Button(tb, text="위치 테스트", command=self.mouse_test); b.grid(row=0, column=1, **pad)
        Tip(b, "클릭할 버튼 위치가 맞는지 확인합니다. 포그라운드면 커서가 그 위로 갑니다(클릭 없음).")
        b = ttk.Button(tb, text="1회 클릭 테스트", command=self.single_test); b.grid(row=0, column=2, **pad)
        Tip(b, "동작을 딱 한 번 실행해 지금 입력 방식이 이 프로그램에 먹는지 확인합니다.")
        if not is_admin():
            ttk.Button(tb, text="관리자 권한으로 다시 실행", command=self.restart_admin).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        # 분석 결과 (한 줄)
        c5, pv = self.card(body, "분석 결과", "일치한 위치 (최대 10개)")
        c5.grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
        self.result_card = c5
        self.slot_labels = []
        for i in range(10):
            fr = ttk.Frame(pv, style="Card.TFrame")
            fr.grid(row=0, column=i, padx=3, pady=2)
            img = tk.Label(fr, width=12, height=5, relief="flat", bd=0)
            img.good = False
            img.pack()
            txt = tk.Label(fr, text=f"{i + 1}", font=("Malgun Gothic", 8), justify="center", width=13, bd=0)
            txt.pack()
            img.bind("<Button-1>", lambda e, k=i: self.register_slot(k))
            self.slot_labels.append((img, txt))

        # 로그
        c6, lg = self.card(body, "로그", "우클릭: 복사 / 지우기")
        c6.grid(row=4, column=0, columnspan=2, sticky="nsew")
        self.log_box = tk.Text(lg, height=5, width=100, state="disabled", font=("Malgun Gothic", 9), wrap="word", relief="flat", bd=0, highlightthickness=0, padx=6, pady=4)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(lg, command=self.log_box.yview)
        sb2.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb2.set)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="로그 복사", command=self.copy_log)
        menu.add_command(label="로그 지우기", command=self.clear_log)
        self.log_box.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        # 상태 바
        self.status_bar = tk.Frame(self)
        self.status_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status_var = tk.StringVar()
        self.status_label = tk.Label(self.status_bar, textvariable=self.status_var, font=("Malgun Gothic", 8), anchor="w")
        self.status_label.pack(side="left", padx=12, pady=2)
        self.apply_theme()
        self.refresh_windows()

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end"))

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def set_state(self, text, color=None):
        self.state_var.set("● " + text)
        self.state_label.configure(fg=color or "#9fb3c8")

    @staticmethod
    def log_tag(msg):
        if msg.startswith("※"):
            return "note"
        if "달성" in msg and "미달" not in msg:
            return "ok"
        if any(k in msg for k in ("오류", "실패", "중지", "없습니다", "없어", "거부", "안 먹")):
            return "err"
        if msg.startswith("["):
            return "iter"
        return ""

    def refresh_windows(self):
        self.area_box["values"] = ["주 모니터 전체"] + list_windows()
        if not self.area_var.get():
            self.area_var.set(self.cfg.get("area") or "주 모니터 전체")
        self.update_area_info()

    def on_area_selected(self):
        self.cfg["area"] = self.area_var.get().strip()
        hwnd = find_window(self.cfg["area"])
        self.cfg["area_exe"] = window_exe(hwnd) if hwnd else None
        self.update_area_info()
        self.save()

    def update_area_info(self):
        self.cfg["area"] = self.area_var.get().strip()
        try:
            hwnd, rect = self.resolve()
            extra = f" | 창: {window_title(hwnd)[:30]} ({window_exe(hwnd) or '?'})" if hwnd else " | 창 없음(포그라운드 입력만 가능)"
            self.area_info.config(text=f"캡처 영역 {rect[2]}x{rect[3]} @({rect[0]},{rect[1]}){extra}", foreground=COL["sub"])
            self.status_var.set(f"영역 {rect[2]}x{rect[3]}" + (f" · {window_title(hwnd)[:24]}" if hwnd else ""))
        except Exception as e:
            self.area_info.config(text=str(e), foreground=COL["err"])
            self.status_var.set(str(e))

    def resolve(self):
        """(hwnd, rect). hwnd는 백그라운드 입력·창 캡처 대상이며 없을 수 있다."""
        area = (self.cfg.get("area") or "").strip()
        if area == "주 모니터 전체":
            rect = primary_rect()
            return hwnd_at(rect), rect
        if area.startswith("영역:"):
            rect = self.cfg.get("rect")
            if not rect:
                raise RuntimeError("드래그로 지정된 영역이 없습니다")
            rect = tuple(rect)
            return hwnd_at(rect), rect
        hwnd = find_window(area, self.cfg.get("area_exe"))
        rect = client_rect(hwnd) if hwnd else None
        if not rect:
            raise RuntimeError(f"'{area}' 창을 찾을 수 없습니다 (창 목록 새로고침)")
        return hwnd, rect

    def resolve_rect(self):
        return self.resolve()[1]

    def drag_area(self):
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        ov = tk.Toplevel(self)
        ov.overrideredirect(True)
        ov.attributes("-alpha", 0.3, "-topmost", True)
        ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
        ov.configure(bg="black", cursor="crosshair")
        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        st = {"x": 0, "y": 0, "r": None}

        def down(e):
            st["x"], st["y"] = e.x_root, e.y_root
            st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

        def move(e):
            if st["r"]:
                cv.coords(st["r"], st["x"] - vx, st["y"] - vy, e.x_root - vx, e.y_root - vy)

        def up(e):
            l, t = min(st["x"], e.x_root), min(st["y"], e.y_root)
            w, h = abs(e.x_root - st["x"]), abs(e.y_root - st["y"])
            ov.destroy()
            if w > 100 and h > 100:
                self.cfg["rect"] = [l, t, w, h]
                self.area_var.set(f"영역: {l},{t} {w}x{h}")
                self.update_area_info()
                self.save()

        cv.bind("<ButtonPress-1>", down)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", up)
        ov.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

    def apply_cfg(self):
        c = self.cfg
        self.area_var.set(c.get("area") or "주 모니터 전체")
        self.v_mode.set(label_of(self.modes(), self.mode()))
        self.v_input.set(label_of(INPUTS, c.get("input")))
        self.v_cap.set(label_of(CAPS, c.get("capture")))
        self.v_hold.set(bool(c.get("hold_mouse", True)))
        self.v_idle.set(float(c.get("idle_wait", 1.0)))
        self.v_use_value.set(c["use_value"]); self.v_value.set(c["value"])
        self.v_use_cat_a.set(c["use_cat_a"]); self.v_cat_a.set(c["cat_a"])
        self.v_use_cat_b.set(c["use_cat_b"]); self.v_cat_b.set(c["cat_b"])
        self.v_cat_a_name.set(c.get("cat_a_name") or "중요"); self.v_cat_b_name.set(c.get("cat_b_name") or "보통")
        self.v_logic.set(c["logic"]); self.v_max.set(c["max_iter"]); self.v_wait.set(c["wait_after"])
        for pr in self.presets.values():
            pr.apply_cfg(c)
        self.v_confirm.set(label_of(CONFIRMS, c.get("g_confirm")))
        self.v_confirm_delay.set(float(c.get("g_confirm_delay", 0.6)))
        self.v_gthr.set(float(c.get("g_thr", 0.8)))
        self.update_mode_widgets()
        self.update_area_info()

    def collect_cfg(self):
        c = self.cfg
        try:
            c["area"] = self.area_var.get().strip()
            c["mode"] = key_of(self.modes(), self.v_mode.get())
            c["input"] = key_of(INPUTS, self.v_input.get())
            c["capture"] = key_of(CAPS, self.v_cap.get())
            c["hold_mouse"] = bool(self.v_hold.get())
            c["idle_wait"] = float(self.v_idle.get())
            c["use_value"], c["value"] = self.v_use_value.get(), int(self.v_value.get())
            c["use_cat_a"], c["cat_a"] = self.v_use_cat_a.get(), int(self.v_cat_a.get())
            c["use_cat_b"], c["cat_b"] = self.v_use_cat_b.get(), int(self.v_cat_b.get())
            c["cat_a_name"], c["cat_b_name"] = self.v_cat_a_name.get().strip() or "중요", self.v_cat_b_name.get().strip() or "보통"
            c["logic"], c["max_iter"] = self.v_logic.get(), int(self.v_max.get())
            c["wait_after"] = float(self.v_wait.get())
            for pr in self.presets.values():
                pr.collect_cfg(c)
            c["g_confirm"] = key_of(CONFIRMS, self.v_confirm.get())
            c["g_confirm_delay"] = float(self.v_confirm_delay.get())
            c["g_thr"] = float(self.v_gthr.get())
        except (tk.TclError, ValueError):
            pass

    def on_mode_change(self):
        self.collect_cfg()
        self.update_mode_widgets()
        self.refresh_tree()
        self.clear_slots()
        self.save()
        self.log("모드: " + label_of(self.modes(), self.mode()))

    def update_mode_widgets(self):
        g = self.generic()
        self.tree["displaycolumns"] = ("name", "cat", "value", "min", "thr", "icon") if g else ("name", "cat", "value", "min", "icon")
        if g:
            self.gen_frame.grid()
            self.b_tpl.grid()
            self.b_add.grid_remove()
            self.l_thr.grid(); self.e_thr.grid()
            self.help_label.config(text="③에서 클릭할 버튼 위치를 지정하고 ②에서 찾을 이미지를 드래그로 등록하세요. 등록한 이미지가 화면에 보이는 개수·점수가 조건을 만족하면 멈춥니다.")
            self.result_card.caption.config(text="일치한 위치 (최대 10개)")
        else:
            self.gen_frame.grid_remove()
            self.b_tpl.grid_remove()
            self.b_add.grid()
            self.l_thr.grid_remove(); self.e_thr.grid_remove()
            self.help_label.config(text=self.preset.help_text())
            self.result_card.caption.config(text=self.preset.result_caption)
        for k, ws in self.preset_widgets.items():
            for w in ws:
                if k == self.mode():
                    w.grid()
                else:
                    w.grid_remove()
        cats = self.categories()
        self.e_cat["values"] = cats
        for b in self.cat_boxes:
            b["values"] = cats
        self.update_gen_labels()

    def update_gen_labels(self):
        c = self.cfg
        p = c.get("g_action")
        self.l_action.config(text=f"({p[0] * 100:.1f}%, {p[1] * 100:.1f}%)" if p else "미지정", foreground=COL["ok"] if p else COL["err"])
        mode = key_of(CONFIRMS, self.v_confirm.get())
        if mode == "click":
            q = c.get("g_confirm_pos")
            self.l_confirm.config(text=f"({q[0] * 100:.1f}%, {q[1] * 100:.1f}%)" if q else "위치 미지정")
            self.b_confirm.grid()
        else:
            self.l_confirm.config(text="")
            self.b_confirm.grid_remove()

    # ---------- 찾을 이미지 표 ----------
    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.cur_items()):
            thr = f"{it['thr']:.2f}" if it.get("thr") else "-"
            self.tree.insert("", "end", iid=str(i), values=(it["name"], it["category"], it["value"], it["min"], thr, "O" if it.get("icon") else "-"))

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def on_select(self, _=None):
        i = self.selected_index()
        if i is None or i >= len(self.cur_items()):
            return
        it = self.cur_items()[i]
        self.e_name.delete(0, "end"); self.e_name.insert(0, it["name"])
        self.e_cat.set(it["category"])
        self.e_val.delete(0, "end"); self.e_val.insert(0, str(it["value"]))
        self.e_min.delete(0, "end"); self.e_min.insert(0, str(it["min"]))
        self.e_thr.delete(0, "end"); self.e_thr.insert(0, f"{float(it.get('thr') or self.cfg.get('g_thr', 0.8)):.2f}")

    def read_editor(self):
        name = self.e_name.get().strip()
        if not name:
            raise ValueError("이름을 입력하세요")
        cat = self.e_cat.get().strip() or "기타"
        try:
            thr = float(self.e_thr.get() or 0)
        except ValueError:
            thr = 0.0
        return name, cat, int(self.e_val.get() or 0), int(self.e_min.get() or 0), thr

    def apply_item(self):
        i = self.selected_index()
        if i is None:
            return
        try:
            name, cat, val, mn, thr = self.read_editor()
        except ValueError as e:
            messagebox.showwarning("입력 오류", str(e)); return
        it = self.cur_items()[i]
        it.update(name=name, category=cat, value=val, min=mn)
        if self.generic() and thr:
            it["thr"] = round(min(0.99, max(0.3, thr)), 2)
        self.after_items_changed(i)

    def add_item(self):
        try:
            name, cat, val, mn, thr = self.read_editor()
        except ValueError as e:
            messagebox.showwarning("입력 오류", str(e)); return
        if self.generic():
            messagebox.showinfo("안내", "항목은 '화면에서 드래그로 등록'으로 추가하세요. (그림이 있어야 찾을 수 있습니다)")
            return
        self.items.append({"name": name, "category": cat, "value": val, "min": mn, "icon": None, "builtin": False})
        self.after_items_changed(len(self.items) - 1)

    def delete_item(self):
        i = self.selected_index()
        if i is None:
            return
        it = self.cur_items().pop(i)
        if it.get("icon") and not it.get("builtin"):
            try:
                os.remove(os.path.join(ICON_DIR, it["icon"]))
            except OSError:
                pass
        self.after_items_changed(None)

    def reset_items(self):
        if self.generic():
            if not messagebox.askyesno("확인", "등록한 이미지를 모두 지울까요?"):
                return
            for it in self.g_items:
                try:
                    os.remove(os.path.join(ICON_DIR, it["icon"]))
                except (OSError, KeyError, TypeError):
                    pass
            self.g_items = []
        else:
            if not messagebox.askyesno("확인", "표를 기본값으로 되돌릴까요? (직접 등록한 아이콘 항목도 사라집니다)"):
                return
            self.items = [d for pr in self.presets.values() for d in pr.default_items()]
        self.after_items_changed(None)

    def after_items_changed(self, select):
        self.refresh_tree()
        if select is not None:
            self.tree.selection_set(str(select))
        for pr in self.presets.values():
            pr.set_items(self.items)
        self.gen.set_items(self.g_items)
        self.update_mode_widgets()
        self.save()

    def item_by_name(self, name):
        for it in self.cur_items():
            if it["name"] == name:
                return it
        return None

    # ---------- 로그 / 큐 ----------
    def log(self, msg):
        self.q.put(("log", msg))

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", f"[{ts}] {payload}\n", self.log_tag(payload))
                    if int(self.log_box.index("end-1c").split(".")[0]) > 2000:
                        self.log_box.delete("1.0", "500.0")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "state":
                    self.set_state(*payload)
                elif kind == "slots":
                    self.show_found(*payload)
                elif kind == "minimize":
                    self.minimized = True
                    self.iconify()
                elif kind == "items":
                    self.refresh_tree()
                    self.save()
                elif kind == "done":
                    self.b_start.configure(state="normal")
                    self.b_stop.configure(state="disabled")
                    if "달성" not in self.state_var.get():
                        self.set_state("대기")
                    if self.minimized:
                        self.minimized = False
                        self.deiconify(); self.lift()
                elif kind == "success":
                    self.set_state("달성!", "#4ade80")
                    if self.minimized:
                        self.minimized = False
                        self.deiconify(); self.lift()
                    messagebox.showinfo("달성", payload)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    # ---------- 분석 결과 표시 ----------
    def debug_image(self, f, found):
        img = f.copy()
        if self.generic():
            for h in found:
                cv2.rectangle(img, (h.x, h.y), (h.x + h.w, h.y + h.h), (0, 220, 0), 2)
                cv2.putText(img, f"{h.idx + 1} {h.score:.2f}", (h.x, max(12, h.y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            p = self.cfg.get("g_action")
            if p:
                cv2.circle(img, (int(p[0] * f.shape[1]), int(p[1] * f.shape[0])), 10, (255, 0, 255), 2)
            return img
        return self.preset.debug_image(img, found)

    def crop_for(self, f, s):
        if self.preset and not isinstance(s, Hit):
            return self.preset.crop_for(f, s)
        m = 20
        y0, y1 = max(0, s.y - m), min(f.shape[0], s.y + s.h + m)
        x0, x1 = max(0, s.x - m), min(f.shape[1], s.x + s.w + m)
        return f[y0:y1, x0:x1]

    def clear_slots(self):
        for i, (img, txt) in enumerate(self.slot_labels):
            img.good = False
            img.configure(image="", width=12, height=5, bg=COL["thumb"])
            txt.configure(text=f"{i + 1}", fg=COL["sub"])
        self.thumbs = [None] * 10

    def show_found(self, f, found):
        self.last_frame, self.last_found = f, found
        self.clear_slots()
        for s in found[:10]:
            crop = self.crop_for(f, s)
            if crop.size == 0:
                continue
            ph = tk.PhotoImage(data=png_b64(cv2.resize(crop, (96, 90), interpolation=cv2.INTER_AREA)))
            self.thumbs[s.idx] = ph
            img, txt = self.slot_labels[s.idx]
            img.good = bool(s.good)
            img.configure(image=ph, width=96, height=90, bg=COL["good"] if s.good else COL["thumb"])
            name = s.name or ("—" if getattr(s, "skipped", False) else "미식별")
            head = f"{s.idx + 1}. {s.flags()}\n" if hasattr(s, "flags") else f"{s.idx + 1}.\n"
            txt.configure(text=f"{head}{name[:11]} ({s.score:.2f})", fg=COL["ok"] if s.good else COL["sub"])

    def register_slot(self, idx):
        if self.generic():
            messagebox.showinfo("안내", "이미지 매칭 모드에서는 '화면에서 드래그로 등록'을 사용하세요."); return
        if self.last_frame is None:
            messagebox.showinfo("안내", "먼저 '현재 화면 분석'을 실행하세요."); return
        pr = self.preset
        crop = pr.icon_crop(self.last_frame, idx)
        if crop is None:
            return
        slot = self.last_found[idx] if self.last_found and idx < len(self.last_found) else None
        self.item_dialog(f"슬롯 {idx + 1} 아이콘 등록", crop, slot.name if slot and slot.name else "", pr.register_category, self.items, None,
                         after=lambda n: (self.log(f"슬롯 {idx + 1} 아이콘을 '{n}'(으)로 등록했습니다."),
                                          self.show_found(self.last_frame, pr.analyze(self.last_frame))))

    def item_dialog(self, title, crop, preset_name, preset_cat, items, extra, after=None):
        """아이콘/템플릿 등록 공용 대화상자. extra = 이미지 매칭용 {'ref_w', 'ref_h': 등록 당시 캡처 크기} 또는 None."""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self); dlg.grab_set()
        prev = crop
        if prev.shape[0] > 260 or prev.shape[1] > 260:
            s = 260.0 / max(prev.shape[0], prev.shape[1])
            prev = cv2.resize(prev, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        ph = tk.PhotoImage(data=png_b64(prev))
        tk.Label(dlg, image=ph).grid(row=0, column=0, rowspan=4, padx=6, pady=6)
        dlg.ph = ph
        ttk.Label(dlg, text="이름 (기존 항목 선택 또는 새 이름)").grid(row=0, column=1, sticky="w", padx=4)
        name = ttk.Combobox(dlg, values=[it["name"] for it in items], width=32)
        name.grid(row=1, column=1, padx=4, sticky="w")
        name.set(preset_name)
        f2 = ttk.Frame(dlg); f2.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(f2, text="분류").grid(row=0, column=0)
        cat = ttk.Combobox(f2, values=self.categories(), width=8); cat.set(preset_cat); cat.grid(row=0, column=1, padx=3)
        ttk.Label(f2, text="점수").grid(row=0, column=2)
        val = ttk.Spinbox(f2, from_=0, to=999, width=5); val.set("5"); val.grid(row=0, column=3, padx=3)
        ttk.Label(f2, text="최소").grid(row=0, column=4)
        mn = ttk.Spinbox(f2, from_=0, to=99, width=4); mn.set("0"); mn.grid(row=0, column=5, padx=3)
        thr = None
        if extra is not None:
            ttk.Label(f2, text="임계").grid(row=0, column=6)
            thr = ttk.Spinbox(f2, from_=0.5, to=0.99, increment=0.01, width=5, format="%.2f"); thr.set(f"{self.cfg.get('g_thr', 0.8):.2f}"); thr.grid(row=0, column=7, padx=3)

        def fill(_=None):
            for it in items:
                if it["name"] == name.get().strip():
                    cat.set(it["category"]); val.set(str(it["value"])); mn.set(str(it["min"]))
                    if thr is not None and it.get("thr"):
                        thr.set(f"{it['thr']:.2f}")

        name.bind("<<ComboboxSelected>>", fill)
        fill()

        def ok():
            n = name.get().strip()
            if not n:
                messagebox.showwarning("입력 오류", "이름을 입력하세요", parent=dlg); return
            fn = ("g_" if extra is not None else "u_") + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".png"
            imwrite(os.path.join(ICON_DIR, fn), crop)
            fields = dict(category=cat.get().strip() or "기타", value=int(val.get() or 0), min=int(mn.get() or 0), icon=fn, builtin=False)
            if extra is not None:
                fields["thr"] = round(float(thr.get() or self.cfg.get("g_thr", 0.8)), 2)
                fields.update(extra)
            it = next((x for x in items if x["name"] == n), None)
            if it:
                if it.get("icon") and not it.get("builtin"):
                    try:
                        os.remove(os.path.join(ICON_DIR, it["icon"]))
                    except OSError:
                        pass
                it.update(fields)
            else:
                items.append(dict(name=n, **fields))
            self.after_items_changed(None)
            dlg.destroy()
            if after:
                after(n)

        ttk.Button(dlg, text="저장", command=ok).grid(row=3, column=1, sticky="e", padx=4, pady=6)

    # ---------- 범용 모드: 화면에서 지정 ----------
    def pick_on_screen(self, kind, done):
        """대상 화면을 캡처해 그 자리에 띄우고 점(kind='point') 또는 사각형(kind='rect')을 고르게 한다."""
        if self.worker and self.worker.is_alive():
            return
        self.collect_cfg()
        try:
            hwnd, rect = self.resolve()
            self.ctl.setup(self.cfg)
            f = self.ctl.capture(hwnd, rect)
        except Exception as e:
            self.log(f"캡처 실패: {e}"); return
        l, t, w, h = rect
        ov = tk.Toplevel(self)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.geometry(f"{w}x{h}+{l}+{t}")
        ph = tk.PhotoImage(data=png_b64(f))
        ov.ph = ph
        cv = tk.Canvas(ov, width=w, height=h, highlightthickness=0, cursor="crosshair")
        cv.pack()
        cv.create_image(0, 0, image=ph, anchor="nw")
        hint = "클릭할 위치를 누르세요 (Esc 취소)" if kind == "point" else "찾을 이미지를 드래그로 감싸세요 (Esc 취소)"
        cv.create_text(w // 2 + 1, 25, text=hint, fill="black", font=("Malgun Gothic", 14, "bold"))
        cv.create_text(w // 2, 24, text=hint, fill="yellow", font=("Malgun Gothic", 14, "bold"))
        st = {"x": 0, "y": 0, "r": None}

        def finish(*args):
            ov.destroy()
            done(*args)

        if kind == "point":
            cv.bind("<ButtonRelease-1>", lambda e: finish((min(max(e.x, 0), w - 1) / w, min(max(e.y, 0), h - 1) / h, w, h)))
        else:
            def down(e):
                st["x"], st["y"] = e.x, e.y
                st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

            def move(e):
                if st["r"]:
                    cv.coords(st["r"], st["x"], st["y"], e.x, e.y)

            def up(e):
                x0, x1 = sorted((max(0, min(st["x"], w)), max(0, min(e.x, w))))
                y0, y1 = sorted((max(0, min(st["y"], h)), max(0, min(e.y, h))))
                if x1 - x0 < 6 or y1 - y0 < 6:
                    ov.destroy(); self.log("선택이 너무 작습니다."); return
                finish(f[y0:y1, x0:x1].copy(), f.shape[1], f.shape[0])

            cv.bind("<ButtonPress-1>", down)
            cv.bind("<B1-Motion>", move)
            cv.bind("<ButtonRelease-1>", up)
        ov.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

    def pick_point(self, key):
        def done(fr):
            self.cfg[key] = [round(fr[0], 4), round(fr[1], 4), int(fr[2]), int(fr[3])]
            self.update_gen_labels()
            self.save()
            self.log(f"{'클릭할 버튼' if key == 'g_action' else '확인 버튼'} 위치 지정: ({fr[0] * 100:.1f}%, {fr[1] * 100:.1f}%)")

        self.pick_on_screen("point", done)

    def pick_template(self):
        def done(crop, ref_w, ref_h):
            if crop.shape[0] < 12 or crop.shape[1] < 12:
                self.log("이미지가 너무 작습니다(12px 미만). 조금 더 크게 감싸세요.")
            self.item_dialog("찾을 이미지 등록", crop, "", "중요", self.g_items, {"ref_w": int(ref_w), "ref_h": int(ref_h)},
                             after=lambda n: self.log(f"이미지 '{n}' 등록 ({crop.shape[1]}x{crop.shape[0]}). '현재 화면 분석'으로 인식을 확인하세요."))

        self.pick_on_screen("rect", done)

    # ---------- 판정 ----------
    def evaluate(self, found):
        c = self.cfg
        by = {it["name"]: it for it in self.cur_items()}
        total, per, cats = 0, {}, {}
        for s in found:
            if not s.good:
                continue
            it = by.get(s.name) if s.name else None
            if it:
                total += it["value"]
                cats[it["category"]] = cats.get(it["category"], 0) + 1
                per[it["name"]] = per.get(it["name"], 0) + 1
            else:
                b = self.preset.bonus(s) if self.preset else None
                if b:
                    total += b[0]
                    cats[b[1]] = cats.get(b[1], 0) + 1
                else:
                    total += int(c.get("unknown_value", 1))
        conds = []
        if c["use_value"]:
            conds.append((total >= c["value"], f"점수 {total}/{c['value']}"))
        if c["use_cat_a"]:
            n = cats.get(c["cat_a_name"], 0)
            conds.append((n >= c["cat_a"], f"{c['cat_a_name']} {n}/{c['cat_a']}"))
        if c["use_cat_b"]:
            n = cats.get(c["cat_b_name"], 0)
            conds.append((n >= c["cat_b"], f"{c['cat_b_name']} {n}/{c['cat_b']}"))
        for it in self.cur_items():
            if it["min"] > 0:
                n = per.get(it["name"], 0)
                conds.append((n >= it["min"], f"{it['name']} {n}/{it['min']}"))
        if not conds:
            return False, f"점수 {total} (조건 없음)", 0
        met = all(ok for ok, _ in conds) if c["logic"] == "AND" else any(ok for ok, _ in conds)
        return met, " | ".join(("✔" if ok else "✘") + t for ok, t in conds), len(conds)

    def summary(self, found):
        if self.generic():
            cnt = {}
            for h in found:
                cnt[h.name] = cnt.get(h.name, 0) + 1
            return ", ".join(f"{n[:10]}x{k}" for n, k in cnt.items()) or "일치 없음"
        return self.preset.summary(found)

    def analyze_frame(self, f, full=True):
        """full=False면 프리셋이 판정에 필요한 만큼만 분석한다(자동 반복용)."""
        return self.gen.analyze(f, self.cfg["g_thr"]) if self.generic() else self.preset.analyze(f, full)

    def frame(self, hwnd, rect):
        raw = self.ctl.capture(hwnd, rect)
        return raw if self.generic() else self.preset.normalize(raw)

    def band(self, f):
        return band_any(f) if self.generic() else self.preset.band(f)

    def action_point(self, rect):
        if self.generic():
            p = self.cfg.get("g_action")
            return frac_to_screen(p, rect) if p else None
        return self.preset.action_point(rect)

    # ---------- 버튼 동작 ----------
    def test_once(self):
        if self.worker and self.worker.is_alive():
            return
        self.collect_cfg(); self.save()
        try:
            hwnd, rect = self.resolve()
            self.ctl.setup(self.cfg)
            f = self.frame(hwnd, rect)
        except Exception as e:
            self.log(f"캡처 실패: {e}"); return
        found = self.analyze_frame(f)
        self.show_found(f, found)
        met, detail, _ = self.evaluate(found)
        path = os.path.join(CAP_DIR, "analysis_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        imwrite(path, self.debug_image(f, found))
        if self.generic():
            self.log(f"분석: 캡처 {rect[2]}x{rect[3]} | 일치: {self.summary(found)} | 판정 그림: {path}")
            self.log(f"조건: {detail} → {'달성' if met else '미달'}")
            for it in self.g_items:
                n = sum(1 for h in found if h.name == it["name"])
                self.log(f"  {it['name']}: {n}개 (최고 일치 {self.gen.best.get(it['name'], 0):.2f} / 임계 {it.get('thr') or self.cfg['g_thr']:.2f})")
            if not self.g_items:
                self.log("  등록된 이미지가 없습니다. ②의 '화면에서 드래그로 등록'을 먼저 하세요.")
            return
        self.preset.log_analysis(f, found, rect, path)
        self.log(f"조건: {detail} → {'달성' if met else '미달'}")

    def restart_admin(self):
        self.save()
        if relaunch_as_admin():
            self.destroy()
        else:
            self.log("관리자 권한 실행이 취소되었거나 실패했습니다.")

    def mouse_test(self):
        if self.worker and self.worker.is_alive():
            return
        self.collect_cfg()
        try:
            hwnd, rect = self.resolve()
        except Exception as e:
            self.log(f"영역 오류: {e}"); return
        target = self.action_point(rect)
        if not target:
            self.log("클릭할 버튼 위치가 지정되지 않았습니다. ③의 '화면에서 클릭으로 지정'을 먼저 하세요."); return
        self.ctl.setup(self.cfg)
        if self.ctl.can_bg(hwnd):
            h, x, y = bg_target(hwnd, target)
            bg_move(hwnd, target)
            self.log(f"백그라운드 대상: '{window_title(hwnd)[:30]}' [{window_class(h)}] 클라이언트 좌표 ({x},{y}). 커서는 움직이지 않습니다. 실제 반응은 '1회 클릭 테스트'로 확인하세요.")
            return
        pos = mouse_move(*target)
        if cursor_at(target, pos):
            self.log(f"마우스 이동 성공: 버튼 위치 {target}. 대상 화면에서 커서가 버튼 위에 있는지 확인하세요.")
        else:
            self.log(f"마우스 이동 실패: 목표 {target}, 실제 {pos}. 대상 프로그램이 관리자 권한이면 이 프로그램도 관리자 권한으로 실행하세요.")

    def precheck(self):
        self.collect_cfg(); self.save()
        try:
            hwnd, rect = self.resolve()
        except Exception as e:
            messagebox.showwarning("대상 창", str(e)); return None
        if self.generic():
            if not self.cfg.get("g_action"):
                messagebox.showwarning("클릭할 버튼", "③에서 클릭할 버튼 위치를 먼저 지정하세요."); return None
            if self.cfg["g_confirm"] == "click" and not self.cfg.get("g_confirm_pos"):
                messagebox.showwarning("후속 동작", "확인 버튼 위치를 지정하거나 후속 동작을 바꾸세요."); return None
        return hwnd, rect

    def begin_worker(self, target):
        self.stop_evt.clear()
        self.b_start.configure(state="disabled"); self.b_stop.configure(state="normal")
        self.set_state("준비 중…", "#fbbf24")
        x, y = self.winfo_rootx(), self.winfo_rooty()
        self.tool_rect = (x, y, self.winfo_width(), self.winfo_height())
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if self.precheck() is None:
            return
        if self.evaluate([])[2] == 0:
            messagebox.showwarning("조건 없음", "사용 중인 종료 조건이 없습니다. 점수 합, 분류 개수 중 하나를 켜거나 표에서 '최소'를 설정하세요.")
            return
        if self.generic() and not self.g_items:
            messagebox.showwarning("이미지 없음", "②에서 찾을 이미지를 하나 이상 등록하세요.")
            return
        self.begin_worker(self.run_loop)

    def single_test(self):
        if self.worker and self.worker.is_alive():
            return
        if self.precheck() is None:
            return
        self.begin_worker(self.run_single)

    def stop(self):
        self.stop_evt.set()

    # ---------- 워커 공용 ----------
    def wait(self, sec):
        end = time.perf_counter() + sec
        while True:
            if self.stop_evt.is_set() or key_down(VK_F9):
                self.stop_evt.set()
                raise Stopped
            if time.perf_counter() >= end:
                return
            time.sleep(0.02)

    def wait_until(self, pred, timeout, step=0.1):
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if pred():
                return True
            self.wait(self.ctl.poll(step))
        return False

    def wait_user_idle(self, max_wait=1.0, quiet=0.1):
        """사용자가 마우스를 움직이거나 버튼을 누르고 있는 동안은 기다린다(최대 max_wait초)."""
        if max_wait <= 0:
            return True
        end = time.perf_counter() + max_wait
        last, since = cursor_pos(), time.perf_counter()
        while time.perf_counter() < end:
            self.wait(0.02)
            p = cursor_pos()
            if p != last or key_down(0x01) or key_down(0x02):
                last, since = p, time.perf_counter()
            elif time.perf_counter() - since >= quiet:
                return True
        return False

    def overlaps_tool(self, rect):
        tx, ty, tw, th = getattr(self, "tool_rect", (0, 0, 0, 0))
        l, t, w, h = rect
        return tx < l + w and tx + tw > l and ty < t + h and ty + th > t

    def wait_results(self, hwnd, rect):
        self.wait(self.cfg["wait_after"])
        last, stable = None, 0
        end = time.perf_counter() + 8.0
        while time.perf_counter() < end:
            f = self.frame(hwnd, rect)
            b = self.band(f)
            busy = bool(self.preset) and self.preset.busy(f)
            if last is not None and np.abs(b - last).mean() < 1.5 and not busy:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            last = b
            self.wait(self.ctl.poll(0.15))

    def do_action(self, hwnd, rect):
        """동작 1회(클릭 + 후속 동작). True=정상 진행, False=대상 반응 없음, None=마우스를 못 움직임."""
        target = self.action_point(rect)
        with self.ctl.take(hwnd):
            if not self.ctl.click(hwnd, target):
                self.log(f"마우스를 움직일 수 없습니다(목표 {target}, 실제 {cursor_pos()}). 대상 프로그램이 관리자 권한이면 이 프로그램도 관리자 권한으로 실행하세요.")
                return None
            if self.generic():
                c = self.cfg
                if c["g_confirm"] != "none":
                    self.wait(max(0.0, c["g_confirm_delay"]))
                    if c["g_confirm"] == "enter":
                        self.ctl.key(hwnd, VK_RETURN)
                    else:
                        self.ctl.click(hwnd, frac_to_screen(c["g_confirm_pos"], rect))
                return True
            return self.preset.do_action(hwnd, rect)

    def prepare_run(self):
        c = self.cfg
        self.ctl.setup(c)
        hwnd, rect = self.resolve()
        if self.ctl.cur_input == "bg" and not hwnd:
            self.ctl.switch_to_fg("대상 창을 찾지 못했습니다(화면 전체/영역 지정)")
        if self.overlaps_tool(rect):
            self.q.put(("minimize", None))
        self.log(f"입력: {self.ctl.describe()} | 모드: {label_of(self.modes(), self.mode())}" + (f" | 대상 창: {window_title(hwnd)[:30]}" if hwnd else ""))
        return hwnd, rect

    def run_single(self):
        try:
            hwnd, rect = self.prepare_run()
            self.wait(0.5)
            before = self.band(self.frame(hwnd, rect))
            res = self.do_action(hwnd, rect)
            if res is None:
                return
            self.wait_results(hwnd, rect)
            diff = float(np.abs(self.band(self.frame(hwnd, rect)) - before).mean())
            if res is False or diff < 1.0:
                self.log(f"1회 테스트: 화면 변화 없음(차이 {diff:.1f}). 이 입력 방식은 이 프로그램에 안 먹는 것 같습니다. 입력을 '포그라운드'로 바꾸거나 관리자 권한으로 실행해 보세요.")
            else:
                self.log(f"1회 테스트: 화면이 바뀌었습니다(차이 {diff:.1f}). 이 입력 방식으로 진행해도 됩니다.")
        except Stopped:
            self.log("정지했습니다.")
        except Exception as e:
            self.log(f"오류: {e}")
        finally:
            self.q.put(("done", None))

    def run_loop(self):
        c = self.cfg
        kernel32.SetThreadExecutionState(0x80000003)
        try:
            for i in (3, 2, 1):
                self.log(f"{i}초 후 시작합니다. (F9: 즉시 정지)")
                self.wait(1)
            hwnd, rect = self.prepare_run()
            if self.ctl.cur_input == "fg" and hwnd:
                with self.ctl.take(hwnd):
                    self.wait(0.2)
            self.wait(0.6)
            prev, same, no_react = None, 0, 0
            timing, t_cycle = "", time.perf_counter()
            for n_iter in range(1, c["max_iter"] + 1):
                t_a = time.perf_counter()
                hwnd, rect = self.resolve()
                f = self.frame(hwnd, rect)
                if self.preset:
                    f = self.preset.before_action(hwnd, rect, f)
                    if f is None:
                        break
                found = self.analyze_frame(f, full=False)
                if self.preset:
                    self.preset.post_analyze(hwnd, rect, f, found)
                t_an = time.perf_counter() - t_a
                self.q.put(("slots", (f, found)))
                met, detail, _ = self.evaluate(found)
                self.q.put(("state", (f"실행 중 · {n_iter}회 ({self.ctl.describe().split('(')[0].strip()})", "#7cb7ff")))
                self.log(f"[{n_iter}회] {self.summary(found)} | {detail}{timing}")
                if met:
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(CAP_DIR, f"success_{ts}.png")
                    imwrite(path, self.ctl.capture(hwnd, rect))
                    self.log(f"조건 달성! 자동 반복을 멈춥니다. 화면 저장: {path}")
                    for _ in range(3):
                        winsound.Beep(1600, 250)
                        time.sleep(0.1)
                    self.q.put(("success", f"조건을 달성해 멈췄습니다.\n{detail}"))
                    break
                b = self.band(f)
                if prev is not None and np.abs(b - prev).mean() < 1.0:
                    same += 1
                else:
                    same = 0
                prev = b
                if same >= 2 and self.ctl.auto and self.ctl.cur_input == "bg":
                    self.ctl.switch_to_fg("백그라운드 입력에 대상이 반응하지 않습니다")
                    same, prev = 0, None
                elif same >= 3:
                    self.log("화면이 바뀌지 않습니다(클릭이 들어가지 않는 듯). 관리자 권한 실행 여부와 입력 방식을 확인하세요. 중지합니다.")
                    break
                self.ctl.idle_time = 0.0
                t_r = time.perf_counter()
                res = self.do_action(hwnd, rect)
                t_act = time.perf_counter() - t_r
                if res is None:
                    break
                if res is False:
                    no_react += 1
                    if no_react >= 2 and self.ctl.auto and self.ctl.cur_input == "bg":
                        self.ctl.switch_to_fg("백그라운드 입력에 대상이 반응하지 않습니다")
                        no_react = 0
                    else:
                        self.log("다시 시도합니다.")
                    self.wait(0.5)
                    timing, t_cycle = "", time.perf_counter()
                    continue
                no_react = 0
                t_w = time.perf_counter()
                self.wait_results(hwnd, rect)
                t_wait = time.perf_counter() - t_w
                idle = self.ctl.idle_time
                total, t_cycle = time.perf_counter() - t_cycle, time.perf_counter()
                timing = f" | {total:.1f}초 (분석 {t_an:.1f} · 마우스 대기 {idle:.1f} · 클릭·확인 {max(0.0, t_act - idle):.1f} · 결과 대기 {t_wait:.1f})"
            else:
                self.log("최대 횟수에 도달했습니다.")
        except Stopped:
            self.log("정지했습니다.")
        except Exception as e:
            self.log(f"오류: {e}")
        finally:
            kernel32.SetThreadExecutionState(0x80000000)
            self.q.put(("done", None))

    def on_close(self):
        self.stop_evt.set()
        self.save()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
