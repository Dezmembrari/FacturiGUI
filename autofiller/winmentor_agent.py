import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol, cast

import pyautogui  # type: ignore
import pygetwindow as gw  # type: ignore
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- Safety & Vision Settings ---
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1  # Global delay between keystrokes/clicks

# Determine if running as a compiled EXE or a raw Python script
BASE_DIR: Path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)

ASSETS_DIR: Path = BASE_DIR / "assets"
CONFIDENCE: float = 0.85  # OpenCV template matching threshold


class WindowProtocol(Protocol):
    """Structural typing contract for pygetwindow OS handles."""

    title: str
    isMinimized: bool

    def restore(self) -> None: ...
    def activate(self) -> None: ...


app = FastAPI(title="WinMentor Auto-Filler Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AutoFillPayload(BaseModel):
    factura_intrare: dict[str, Any] | None = None
    bonuri_consum: list[dict[str, Any]] = []


# --- Core Vision Helpers ---

def focus_winmentor() -> bool:
    """Finds WinMentor, unminimizes it, and forces it to the foreground."""
    raw_windows = cast(list[WindowProtocol], gw.getAllWindows())
    windows: list[WindowProtocol] = [
        w for w in raw_windows if "WINMENTOR" in w.title.upper()
    ]
    if not windows:
        return False

    win = windows[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    time.sleep(0.5)
    return True


def locate_image(img_name: str, timeout: float = 0.0) -> tuple[int, int] | None:
    """Scans the screen for an asset, returning (x, y) coordinates."""
    img_path = str(ASSETS_DIR / img_name)
    if not os.path.exists(img_path):
        print(f"[!] Asset missing: {img_path}")
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
            raise TimeoutError("Articole window did not open.")

        # 2. Search for the part
        # Offset X by -40 to click left of the magnifying glass (inside the input)
        click_image("icon_lupa_search.png", offset_x=-40)
        pyautogui.write(str(item["part_code"]), interval=0.02)

        # Move down to select the first filtered result and hit enter
        pyautogui.press("down")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(0.5)

        # 3. Smart Detection: Did the window close?
        if locate_image("title_articole.png", timeout=0.0):
            print(f"[*] Part {item['part_code']} not found. Creating new article...")

            # Click add new article (Uses your consolidated + Adaug button)
            click_image("btn_adaug_general.png")
            if not locate_image("anchor_denumire_nou.png", timeout=5.0):
                raise TimeoutError("Articole de stoc definition window did not open.")

            # Fill Denumire (Offset X+100 right of the anchor)
            click_image("anchor_denumire_nou.png", offset_x=100)
            pyautogui.write(str(item["part_name"]).upper(), interval=0.02)

            # Fill Cod Extern (Offset X+100 right of the anchor)
            click_image("anchor_cod_extern.png", offset_x=100)
            pyautogui.write(str(item["part_code"]).upper(), interval=0.02)

            # PAUSE: Wait for the operator to verify, add UM, and click Salvez manually
            wait_for_vanish("anchor_denumire_nou.png")

            # Back on the Articole screen, the new item is now highlighted. Hit enter.
            pyautogui.press("enter")
            time.sleep(0.5)

        # 4. Fill Grid Quantities and Prices
        pyautogui.write(str(item["quantity"]), interval=0.02)
        pyautogui.press("enter")

        # If it's a Factura, we also need to type the Price (Bon consum auto-calculates price)
        if "unit_price" in item and float(item["unit_price"]) > 0:
            pyautogui.write(str(item["unit_price"]), interval=0.02)
            pyautogui.press("enter")

        time.sleep(0.2)


def execute_factura_intrare(factura: dict[str, Any]) -> None:
    print(f"[*] Starting Factura Intrare: {factura['furnizor']}")

    click_image("Intrari.png")
    click_image("Facturi.png")

    # Click Add on the Facturi list to create a new one
    click_image("btn_adaug_general.png")

    # Fill Header
    click_image("anchor_furnizor.png", offset_y=30)
    pyautogui.write(str(factura["furnizor"]), interval=0.02)
    pyautogui.press("enter")
    time.sleep(0.3)

    click_image("anchor_numar.png", offset_x=60)
    pyautogui.write(str(factura["numar_doc_winmentor"]), interval=0.02)

    click_image("anchor_data.png", offset_x=60)
    pyautogui.write(str(factura["data_winmentor"]), interval=0.02)

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
    print(" 🛑 EMERGENCY STOP: Slam your mouse into any corner of your screen.")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8001)