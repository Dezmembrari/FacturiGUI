import base64
import math
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import cv2
import fitz  # pyright: ignore[reportMissingTypeStubs]
import numpy as np

NDArrayUint8 = np.ndarray[Any, np.dtype[np.uint8]]
NDArrayFloat32 = np.ndarray[Any, np.dtype[np.float32]]


class _FitzPixmapProto(Protocol):
    samples: bytes
    height: int
    width: int


class _FitzPageProto(Protocol):
    def get_pixmap(self, *, dpi: int = 250, alpha: bool = False) -> _FitzPixmapProto: ...


class _FitzDocProto(Protocol):
    def __len__(self) -> int: ...
    def load_page(self, page_id: int) -> _FitzPageProto: ...
    def close(self) -> None: ...


_fitz_open = cast(Callable[[str], _FitzDocProto], getattr(fitz, "open"))


@dataclass(frozen=True)
class TalonCropConfig:
    """
    Normalized [0.0 .. 1.0] bounding box on the flattened Talon document.
    Defaults to the Top-Left A-to-K region of an unfolded Romanian Talon.
    """
    x1: float = 0.02
    y1: float = 0.02
    x2: float = 0.56
    y2: float = 0.62
    rotate_deg: int = 0           # 0, 90, 180, 270
    redact_owner_c: bool = False  # Blacks out the C.2/C.3 owner name/address band between B and D.1


@dataclass(frozen=True)
class LocalScanPreview:
    warped_data_url: str
    scanned_crop_data_url: str
    suggested_crop: TalonCropConfig
    detected_quad: bool


# --- Image Loading (Supports JPG/PNG/WEBP and PDF locally) -------------------------------

def _load_file_as_bgr(file_path: str) -> NDArrayUint8:
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        doc = _fitz_open(file_path)
        try:
            if len(doc) == 0:
                raise ValueError("Uploaded PDF has no pages.")
            page = doc.load_page(0)
            pix = page.get_pixmap(dpi=250, alpha=False)
            img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                (pix.height, pix.width, 3)
            )
            return cast(NDArrayUint8, cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))
        finally:
            doc.close()

    raw_bytes = np.fromfile(file_path, dtype=np.uint8)
    decoded = cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError(f"Could not decode image file: {file_path}")
    return cast(NDArrayUint8, decoded)


def _rotate_image(img: NDArrayUint8, rotate_deg: int) -> NDArrayUint8:
    deg = rotate_deg % 360
    if deg == 90:
        return cast(NDArrayUint8, cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE))
    if deg == 180:
        return cast(NDArrayUint8, cv2.rotate(img, cv2.ROTATE_180))
    if deg == 270:
        return cast(NDArrayUint8, cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE))
    return img


# --- 4-Point Perspective Document Detection (Distant Photo -> Flat Document) -------------

def _order_points(pts: NDArrayFloat32) -> NDArrayFloat32:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[int(np.argmin(s))]      # Top-left
    rect[2] = pts[int(np.argmax(s))]      # Bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[int(np.argmin(diff))]   # Top-right
    rect[3] = pts[int(np.argmax(diff))]   # Bottom-left
    return rect


# def _point_xy(pt: Any) -> tuple[float, float]:
#     pair = cast( Sequence[float], pt.tolist() ) if hasattr(pt, "tolist") else (0.0, 0.0)
#     return float(pair[0]), float(pair[1])


def _dist(p1: Any, p2: Any) -> float:
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    return math.hypot(x1 - x2, y1 - y2)


def _four_point_warp(image: NDArrayUint8, pts: NDArrayFloat32) -> NDArrayUint8:
    rect = _order_points(pts)
    tl, tr, br, bl = rect[0], rect[1], rect[2], rect[3]

    width_a = _dist(br, bl)
    width_b = _dist(tr, tl)
    max_width = max(int(width_a), int(width_b), 1)

    height_a = _dist(tr, br)
    height_b = _dist(tl, bl)
    max_height = max(int(height_a), int(height_b), 1)

    dst = np.array(
        [
            [0, 0],
            [max_width - 1, 0],
            [max_width - 1, max_height - 1],
            [0, max_height - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, matrix, (max_width, max_height))
    return cast(NDArrayUint8, warped)


def detect_and_warp_document(image: NDArrayUint8) -> tuple[NDArrayUint8, bool]:
    """
    Detects the Talon paper inside a distant camera photo and perspective-warps it flat.
    If the photo is already a tight close-up with no outer background border, returns the image as-is.
    """
    h, w = image.shape[:2]
    scale = 800.0 / float(max(h, w))
    small = (
        cast(NDArrayUint8, cv2.resize(image, (int(w * scale), int(h * scale))))
        if scale < 1.0
        else image.copy()
    )
    ratio = 1.0 / scale if scale < 1.0 else 1.0

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    edged = cv2.Canny(blurred, 45, 150)
    kernel = np.ones((5, 5), np.uint8)
    closed = cv2.morphologyEx(edged, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    small_area = float(small.shape[0] * small.shape[1])

    best_quad: NDArrayFloat32 | None = None
    best_area = 0.0

    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area < 0.12 * small_area or area > 0.94 * small_area:
            continue
        peri = float(cv2.arcLength(cnt, True))
        approx = cv2.approxPolyDP(cnt, 0.025 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx) and area > best_area:
            best_area = area
            best_quad = cast(NDArrayFloat32, approx.reshape(4, 2).astype(np.float32) * ratio)

    if best_quad is not None:
        return _four_point_warp(image, best_quad), True

    return image, False


def default_a_to_k_crop_for_shape(h: int, w: int) -> TalonCropConfig:
    """
    Chooses default Top-Left A-to-K crop bounds based on aspect ratio:
    - Landscape unfolded Talon spread (w/h > 1.15): A-to-K is in the left column, upper 62%.
    - Single folded column / portrait capture: A-to-K is across the width, upper 62%.
    """
    aspect = float(w) / float(max(h, 1))
    if aspect > 1.15:
        return TalonCropConfig(x1=0.02, y1=0.02, x2=0.56, y2=0.62)
    return TalonCropConfig(x1=0.02, y1=0.02, x2=0.94, y2=0.62)


# --- Top-Left A-to-K Crop + Scanned Document Filter --------------------------------------

def crop_and_scanify(warped_bgr: NDArrayUint8, cfg: TalonCropConfig) -> NDArrayUint8:
    """
    1. Rotates if requested.
    2. Crops strictly to the normalized Top-Left (x1, y1, x2, y2) A-to-K region.
    3. Optionally redacts the C.2/C.3 owner name/address band (between B and D.1).
    4. Applies shadow removal + CLAHE + unsharp masking to produce a clean monochrome scan.
    """
    rotated = _rotate_image(warped_bgr, cfg.rotate_deg)
    h, w = rotated.shape[:2]

    x1_px = max(0, min(w - 2, int(round(min(cfg.x1, cfg.x2) * w))))
    x2_px = max(x1_px + 1, min(w, int(round(max(cfg.x1, cfg.x2) * w))))
    y1_px = max(0, min(h - 2, int(round(min(cfg.y1, cfg.y2) * h))))
    y2_px = max(y1_px + 1, min(h, int(round(max(cfg.y1, cfg.y2) * h))))

    cropped = rotated[y1_px:y2_px, x1_px:x2_px].copy()
    gray = cast(NDArrayUint8, cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY))

    ch, cw = gray.shape[:2]
    if max(ch, cw) < 1300:
        scale = 1300.0 / float(max(ch, cw))
        gray = cast(
            NDArrayUint8,
            cv2.resize(gray, (int(cw * scale), int(ch * scale)), interpolation=cv2.INTER_CUBIC),
        )

    dilated = cv2.dilate(gray, np.ones((17, 17), np.uint8))
    bg_blur = cv2.medianBlur(dilated, 35)
    normalized = cast(NDArrayUint8, cv2.divide(gray, bg_blur, scale=255))

    clahe = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8))
    contrasted = cast(NDArrayUint8, clahe.apply(normalized))
    blurred = cv2.GaussianBlur(contrasted, (0, 0), 1.1)
    scanned = cast(NDArrayUint8, cv2.addWeighted(contrasted, 1.5, blurred, -0.5, 0))

    if cfg.redact_owner_c:
        sh, sw = scanned.shape[:2]
        ry1, ry2 = int(sh * 0.18), int(sh * 0.43)
        cv2.rectangle(scanned, (0, ry1), (sw, ry2), 255, thickness=-1)
        cv2.putText(
            scanned,
            "[REDACTED OWNER C.2/C.3]",
            (int(sw * 0.05), int((ry1 + ry2) / 2)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            80,
            2,
        )

    return scanned


def _to_png_data_url(img: NDArrayUint8, max_dim: int = 1100) -> str:
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        s = float(max_dim) / float(max(h, w))
        img = cast(NDArrayUint8, cv2.resize(img, (int(w * s), int(h * s))))
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode image to PNG.")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def build_local_scan_preview(
    file_path: str, custom_crop: TalonCropConfig | None = None
) -> LocalScanPreview:
    bgr = _load_file_as_bgr(file_path)
    warped, found_quad = detect_and_warp_document(bgr)

    rot_warped = _rotate_image(warped, custom_crop.rotate_deg if custom_crop else 0)
    wh, ww = rot_warped.shape[:2]
    cfg = custom_crop if custom_crop is not None else default_a_to_k_crop_for_shape(wh, ww)

    scanned_crop = crop_and_scanify(warped, cfg)
    return LocalScanPreview(
        warped_data_url=_to_png_data_url(rot_warped, max_dim=1000),
        scanned_crop_data_url=_to_png_data_url(scanned_crop, max_dim=1000),
        suggested_crop=cfg,
        detected_quad=found_quad,
    )


def prepare_talon_for_ocr(
    file_path: str, custom_crop: TalonCropConfig | None = None
) -> str:
    bgr = _load_file_as_bgr(file_path)
    warped, _ = detect_and_warp_document(bgr)
    wh, ww = warped.shape[:2]
    cfg = custom_crop if custom_crop is not None else default_a_to_k_crop_for_shape(wh, ww)
    scanned_crop = crop_and_scanify(warped, cfg)

    fd, out_png_path = tempfile.mkstemp(suffix=".png", prefix="talon_ak_scan_")
    os.close(fd)
    ok, buf = cv2.imencode(".png", scanned_crop)
    if not ok:
        raise RuntimeError("Failed to encode cropped Talon scan to PNG.")
    Path(out_png_path).write_bytes(buf.tobytes())
    return out_png_path