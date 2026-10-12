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
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, cast

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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pyautogui  # type: ignore  # noqa: E402
from fastapi import (  # noqa: E402
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from numpy.typing import NDArray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

GrayImage = NDArray[np.uint8]

# --- Safety & Vision Settings ---
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1

ASSETS_DIR: Path = BASE_DIR / "assets"
DEBUG_DIR: Path = BASE_DIR / "debug_vision"

CONFIDENCE: float = float(os.getenv("VISION_CONFIDENCE", "0.8"))
TITLE_HINT: str = os.getenv("WINMENTOR_TITLE", "WINMENTOR").upper()
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


def focus_winmentor() -> bool:
    """Finds WinMentor, maximizes it, forces it to the foreground and VERIFIES it.
    Returns False if keystrokes could end up in the wrong window."""
    log.info(
        "Screen: pyautogui.size()=%s, DPI awareness=%s", tuple(pyautogui.size()), DPI_AWARENESS_RESULT
    )
    chosen, candidates = find_winmentor_window()
    for c in candidates:
        log.info("Window candidate: %s", json.dumps(c, ensure_ascii=True))
    if chosen is None:
        log.error(
            "No usable WinMentor window found (title hint=%r, exe hint=%r).", TITLE_HINT, EXE_HINT
        )
        return False

    hwnd = int(chosen["hwnd"])
    elevated = process_elevated(int(chosen["pid"]))
    if not is_admin() and elevated is not False:
        log.warning(
            "WinMentor may be running elevated (token check=%s) while the agent is not. "
            "Windows will block clicks/keys/ShowWindow. Run the agent as administrator.",
            elevated,
        )

    if user32.IsIconic(hwnd):
        log.info("Restoring minimized window")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)
    if not user32.IsZoomed(hwnd):
        log.info("Maximizing window")
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        time.sleep(0.5)

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
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            break
        log.warning("Foreground attempt %d failed", attempt)

    final = window_info(hwnd)
    in_front = int(user32.GetForegroundWindow() or 0) == hwnd
    log.info(
        "After focus: maximized=%s foreground=%s rect=%s", final["maximized"], in_front, final["rect"]
    )
    if not in_front:
        log.error(
            "Could not bring WinMentor to the foreground; aborting to avoid typing into another window."
        )
        return False
    if not final["maximized"]:
        log.warning("Window is in front but NOT maximized; vision matching may fail.")
    return True


# ---------------------------------------------------------------------------
# Vision: our own OpenCV matcher so we always know the score, even on a miss.
# ---------------------------------------------------------------------------
class Match(NamedTuple):
    score: float
    x: int  # centre
    y: int
    w: int
    h: int


_template_cache: Dict[str, tuple[float, GrayImage]] = {}
_last_miss: Dict[str, Optional[Match]] = {}


def load_template(name: str) -> GrayImage:
    path = ASSETS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Asset missing: {path}")
    mtime = path.stat().st_mtime
    cached = _template_cache.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    # imdecode(fromfile) instead of imread: imread fails on non-ASCII Windows paths
    raw = np.fromfile(str(path), dtype=np.uint8)
    decoded = cast("GrayImage | None", cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE))
    if decoded is None:
        raise ValueError(f"Could not decode image: {path}")
    _template_cache[name] = (mtime, decoded)
    return decoded


def grab_gray() -> GrayImage:
    rgb = np.array(pyautogui.screenshot())
    return cast(GrayImage, cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))


def best_match(name: str, screen: GrayImage, scale: float = 1.0) -> Optional[Match]:
    tmpl = load_template(name)
    if scale != 1.0:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        tmpl = cast(GrayImage, cv2.resize(tmpl, None, fx=scale, fy=scale, interpolation=interp))
    th = int(tmpl.shape[0])
    tw = int(tmpl.shape[1])
    if th > int(screen.shape[0]) or tw > int(screen.shape[1]):
        return None
    res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
    return Match(float(max_val), int(max_loc[0]) + tw // 2, int(max_loc[1]) + th // 2, tw, th)


def locate_image(img_name: str, timeout: float = 0.0) -> tuple[int, int] | None:
    """Polls the screen for an asset. Missing assets / OpenCV errors RAISE."""
    start = time.time()
    best: Optional[Match] = None
    while True:
        m = best_match(img_name, grab_gray())
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


def wait_for_vanish(img_name: str, check_interval: float = 0.5, max_wait: float = 1800.0) -> None:
    log.info("Waiting for operator (until %s disappears)", img_name)
    start = time.time()
    while locate_image(img_name, timeout=0.0) is not None:
        if time.time() - start > max_wait:
            raise TimeoutError(f"Operator did not close {img_name} within {max_wait:.0f}s.")
        time.sleep(check_interval)
    log.info("Window closed. Resuming automation.")
    time.sleep(0.5)


def type_text(value: object) -> None:
    """pyautogui.write silently DROPS non-ASCII characters (ă î ș ț ...).
    Fold diacritics to ASCII so nothing disappears unnoticed."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    pyautogui.write(text, interval=0.02)


# --- WinMentor Macro Sequences ---
def add_grid_items(items: list[dict[str, Any]]) -> None:
    for idx, item in enumerate(items, 1):
        log.info("Item %d/%d: %s", idx, len(items), item.get("part_code"))
        click_image("btn_plus_grid.png")
        if not locate_image("title_articole.png", timeout=5.0):
            take_debug_screenshot("title_articole.png")
            raise TimeoutError("Articole window did not open.")

        click_image("icon_lupa_search.png", offset_x=-40)
        type_text(item.get("part_code", ""))
        pyautogui.press("down")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(0.5)

        if locate_image("title_articole.png", timeout=0.0):
            log.info("Part %s not found. Creating new article...", item.get("part_code"))
            click_image("btn_adaug_general.png")
            if not locate_image("anchor_denumire_nou.png", timeout=5.0):
                take_debug_screenshot("anchor_denumire_nou.png")
                raise TimeoutError("Articole de stoc definition window did not open.")

            click_image("anchor_denumire_nou.png", offset_x=100)
            type_text(str(item.get("part_name", "")).upper())

            click_image("anchor_cod_extern.png", offset_x=100)
            type_text(str(item.get("part_code", "")).upper())

            wait_for_vanish("anchor_denumire_nou.png")
            pyautogui.press("enter")
            time.sleep(0.5)

        type_text(item.get("quantity", 1))
        pyautogui.press("enter")

        unit_price = item.get("unit_price", 0.0)
        if unit_price and float(unit_price) > 0:
            type_text(unit_price)
            pyautogui.press("enter")

        time.sleep(0.2)


def execute_factura_intrare(factura: dict[str, Any]) -> None:
    log.info("Starting Factura Intrare: %s", factura.get("furnizor"))
    click_image("Intrari.png")
    click_image("Facturi.png")
    click_image("btn_adaug_general.png")

    click_image("anchor_furnizor.png", offset_y=30)
    type_text(factura.get("furnizor", ""))
    pyautogui.press("enter")
    time.sleep(0.3)

    click_image("anchor_numar.png", offset_x=60)
    type_text(factura.get("numar_doc_winmentor", ""))

    click_image("anchor_data.png", offset_x=60)
    type_text(factura.get("data_winmentor", ""))

    add_grid_items(cast(list[dict[str, Any]], factura.get("items", [])))
    log.info("Factura typed. Awaiting operator review and Save.")


def execute_bon_consum(bon: dict[str, Any]) -> None:
    log.info("Starting Bon Consum for: %s", bon.get("cine_consuma", "General"))
    click_image("Interne.png")
    click_image("Bonuri_consum.png")
    click_image("btn_adaug_general.png")
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
def diag_screenshot() -> Response:
    buf = io.BytesIO()
    pyautogui.screenshot().save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@diag.get("/match")
def diag_match(img: Optional[str] = None, sweep: bool = False) -> Dict[str, Any]:
    """Score of every asset (or one with ?img=Intrari.png) against the current screen.
    sweep=true also tries scales 0.5x-2.0x: if a scaled score is high while the
    1.0x score is low, the templates were captured at a different display scale."""
    screen = grab_gray()
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
def diag_bundle() -> Response:
    """One zip to send back: diagnostics, current screen, logs, recent failure snapshots."""
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