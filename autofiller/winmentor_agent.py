import ctypes
import importlib
import io
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import uuid
import zipfile
import re
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, cast

import cv2
import numpy as np
import pyautogui  # type: ignore
import pygetwindow as gw  # type: ignore
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

BASE_DIR: Path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)

# ---------------------------------------------------------------------------
# DPI awareness. Must run BEFORE pyautogui / PIL touch the screen, otherwise
# screenshots, mouse coordinates and template scale can disagree on PCs that
# use display scaling (125% / 150%).
# ---------------------------------------------------------------------------
def _enable_dpi_awareness() -> str:
    try:
        hr = int(ctypes.windll.shcore.SetProcessDpiAwareness(2))  # per-monitor
        if hr == 0:
            return "per-monitor (set by agent)"
        if (hr & 0xFFFFFFFF) == 0x80070005:
            return "already set before agent code ran"
        return f"shcore returned HRESULT {hr & 0xFFFFFFFF:#x}"
    except Exception as e1:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system (user32 fallback)"
        except Exception as e2:
            return f"FAILED: {e1!r} / {e2!r}"


DPI_AWARENESS_RESULT: str = _enable_dpi_awareness()


def _probe_imports() -> None:
    """If a dependency was not bundled by PyInstaller, leave a readable file
    next to the exe instead of a console window that flashes and closes."""
    try:
        for name in ("cv2", "numpy", "pyautogui", "PIL", "fastapi", "uvicorn"):
            importlib.import_module(name)
    except Exception:
        (BASE_DIR / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


_probe_imports()

# Changed to BGR to preserve red text formatting crucial for WinMentor
BGRImage = NDArray[np.uint8]

# --- Safety & Vision Settings ---
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1

ASSETS_DIR: Path = BASE_DIR / "assets"
DEBUG_DIR: Path = BASE_DIR / "debug_vision"

CONFIDENCE: float = float(os.getenv("VISION_CONFIDENCE", "0.80"))
TITLE_HINT: str = os.getenv("WINMENTOR_TITLE", "MENTOR").upper()
EXE_HINT: str = os.getenv("WINMENTOR_EXE", "mentor").lower()
ALLOWED_ORIGINS: List[str] = [
    o.strip() for o in os.getenv("AGENT_ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

IGNORED_EXES = {
    "conhost.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "code.exe",
}
IGNORED_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}
SWEEP_SCALES: List[float] = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]

# Layer 3: Restrict searches to expected UI zones (y_min, y_max, x_min, x_max as screen ratios)
ROI_RULES: Dict[str, Tuple[float, float, float, float]] = {
    "Intrari.png": (0.0, 0.18, 0.0, 0.40),
    "Facturi.png": (0.05, 0.40, 0.0, 0.35),
    "Interne.png": (0.0, 0.18, 0.05, 0.45),
    "Bonuri_consum.png": (0.05, 0.45, 0.05, 0.35),
    "anchor_furnizor.png": (0.15, 0.45, 0.0, 0.40),
    "anchor_numar.png": (0.20, 0.50, 0.25, 0.65),
    "anchor_data.png": (0.25, 0.55, 0.25, 0.65),
    "btn_plus_grid.png": (0.35, 0.55, 0.65, 0.85),
    "title_articole.png": (0.50, 0.75, 0.10, 0.35),
    "anchor_denumire_nou.png": (0.15, 0.35, 0.20, 0.40),
    "anchor_cod_extern.png": (0.50, 0.70, 0.45, 0.65),
    "cod_fiscal.png": (0.15, 0.60, 0.10, 0.90),      # Broad ROI for the popup columns
    "title_furnizori.png": (0.15, 0.60, 0.10, 0.90), # Title bar of the supplier search window
}

# ---------------------------------------------------------------------------
# Logging: always to file (works with or without a console), ASCII only.
# ---------------------------------------------------------------------------
def _resolve_log_dir() -> Path:
    primary = BASE_DIR / "logs"
    try:
        primary.mkdir(exist_ok=True)
        return primary
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "winmentor_agent_logs"
        fallback.mkdir(exist_ok=True)
        return fallback


LOG_DIR: Path = _resolve_log_dir()


def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(
        LOG_DIR / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # None when built with --noconsole
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return logging.getLogger("agent")


log: logging.Logger = _setup_logging()


# ---------------------------------------------------------------------------
# Win32 helpers (ctypes only, no pygetwindow)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
shell32 = ctypes.windll.shell32

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetConsoleWindow.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]

SW_MAXIMIZE: int = 3
SW_RESTORE: int = 9
SW_MINIMIZE: int = 6
WM_SYSCOMMAND: int = 0x0112
SC_RESTORE: int = 0xF120
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_exe(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def process_elevated(pid: int) -> Optional[bool]:
    """True/False if known, None if the token could not be opened
    (from a non-elevated process this usually means the target IS elevated)."""
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    tok = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            return None
        elev = wintypes.DWORD()
        ret = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret)
        )  # 20 = TokenElevation
        kernel32.CloseHandle(tok)
        return bool(elev.value) if ok else None
    finally:
        kernel32.CloseHandle(h)


def window_info(hwnd: int) -> Dict[str, Any]:
    n = int(user32.GetWindowTextLengthW(hwnd))
    title = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, title, n + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "title": title.value,
        "class": cls.value,
        "pid": int(pid.value),
        "exe": process_exe(int(pid.value)),
        "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
        "minimized": bool(user32.IsIconic(hwnd)),
        "maximized": bool(user32.IsZoomed(hwnd)),
    }


def enum_windows() -> List[int]:
    handles: List[int] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and int(user32.GetWindowTextLengthW(hwnd)) > 0:
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return handles


def find_winmentor_window() -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (chosen window or None, every window whose title matched).
    Our own console window has the exe path (...winmentor_agent.exe) as its
    title, so a naive title match can pick it instead of WinMentor."""
    console = int(kernel32.GetConsoleWindow() or 0)
    candidates: List[Dict[str, Any]] = []
    for hwnd in enum_windows():
        info = window_info(hwnd)
        if TITLE_HINT not in str(info["title"]).upper():
            continue
        reason: Optional[str] = None
        if hwnd == console:
            reason = "own console window"
        elif info["pid"] == os.getpid():
            reason = "own process"
        elif info["class"] in IGNORED_CLASSES:
            reason = f"console class {info['class']}"
        elif info["exe"] in IGNORED_EXES:
            reason = f"ignored exe {info['exe']}"
        info["excluded"] = reason
        candidates.append(info)

    usable = [c for c in candidates if not c["excluded"]]
    preferred = [c for c in usable if EXE_HINT and EXE_HINT in str(c["exe"])]
    if preferred:
        return preferred[0], candidates
    if usable:
        return usable[0], candidates
    return None, candidates


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def windows_of_pid(pid: int) -> List[Dict[str, Any]]:
    return [w for w in (window_info(h) for h in enum_windows()) if w["pid"] == pid]


def _area(w: Dict[str, Any]) -> int:
    r = cast(List[int], w["rect"])
    return (int(r[2]) - int(r[0])) * (int(r[3]) - int(r[1]))


def pick_main_window(pid: int) -> Optional[Dict[str, Any]]:
    """Delphi apps keep a hidden 'TApplication' window (it owns the taskbar button).
    The real main form is a different top-level window of the same process."""
    wins = [w for w in windows_of_pid(pid) if w["class"] != "TApplication"]
    return max(wins, key=_area) if wins else None


def minimize_own_windows() -> int:
    """Minimizes the agent's own console/windows so they can never sit on top of
    WinMentor and corrupt the screenshots the vision step matches against.
    Returns how many windows were minimized."""
    targets: set[int] = {
        int(w["hwnd"]) for w in windows_of_pid(os.getpid()) if not w["minimized"]
    }
    console = int(kernel32.GetConsoleWindow() or 0)
    if console and user32.IsWindowVisible(console) and not user32.IsIconic(console):
        targets.add(console)
    for h in targets:
        user32.ShowWindow(h, SW_MINIMIZE)
    if targets:
        log.info("Minimized %d agent window(s): %s", len(targets), sorted(targets))
        time.sleep(0.5)
    return len(targets)


def focus_winmentor() -> bool:
    """Finds WinMentor's main form, restores/maximizes it, forces it to the foreground
    and VERIFIES it. Returns False if keystrokes could end up in the wrong window."""
    log.info(
        "Screen: pyautogui.size()=%s, DPI awareness=%s", tuple(pyautogui.size()), DPI_AWARENESS_RESULT
    )
    minimize_own_windows()
    chosen, candidates = find_winmentor_window()
    for c in candidates:
        log.info("Window candidate: %s", json.dumps(c, ensure_ascii=True))
    if chosen is None:
        log.error(
            "No usable WinMentor window found (title hint=%r, exe hint=%r).", TITLE_HINT, EXE_HINT
        )
        for hwnd in enum_windows():
            w = window_info(hwnd)
            if not w["minimized"]:
                log.info("Visible window: title=%r class=%s exe=%s", w["title"], w["class"], w["exe"])
        return False

    app_hwnd = int(chosen["hwnd"])
    pid = int(chosen["pid"])
    elevated = process_elevated(pid)
    if not is_admin() and elevated is not False:
        log.warning(
            "WinMentor may be running elevated (token check=%s) while the agent is not. "
            "Windows will block clicks/keys/ShowWindow. Run the agent as administrator.",
            elevated,
        )

    main = pick_main_window(pid)
    if main is None or main["minimized"]:
        log.info("Main form hidden/minimized; sending SC_RESTORE to the TApplication window")
        user32.PostMessageW(app_hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
        time.sleep(1.0)
        main = pick_main_window(pid)
        if main is None:
            log.info("No main form yet; trying ShowWindow(SW_RESTORE) on the TApplication window")
            user32.ShowWindow(app_hwnd, SW_RESTORE)
            time.sleep(1.0)
            main = pick_main_window(pid)
    for w in windows_of_pid(pid):
        log.info("WinMentor process window: %s", json.dumps(w, ensure_ascii=True))
    if main is None:
        log.error("Could not find the WinMentor main form (only the TApplication window exists).")
        return False

    hwnd = int(main["hwnd"])
    log.info("Main form: hwnd=%d title=%r class=%s", hwnd, main["title"], main["class"])
    if user32.IsIconic(hwnd):
        log.info("Restoring main form")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)
    if not user32.IsZoomed(hwnd):
        log.info("Maximizing main form")
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        time.sleep(0.5)

    in_front = False
    for attempt in range(1, 4):
        fg = int(user32.GetForegroundWindow() or 0)
        cur_tid = int(kernel32.GetCurrentThreadId())
        fg_tid = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        time.sleep(0.6)
        fg_now = int(user32.GetForegroundWindow() or 0)
        # Accept any window of the WinMentor process (main form or one of its dialogs).
        in_front = fg_now != 0 and window_pid(fg_now) == pid
        if in_front:
            break
        log.warning("Foreground attempt %d failed (foreground=%s)", attempt, window_info(fg_now) if fg_now else None)

    final = window_info(hwnd)
    log.info(
        "After focus: maximized=%s foreground=%s rect=%s", final["maximized"], in_front, final["rect"]
    )
    if not in_front:
        log.error(
            "Could not bring WinMentor to the foreground; aborting to avoid typing into another window."
        )
        return False
    if not final["maximized"]:
        log.warning("Main form is in front but NOT maximized; vision matching may fail.")
    if minimize_own_windows():
        log.warning("An agent window was visible after focusing WinMentor; minimized it again.")
    return True


# ---------------------------------------------------------------------------
# Vision: Updated for BGR Color matching and Region Of Interest (ROI)
# ---------------------------------------------------------------------------
class Match(NamedTuple):
    score: float
    x: int  # centre
    y: int
    w: int
    h: int


_template_cache: Dict[str, tuple[float, BGRImage]] = {}
_last_miss: Dict[str, Optional[Match]] = {}


def load_template(name: str) -> BGRImage:
    path = ASSETS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Asset missing: {path}")
    mtime = path.stat().st_mtime
    cached = _template_cache.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    
    # Load in COLOR (BGR) to preserve red letters in WinMentor
    raw = np.fromfile(str(path), dtype=np.uint8)
    decoded = cast("BGRImage | None", cv2.imdecode(raw, cv2.IMREAD_COLOR))
    if decoded is None:
        raise ValueError(f"Could not decode image: {path}")
    _template_cache[name] = (mtime, decoded)
    return decoded


def grab_bgr() -> BGRImage:
    """Takes a native screen snapshot and converts to OpenCV BGR format."""
    rgb = np.array(pyautogui.screenshot())
    return cast(BGRImage, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def best_match(name: str, screen: BGRImage, scale: float = 1.0) -> Optional[Match]:
    tmpl = load_template(name)
    if scale != 1.0:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        tmpl = cast(BGRImage, cv2.resize(tmpl, None, fx=scale, fy=scale, interpolation=interp))
    th = int(tmpl.shape[0])
    tw = int(tmpl.shape[1])
    
    # Apply ROI logic to restrict the search area, reducing false positives
    roi_bounds = ROI_RULES.get(name)
    if roi_bounds:
        screen_h, screen_w = screen.shape[:2]
        y1, y2 = int(screen_h * roi_bounds[0]), int(screen_h * roi_bounds[1])
        x1, x2 = int(screen_w * roi_bounds[2]), int(screen_w * roi_bounds[3])
        sub_screen = screen[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1
    else:
        sub_screen = screen
        offset_x, offset_y = 0, 0

    if th > int(sub_screen.shape[0]) or tw > int(sub_screen.shape[1]):
        return None
        
    res = cv2.matchTemplate(sub_screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
    
    # Return match coordinates adjusted to full screen scale
    return Match(
        float(max_val), 
        int(max_loc[0]) + offset_x + tw // 2, 
        int(max_loc[1]) + offset_y + th // 2, 
        tw, 
        th
    )


def locate_image(img_name: str, timeout: float = 0.0) -> tuple[int, int] | None:
    """Polls the screen for an asset. Missing assets / OpenCV errors RAISE."""
    start = time.time()
    best: Optional[Match] = None
    while True:
        m = best_match(img_name, grab_bgr())
        if m is not None and (best is None or m.score > best.score):
            best = m
        if m is not None and m.score >= CONFIDENCE:
            return (m.x, m.y)
        if (time.time() - start) >= timeout:
            break
        time.sleep(0.3)
    _last_miss[img_name] = best
    return None


def save_snapshot(prefix: str, label: str, best: Optional[Match] = None) -> Optional[Path]:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe = Path(label).stem
        path = DEBUG_DIR / f"{prefix}_{safe}_{ts}.png"
        shot = pyautogui.screenshot()
        if best is not None:
            ImageDraw.Draw(shot).rectangle(
                (
                    best.x - best.w // 2,
                    best.y - best.h // 2,
                    best.x + best.w // 2,
                    best.y + best.h // 2,
                ),
                outline="red",
                width=3,
            )
        shot.save(str(path))
        tpl = ASSETS_DIR / label
        if tpl.exists():
            shutil.copy(tpl, DEBUG_DIR / f"{prefix}_{safe}_{ts}_template.png")
        return path
    except Exception:
        log.exception("Could not save debug snapshot")
        return None


def take_debug_screenshot(failed_img_name: str) -> None:
    best = _last_miss.get(failed_img_name)
    path = save_snapshot("FAILED", failed_img_name, best)
    if best is None:
        log.error(
            "Could not find '%s' (template larger than screen?). Snapshot: %s", failed_img_name, path
        )
    else:
        log.error(
            "Could not find '%s': best score %.3f at (%d,%d), threshold %.2f. "
            "Snapshot (red box = best guess): %s",
            failed_img_name, best.score, best.x, best.y, CONFIDENCE, path,
        )


def click_image(img_name: str, offset_x: int = 0, offset_y: int = 0, timeout: float = 10.0) -> None:
    pos = locate_image(img_name, timeout=timeout)
    if pos is None:
        take_debug_screenshot(img_name)
        raise TimeoutError(f"Could not find {img_name} on screen after {timeout} seconds.")
    log.info("click %s at %s offset=(%d,%d)", img_name, pos, offset_x, offset_y)
    pyautogui.moveTo(pos[0] + offset_x, pos[1] + offset_y, duration=0.2)
    pyautogui.click()
    time.sleep(0.2)


def wait_for_vanish(img_name: str, check_interval: float = 0.5, max_wait: float = 1800.0, crop_left_half: bool = False) -> None:
    """Waits for an element to disappear. If crop_left_half is True, it dynamically chops the 
    template to ignore any text that was typed into the neighboring input field."""
    log.info("Waiting for operator (until %s disappears)", img_name)
    start = time.time()
    
    tmpl = load_template(img_name)
    if crop_left_half:
        _, w = tmpl.shape[:2]
        tmpl = tmpl[:, :int(w * 0.45)] # Keep only the left 45% (the red label)

    misses = 0
    while True:
        if time.time() - start > max_wait:
            raise TimeoutError(f"Operator did not close {img_name} within {max_wait:.0f}s.")
            
        screen = grab_bgr()
        roi_bounds = ROI_RULES.get(img_name)
        if roi_bounds:
            screen_h, screen_w = screen.shape[:2]
            y1, y2 = int(screen_h * roi_bounds[0]), int(screen_h * roi_bounds[1])
            x1, x2 = int(screen_w * roi_bounds[2]), int(screen_w * roi_bounds[3])
            sub_screen = screen[y1:y2, x1:x2]
        else:
            sub_screen = screen
            
        if tmpl.shape[0] <= sub_screen.shape[0] and tmpl.shape[1] <= sub_screen.shape[1]:
            res = cv2.matchTemplate(sub_screen, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
        else:
            max_val = 0.0
            
        if max_val < CONFIDENCE:
            misses += 1
            if misses >= 3: # Require it to be missing for ~1.5 seconds to avoid flicker bugs
                break
        else:
            misses = 0
            
        time.sleep(check_interval)
        
    log.info("Window closed. Resuming automation.")
    time.sleep(0.5)


def wait_for_window_close_dynamic(max_wait: float = 1800.0) -> None:
    """
    Takes a snapshot of the center of the modal window and waits for it to drastically change.
    This flawlessly detects when the operator presses Save (Alt+Z) and the modal disappears.
    """
    log.info("Waiting for operator to save and close the popup...")
    start = time.time()
    
    # Give the UI a tiny moment to settle after typing
    time.sleep(0.5)
    
    screen_w, screen_h = pyautogui.size()
    cx, cy = screen_w // 2, screen_h // 2
    
    # Grab a large 400x400 box in the center of the screen (the grey popup window)
    box_size = 400
    left = cx - (box_size // 2)
    top = cy - (box_size // 2)
    
    anchor_patch = pyautogui.screenshot(region=(left, top, box_size, box_size))
    anchor_bgr = cv2.cvtColor(np.array(anchor_patch), cv2.COLOR_RGB2BGR)
    
    misses = 0
    while True:
        if time.time() - start > max_wait:
            raise TimeoutError(f"Operator did not close the popup within {max_wait:.0f}s.")
            
        current_patch = pyautogui.screenshot(region=(left, top, box_size, box_size))
        current_bgr = cv2.cvtColor(np.array(current_patch), cv2.COLOR_RGB2BGR)
        
        res = cv2.matchTemplate(current_bgr, anchor_bgr, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        
        # When the modal closes, the grey window is replaced by the grid.
        # This causes a massive pixel change, dropping max_val far below 0.60.
        if max_val < 0.60:
            misses += 1
            if misses >= 3:
                break
        else:
            misses = 0
            
        time.sleep(0.5)
        
    log.info("Popup closed! Resuming automation.")
    time.sleep(0.5)


def type_text(value: object) -> None:
    """pyautogui.write silently DROPS non-ASCII characters (ă î ș ț ...).
    Fold diacritics to ASCII so nothing disappears unnoticed."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    pyautogui.write(text, interval=0.02)


def press_alt_shortcut(key: str) -> None:
    """Safely triggers Alt+Key shortcuts for older Delphi VCL apps.
    Adds a slight hardware delay to ensure the OS registers the intercept."""
    log.info("Pressing Alt + %s", key.upper())
    pyautogui.keyDown("alt")
    time.sleep(0.05)
    pyautogui.press(key)
    time.sleep(0.05)
    pyautogui.keyUp("alt")
    time.sleep(0.3)


# --- Data Sanitization Helpers ---

def clean_company_name(name: str) -> str:
    """Removes SC, SRL, SA prefixes/suffixes so incremental search locks onto the core name."""
    n = str(name).upper().strip()
    n = re.sub(r'^S\.?C\.?\s+', '', n)   # Strip leading SC or S.C.
    n = re.sub(r'\s+S\.?R\.?L\.?$', '', n) # Strip trailing SRL or S.R.L.
    n = re.sub(r'\s+S\.?A\.?$', '', n)   # Strip trailing SA or S.A.
    return n.strip()


def format_winmentor_date(date_str: str) -> str:
    """Converts web YYYY-MM-DD string to pure DDMMYYYY numeric stream."""
    d = str(date_str).strip()
    if "-" in d:
        parts = d.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            return f"{parts[2]}{parts[1]}{parts[0]}"
    # Fallback: strip everything except numbers
    return "".join(c for c in d if c.isdigit())


# --- WinMentor Macro Sequences ---
def add_grid_items(items: list[dict[str, Any]]) -> None:
    if not items:
        return

    # 1. Open the Catalog ONCE for the entire batch
    log.info("Opening Articole Catalog...")
    click_image("btn_plus_grid.png")
    if not locate_image("title_articole.png", timeout=5.0):
        take_debug_screenshot("title_articole.png")
        raise TimeoutError("Articole window did not open.")

    for idx, item in enumerate(items, 1):
        log.info("Item %d/%d: %s", idx, len(items), item.get("part_code"))
        
        # Check if backend flagged this part as brand new
        is_new = item.get("is_new") is True or item.get("status") == "NEW"

        # 2. If it's a NEW article, create it FIRST
        if is_new:
            log.info("API flagged as NEW. Creating article...")
            press_alt_shortcut("a")
            
            if not locate_image("anchor_denumire_nou.png", timeout=10.0):
                take_debug_screenshot("anchor_denumire_nou.png")
                raise TimeoutError("Articole de stoc definition window did not open.")

            press_alt_shortcut("d")
            type_text(str(item.get("part_name", "")).upper())

            press_alt_shortcut("x")
            type_text(str(item.get("part_code", "")).upper())

            log.info("Awaiting Operator Save (Alt+Z)...")
            
            # The dynamic watcher waits flawlessly for the window to vanish!
            wait_for_window_close_dynamic()
            
            # Since the operator pressed Save, WinMentor jumps directly to Quantity. We SKIP searching!
        else:
            # 3. Incremental Search (ONLY FOR EXISTING PARTS)
            # Type part code directly into the active Cod Extern field
            type_text(str(item.get("part_code", "")))
            time.sleep(0.2)
            pyautogui.press("enter")
        
        # 4. Tiny Popup 1: Cantitate (Quantity)
        time.sleep(0.3)
        type_text(item.get("quantity", 1))
        pyautogui.press("enter")
        
        # 5. Tiny Popup 2: Pret (Price) - Only type if > 0 (Factura Intrare)
        unit_price = item.get("unit_price", 0.0)
        if unit_price and float(unit_price) > 0:
            time.sleep(0.2)
            type_text(unit_price)
            pyautogui.press("enter")

        # Wait for the item to commit to the grid and the search box to clear itself
        time.sleep(0.4)

    # 6. Close the catalog when the loop finishes
    log.info("All items added. Closing Articole catalog.")
    pyautogui.press("esc")
    time.sleep(0.5)
    
    # Fallback to Alt+I if Esc failed
    if locate_image("title_articole.png", timeout=0.0):
        press_alt_shortcut("i")


def execute_factura_intrare(factura: dict[str, Any]) -> None:
    log.info("Starting Factura Intrare: %s", factura.get("furnizor"))
    click_image("Intrari.png")
    click_image("Facturi.png")
    
    # Wait to make sure the list rendered before sending Alt+A
    time.sleep(1.0)
    press_alt_shortcut("a")

    if not locate_image("anchor_furnizor.png", timeout=10.0):
        take_debug_screenshot("anchor_furnizor.png")
        raise TimeoutError("Factura window did not open in time.")

    # Furnizor List (Alt+F)
    press_alt_shortcut("f")
    time.sleep(0.5)

    raw_cif = factura.get("furnizor_cif")
    cif_typed = False

    # Attempt 1: Search by CIF if available
    if raw_cif:
        clean_cif = re.sub(r'[^0-9]', '', str(raw_cif))
        if clean_cif:
            log.info("Using Cod Fiscal for Supplier Search: %s", clean_cif)
            try:
                # Click the CIF column header to sort/search by it
                click_image("cod_fiscal.png", timeout=2.0)
                time.sleep(0.2)
                type_text(clean_cif)
                pyautogui.press("enter")
                time.sleep(0.5)
                cif_typed = True
            except TimeoutError:
                log.warning("cod_fiscal.png not found on screen. Falling back to Name search.")

    # Attempt 2: Search by Name (Fallback)
    if not cif_typed:
        log.info("No CIF found or CIF search failed. Defaulting to Name search.")
        clean_furnizor = clean_company_name(factura.get("furnizor", ""))
        type_text(clean_furnizor)
        pyautogui.press("enter")
        time.sleep(0.5)

    # Safety Check: Did the search window close?
    # If the supplier doesn't exist, WinMentor keeps the search popup open.
    if locate_image("title_furnizori.png", timeout=0.5):
        log.warning("Supplier not found automatically! Waiting for operator to select/create supplier...")
        wait_for_vanish("title_furnizori.png")

    # Numar (Alt+N)
    press_alt_shortcut("n")
    type_text(factura.get("numar_doc_winmentor", ""))

    # Data (Alt+D)
    press_alt_shortcut("d")
    clean_date = format_winmentor_date(factura.get("data_winmentor", ""))
    type_text(clean_date)

    add_grid_items(cast(list[dict[str, Any]], factura.get("items", [])))
    log.info("Factura typed. Awaiting operator review and Save.")


def execute_bon_consum(bon: dict[str, Any]) -> None:
    log.info("Starting Bon Consum for: %s", bon.get("cine_consuma", "General"))
    click_image("Interne.png")
    click_image("Bonuri_consum.png")
    
    time.sleep(1.0)
    press_alt_shortcut("a")
    
    if not locate_image("btn_plus_grid.png", timeout=10.0):
        take_debug_screenshot("btn_plus_grid.png")
        raise TimeoutError("Bon Consum window did not open in time.")

    time.sleep(0.5)
    add_grid_items(cast(list[dict[str, Any]], bon.get("items", [])))
    log.info("Bon Consum typed. Awaiting operator review and Save.")


# --- API ---
class AutoFillPayload(BaseModel):
    factura_intrare: Optional[Dict[str, Any]] = Field(default=None)
    bonuri_consum: List[Dict[str, Any]] = Field(default_factory=list)  # type: ignore


_run_lock = threading.Lock()


def run_automation_task(payload: AutoFillPayload, run_id: str) -> None:
    if not _run_lock.acquire(blocking=False):
        log.warning("[%s] Another run is in progress; ignoring.", run_id)
        return
    try:
        log.info(
            "[%s] Run started (factura=%s, bonuri=%d)",
            run_id, bool(payload.factura_intrare), len(payload.bonuri_consum),
        )
        if not focus_winmentor():
            log.error("[%s] Aborting: could not focus WinMentor.", run_id)
            return
            
        # --- Hands-Off Warning ---
        log.info("[%s] WinMentor focused. Starting in 3 seconds... HANDS OFF MOUSE & KEYBOARD!", run_id)
        time.sleep(3.0)

        if payload.factura_intrare:
            execute_factura_intrare(payload.factura_intrare)
        for bon in payload.bonuri_consum:
            execute_bon_consum(bon)
        log.info("[%s] Automation sequence complete.", run_id)
    except pyautogui.FailSafeException:
        log.warning("[%s] EMERGENCY ABORT (mouse in screen corner)", run_id)
    except TimeoutError as e:
        log.error("[%s] %s", run_id, e)
    except Exception:
        log.exception("[%s] Unexpected error", run_id)
        save_snapshot("ERROR", "unexpected")
    finally:
        _run_lock.release()


app = FastAPI(title="WinMentor Auto-Filler Agent")
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"]
)


@app.post("/autofill")
def trigger_autofill(
    payload: AutoFillPayload, background_tasks: BackgroundTasks
) -> dict[str, str]:
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    run_id = uuid.uuid4().hex[:8]
    log.info("[%s] Received /autofill webhook", run_id)
    background_tasks.add_task(run_automation_task, payload, run_id)
    return {
        "status": "started",
        "run_id": run_id,
        "message": "Automation dispatched to background thread.",
    }


# --- Diagnostics: open http://127.0.0.1:8001/docs on the operator PC ---
def local_only(request: Request) -> None:
    """Browsers tag cross-site requests; refuse them so a random web page
    cannot read screenshots through the permissive CORS policy."""
    if request.headers.get("sec-fetch-site", "none") in ("cross-site", "same-site"):
        raise HTTPException(status_code=403, detail="Diagnostics are local-only.")


diag = APIRouter(prefix="/diagnostics", dependencies=[Depends(local_only)])


def prepare_capture(focus: bool, delay: float) -> None:
    """The browser calling these endpoints is itself the foreground window, so
    optionally bring WinMentor to the front and/or wait before capturing."""
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A run is in progress.")
    if focus and not focus_winmentor():
        raise HTTPException(status_code=409, detail="Could not focus WinMentor (see logs).")
    time.sleep(max(0.0, min(delay, 30.0)))


def collect_diagnostics() -> Dict[str, Any]:
    shot = pyautogui.screenshot()
    sid = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    scale: Optional[int]
    try:
        scale = int(ctypes.windll.shcore.GetScaleFactorForDevice(0))
    except Exception:
        scale = None
    chosen, candidates = find_winmentor_window()
    assets: Dict[str, Any] = {}
    if ASSETS_DIR.exists():
        for p in sorted(ASSETS_DIR.glob("*.png")):
            try:
                with Image.open(p) as im:
                    assets[p.name] = [int(im.size[0]), int(im.size[1])]
            except Exception as e:
                assets[p.name] = f"unreadable: {e!r}"
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frozen_exe": bool(getattr(sys, "frozen", False)),
        "base_dir": str(BASE_DIR),
        "assets_dir_exists": ASSETS_DIR.exists(),
        "python": sys.version,
        "windows": platform.platform(),
        "versions": {
            "pyautogui": str(getattr(pyautogui, "__version__", "?")),
            "pillow": str(getattr(Image, "__version__", "?")),
            "opencv": str(cv2.__version__),
            "numpy": str(np.__version__),
        },
        "is_admin": is_admin(),
        "session_id": int(sid.value),
        "console_session_id": int(kernel32.WTSGetActiveConsoleSessionId()),
        "dpi_awareness": DPI_AWARENESS_RESULT,
        "display_scale_percent": scale,
        "monitors": int(user32.GetSystemMetrics(80)),
        "pyautogui_size": [int(pyautogui.size()[0]), int(pyautogui.size()[1])],
        "screenshot_size": [int(shot.size[0]), int(shot.size[1])],
        "confidence_threshold": CONFIDENCE,
        "assets": assets,
        "winmentor_chosen": chosen,
        "winmentor_candidates": candidates,
        "run_in_progress": _run_lock.locked(),
    }


@diag.get("")
def diag_info() -> Dict[str, Any]:
    return collect_diagnostics()


@diag.get("/screenshot")
def diag_screenshot(focus: bool = False, delay: float = 0.0) -> Response:
    prepare_capture(focus, delay)
    buf = io.BytesIO()
    pyautogui.screenshot().save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@diag.get("/match")
def diag_match(
    img: Optional[str] = None, sweep: bool = False, focus: bool = False, delay: float = 0.0
) -> Dict[str, Any]:
    """Score of every asset (or one with ?img=Intrari.png) against the current screen.
    sweep=true also tries scales 0.5x-2.0x: if a scaled score is high while the
    1.0x score is low, the templates were captured at a different display scale."""
    prepare_capture(focus, delay)
    screen = grab_bgr()
    names: List[str] = [img] if img else sorted(p.name for p in ASSETS_DIR.glob("*.png"))
    out: Dict[str, Any] = {}
    for n in names:
        try:
            m = best_match(n, screen)
            entry: Dict[str, Any] = {
                "score": None if m is None else round(m.score, 3),
                "center": None if m is None else [m.x, m.y],
                "found": m is not None and m.score >= CONFIDENCE,
            }
            if sweep or img:
                best_scale: Optional[float] = None
                best_score = -1.0
                for s in SWEEP_SCALES:
                    mm = best_match(n, screen, s)
                    if mm is not None and mm.score > best_score:
                        best_score = mm.score
                        best_scale = s
                if best_scale is not None:
                    entry["best_scale"] = best_scale
                    entry["best_scale_score"] = round(best_score, 3)
            out[n] = entry
        except Exception as e:
            out[n] = {"error": repr(e)}
    return out


@diag.post("/focus")
def diag_focus() -> Dict[str, Any]:
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A run is in progress.")
    ok = focus_winmentor()
    chosen, _ = find_winmentor_window()
    return {"focused": ok, "window": chosen}


@diag.get("/bundle")
def diag_bundle(focus: bool = False, delay: float = 0.0) -> Response:
    """One zip to send back: diagnostics, current screen, logs, recent failure snapshots."""
    prepare_capture(focus, delay)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("diagnostics.json", json.dumps(collect_diagnostics(), indent=2, default=str))
        shot = io.BytesIO()
        pyautogui.screenshot().save(shot, format="PNG")
        z.writestr("screen_now.png", shot.getvalue())
        for f in sorted(LOG_DIR.glob("agent.log*")):
            z.write(f, f"logs/{f.name}")
        if DEBUG_DIR.exists():
            for f in sorted(DEBUG_DIR.glob("*.png"))[-20:]:
                z.write(f, f"debug_vision/{f.name}")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=agent_debug_bundle.zip"},
    )


app.include_router(diag)


if __name__ == "__main__":
    import uvicorn

    try:
        d = collect_diagnostics()
        log.info("=" * 60)
        log.info("WinMentor Auto-Filler Agent ONLINE (frozen=%s)", d["frozen_exe"])
        log.info("Assets: %s (exists=%s, %d files)", ASSETS_DIR, d["assets_dir_exists"], len(d["assets"]))
        log.info(
            "Admin=%s DPI=%s scale=%s%% monitors=%s screen=%s session=%s/%s",
            d["is_admin"], d["dpi_awareness"], d["display_scale_percent"], d["monitors"],
            d["screenshot_size"], d["session_id"], d["console_session_id"],
        )
        log.info("OpenCV %s, NumPy %s", d["versions"]["opencv"], d["versions"]["numpy"])
        log.info("Diagnostics UI: http://127.0.0.1:8001/docs")
        log.info("EMERGENCY STOP: slam the mouse into any screen corner.")
        log.info("=" * 60)
    except Exception:
        log.exception("Startup diagnostics failed")

    # log_config=None: uvicorn's default logging config crashes when stdout is None (--noconsole)
    uvicorn.run(app, host="127.0.0.1", port=8001, log_config=None)