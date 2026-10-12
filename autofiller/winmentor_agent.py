import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, cast

import pyautogui  # type: ignore
import pygetwindow as gw  # type: ignore
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --- Safety & Vision Settings ---
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1  # Global delay between keystrokes/clicks

# Determine if running as a compiled EXE or a raw Python script
BASE_DIR: Path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)

ASSETS_DIR: Path = BASE_DIR / "assets"
DEBUG_DIR: Path = BASE_DIR / "debug_vision"
CONFIDENCE: float = 0.8  # Lowered slightly to 0.8 to handle minor font smoothing differences


class WindowProtocol(Protocol):
    """Structural typing contract for pygetwindow OS handles."""

    title: str
    isMinimized: bool
    isMaximized: bool

    def restore(self) -> None: ...
    def activate(self) -> None: ...
    def maximize(self) -> None: ...


app = FastAPI(title="WinMentor Auto-Filler Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AutoFillPayload(BaseModel):
    factura_intrare: Optional[Dict[str, Any]] = Field(default=None)
    # Pylance throws a false positive on default_factory=list here, so we suppress it.
    bonuri_consum: List[Dict[str, Any]] = Field(default_factory=list)  # type: ignore


# --- Core Vision Helpers ---

def take_debug_screenshot(failed_img_name: str) -> None:
    """Takes a full screenshot to show exactly what the bot sees when it fails."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        safe_name = failed_img_name.replace(".png", "").replace(".jpg", "")
        filepath = DEBUG_DIR / f"FAILED_{safe_name}_{timestamp}.png"
        pyautogui.screenshot(str(filepath))
        print(f"\n[📸 VISUAL DEBUGGER] I couldn't find '{failed_img_name}'.")
        print(f"I took a screenshot of what I currently see. Open this file to debug:")
        print(f"--> {filepath}\n")
    except Exception as e:
        print(f"[!] Could not save debug screenshot: {e}")


def focus_winmentor() -> bool:
    """Finds WinMentor, maximizes it, and forces it to the foreground."""
    print(f"[DEBUG] Current Screen Resolution recognized by bot: {pyautogui.size()}")
    
    # Force cast the untyped OS return to List[Any] to satisfy Pylance
    raw_windows = cast(List[Any], gw.getAllWindows())
    windows: List[WindowProtocol] = []
    
    for w in raw_windows:
        title = str(getattr(w, "title", ""))
        if "WINMENTOR" in title.upper():
            windows.append(cast(WindowProtocol, w))
            print(f"[DEBUG] Found WinMentor Window: '{title}'")
            
    if not windows:
        print("[!] WinMentor window not found in the active process list.")
        return False

    win = windows[0]
    
    try:
        if win.isMinimized:
            print("[DEBUG] WinMentor is minimized. Attempting to restore...")
            win.restore()
            time.sleep(0.5)
            
        # Force maximization so UI elements are never hidden off-screen
        if not getattr(win, "isMaximized", True):
            print("[DEBUG] WinMentor is not maximized. Maximizing now...")
            win.maximize()
            time.sleep(0.5)
            
        print("[DEBUG] Activating window...")
        # Bypassing Windows 10 Foreground Lock Timeout by sending a dummy Alt keystroke
        pyautogui.press('alt')
        win.activate()
        time.sleep(0.8)  # Wait slightly longer for the Windows maximize animation to finish
    except Exception as e:
        print(f"[!] Minor error during window activation: {e}")
        
    return True


def locate_image(img_name: str, timeout: float = 0.0) -> tuple[int, int] | None:
    """Scans the screen for an asset, returning (x, y) coordinates."""
    img_path = str(ASSETS_DIR / img_name)
    if not os.path.exists(img_path):
        print(f"[!] Asset missing from folder: {img_path}")
        return None

    start_time = time.time()
    while True:
        try:
            pos_box: Any = pyautogui.locateCenterOnScreen(img_path, confidence=CONFIDENCE)
            if pos_box is not None:
                return (int(pos_box[0]), int(pos_box[1]))
        except (pyautogui.ImageNotFoundException, Exception):
            pass

        if (time.time() - start_time) >= timeout:
            return None
        time.sleep(0.3)


def click_image(
    img_name: str, offset_x: int = 0, offset_y: int = 0, timeout: float = 10.0
) -> None:
    """Waits for an image, moves the mouse with an offset, and clicks."""
    pos = locate_image(img_name, timeout=timeout)
    if pos is None:
        take_debug_screenshot(img_name)
        raise TimeoutError(f"Could not find {img_name} on screen after {timeout} seconds.")

    pyautogui.moveTo(pos[0] + offset_x, pos[1] + offset_y, duration=0.2)
    pyautogui.click()
    time.sleep(0.2)


def wait_for_vanish(img_name: str, check_interval: float = 0.5) -> None:
    """Halts execution until the specified image disappears from the screen."""
    print(f"[*] Waiting for operator... (waiting for {img_name} to close)")
    while locate_image(img_name, timeout=0.0) is not None:
        time.sleep(check_interval)
    print("[*] Window closed. Resuming automation.")
    time.sleep(0.5)


# --- WinMentor Macro Sequences ---

def add_grid_items(items: list[dict[str, Any]]) -> None:
    """Handles the repetitive task of adding parts to either an Invoice or a Bon."""
    for item in items:
        # 1. Open the Catalog
        click_image("btn_plus_grid.png")
        if not locate_image("title_articole.png", timeout=5.0):
            take_debug_screenshot("title_articole.png")
            raise TimeoutError("Articole window did not open.")

        # 2. Search for the part
        # Offset X by -40 to click left of the magnifying glass (inside the input)
        click_image("icon_lupa_search.png", offset_x=-40)
        pyautogui.write(str(item.get("part_code", "")), interval=0.02)

        # Move down to select the first filtered result and hit enter
        pyautogui.press("down")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(0.5)

        # 3. Smart Detection: Did the window close?
        if locate_image("title_articole.png", timeout=0.0):
            print(f"[*] Part {item.get('part_code')} not found. Creating new article...")

            # Click add new article (Uses your consolidated + Adaug button)
            click_image("btn_adaug_general.png")
            if not locate_image("anchor_denumire_nou.png", timeout=5.0):
                take_debug_screenshot("anchor_denumire_nou.png")
                raise TimeoutError("Articole de stoc definition window did not open.")

            # Fill Denumire (Offset X+100 right of the anchor)
            click_image("anchor_denumire_nou.png", offset_x=100)
            pyautogui.write(str(item.get("part_name", "")).upper(), interval=0.02)

            # Fill Cod Extern (Offset X+100 right of the anchor)
            click_image("anchor_cod_extern.png", offset_x=100)
            pyautogui.write(str(item.get("part_code", "")).upper(), interval=0.02)

            # PAUSE: Wait for the operator to verify, add UM, and click Salvez manually
            wait_for_vanish("anchor_denumire_nou.png")

            # Back on the Articole screen, the new item is now highlighted. Hit enter.
            pyautogui.press("enter")
            time.sleep(0.5)

        # 4. Fill Grid Quantities and Prices
        pyautogui.write(str(item.get("quantity", 1)), interval=0.02)
        pyautogui.press("enter")

        # If it's a Factura, we also need to type the Price (Bon consum auto-calculates price)
        unit_price = item.get("unit_price", 0.0)
        if unit_price and float(unit_price) > 0:
            pyautogui.write(str(unit_price), interval=0.02)
            pyautogui.press("enter")

        time.sleep(0.2)


def execute_factura_intrare(factura: dict[str, Any]) -> None:
    print(f"[*] Starting Factura Intrare: {factura.get('furnizor')}")

    click_image("Intrari.png")
    click_image("Facturi.png")

    # Click Add on the Facturi list to create a new one
    click_image("btn_adaug_general.png")

    # Fill Header
    click_image("anchor_furnizor.png", offset_y=30)
    pyautogui.write(str(factura.get("furnizor", "")), interval=0.02)
    pyautogui.press("enter")
    time.sleep(0.3)

    click_image("anchor_numar.png", offset_x=60)
    pyautogui.write(str(factura.get("numar_doc_winmentor", "")), interval=0.02)

    click_image("anchor_data.png", offset_x=60)
    pyautogui.write(str(factura.get("data_winmentor", "")), interval=0.02)

    # Run the item insertion loop
    raw_items = cast(list[dict[str, Any]], factura.get("items", []))
    add_grid_items(raw_items)

    print("[*] Factura typed. Awaiting operator review and Save.")


def execute_bon_consum(bon: dict[str, Any]) -> None:
    print(f"[*] Starting Bon Consum for: {bon.get('cine_consuma', 'General')}")

    click_image("Interne.png")
    click_image("Bonuri_consum.png")

    click_image("btn_adaug_general.png")
    time.sleep(0.5)

    raw_items = cast(list[dict[str, Any]], bon.get("items", []))
    add_grid_items(raw_items)

    print("[*] Bon Consum typed. Awaiting operator review and Save.")


def run_automation_task(payload: AutoFillPayload) -> None:
    """The master background task that controls the sequence."""
    try:
        if not focus_winmentor():
            print("[!] WinMentor window not found. Aborting.")
            return

        if payload.factura_intrare:
            execute_factura_intrare(payload.factura_intrare)

        if payload.bonuri_consum:
            for bon in payload.bonuri_consum:
                execute_bon_consum(bon)

        print("\n[+] Automation Sequence Complete!")

    except pyautogui.FailSafeException:
        print("\n[!!!] EMERGENCY ABORT TRIGGERED BY USER (Mouse in screen corner) [!!!]")
    except Exception as e:
        print(f"\n[!] Automation Error: {e}")


# --- API Endpoint ---

@app.post("/autofill")
def trigger_autofill(
    payload: AutoFillPayload, background_tasks: BackgroundTasks
) -> dict[str, str]:
    print("\n--- Received Auto-Fill Webhook from FacturiGUI ---")
    background_tasks.add_task(run_automation_task, payload)
    return {
        "status": "started",
        "message": "Automation dispatched to background thread.",
    }


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print(" 🤖 WinMentor Auto-Filler Agent is ONLINE")
    print(f" 📂 Assets Directory: {ASSETS_DIR}")
    print(f" 📂 Vision Debug Folder: {DEBUG_DIR}")
    print(" 🛑 EMERGENCY STOP: Slam your mouse into any corner of your screen.")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8001)