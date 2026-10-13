"""Offline WinMentor template-matching test using Color Matching & ROI."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

BGRImage = NDArray[np.uint8]

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
ASSETS_DIR: Final[Path] = BASE_DIR / "assets"
SCREENSHOTS_DIR: Final[Path] = BASE_DIR / "screenshots"
OUTPUT_DIR: Final[Path] = BASE_DIR / "test_agent_results"
CONFIDENCE_THRESHOLD: Final[float] = 0.85

# Layer 3: Restrict searches to specific screen areas (y_min, y_max, x_min, x_max as ratios)
ROI_RULES: Final[dict[str, tuple[float, float, float, float]]] = {
    "Intrari.png": (0.0, 0.15, 0.0, 0.40),       # Top-left menu bar
    "Facturi.png": (0.0, 0.35, 0.0, 0.30),       # Dropdown under Intrari
    "Interne.png": (0.0, 0.15, 0.05, 0.45),      # Top menu bar
    "Bonuri_consum.png": (0.0, 0.35, 0.05, 0.35),# Dropdown under Interne
}

SCREENSHOT_NAMES: Final[tuple[str, ...]] = (
    "Screenshot 2026-10-12 145321.png",
    "Screenshot 2026-10-12 145347.png",
    "Screenshot 2026-10-12 145420.png",
    "Screenshot 2026-10-12 145448.png",
    "Screenshot 2026-10-12 145514.png",
    "Screenshot 2026-10-12 145541.png",
    "Screenshot 2026-10-12 145604.png",
    "Screenshot 2026-10-12 145653.png",
)

@dataclass(frozen=True, slots=True)
class MatchResult:
    screenshot: str
    template: str
    score: float
    detected: bool
    x: int | None
    y: int | None
    width: int | None
    height: int | None
    note: str

def load_bgr(path: Path) -> BGRImage:
    """Load image in color (BGR) to preserve red WinMentor shortcut letters."""
    raw: NDArray[np.uint8] = np.fromfile(str(path), dtype=np.uint8)
    decoded = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError(f"Could not decode image: {path}")
    return np.asarray(decoded, dtype=np.uint8).copy()

def match_template_roi(
    screen: BGRImage,
    template: BGRImage,
    roi_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[float, int, int, int, int] | None:
    
    screen_h, screen_w = screen.shape[:2]
    tmpl_h, tmpl_w = template.shape[:2]

    if roi_bounds:
        y1, y2 = int(screen_h * roi_bounds[0]), int(screen_h * roi_bounds[1])
        x1, x2 = int(screen_w * roi_bounds[2]), int(screen_w * roi_bounds[3])
        sub_screen = screen[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1
    else:
        sub_screen = screen
        offset_x, offset_y = 0, 0

    if tmpl_h > sub_screen.shape[0] or tmpl_w > sub_screen.shape[1]:
        return None

    # Color match across all 3 channels
    response = cv2.matchTemplate(sub_screen, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(response)

    return (float(max_val), int(max_loc[0]) + offset_x, int(max_loc[1]) + offset_y, int(tmpl_w), int(tmpl_h))

def get_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("arial.ttf", 15)
    except OSError:
        return ImageFont.load_default()

def annotate_screenshot(screenshot_path: Path, matches: list[MatchResult], output_path: Path) -> None:
    with Image.open(screenshot_path) as source:
        annotated = source.convert("RGB")
    draw = ImageDraw.Draw(annotated)
    font = get_font()

    for item in matches:
        if item.x is None or item.y is None or item.width is None or item.height is None:
            continue
        
        left = item.x - item.width // 2
        top = item.y - item.height // 2
        right = left + item.width
        bottom = top + item.height

        color = "#16a34a" if item.detected else "#dc2626"
        draw.rectangle((left, top, right, bottom), outline=color, width=3)
        label = f"{item.template} {item.score:.3f}"
        label_top = max(0, top - 20)
        label_box = draw.textbbox((left, label_top), label, font=font)
        draw.rectangle(label_box, fill=color)
        draw.text((left, label_top), label, fill="white", font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    annotated.save(output_path)

def main() -> int:
    asset_paths = sorted(p for p in ASSETS_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg"})
    screenshot_paths = [SCREENSHOTS_DIR / n for n in SCREENSHOT_NAMES if (SCREENSHOTS_DIR / n).is_file()]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    annotated_dir = OUTPUT_DIR / "annotated"
    annotated_dir.mkdir(parents=True, exist_ok=True)

    results: list[MatchResult] = []
    templates: dict[str, BGRImage] = {p.name: load_bgr(p) for p in asset_paths}

    for screenshot_path in screenshot_paths:
        screen = load_bgr(screenshot_path)
        screenshot_results: list[MatchResult] = []

        for template_name, template in templates.items():
            roi = ROI_RULES.get(template_name)
            found = match_template_roi(screen, template, roi_bounds=roi)

            if found is None:
                res = MatchResult(screenshot_path.name, template_name, 0.0, False, None, None, None, None, "ROI mismatch")
            else:
                score, left, top, width, height = found
                detected = score >= CONFIDENCE_THRESHOLD
                res = MatchResult(
                    screenshot_path.name, template_name, score, detected,
                    left + width // 2, top + height // 2, width, height,
                    "OK" if detected else "Below threshold"
                )
            results.append(res)
            screenshot_results.append(res)

        annotate_screenshot(screenshot_path, screenshot_results, annotated_dir / screenshot_path.name)

    # Print summary
    passed = sum(r.detected for r in results)
    print(f"\n✅ Tests Complete! Matches >= {CONFIDENCE_THRESHOLD}: {passed}/{len(results)}")
    print(f"Check the visual boxes in: {annotated_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())