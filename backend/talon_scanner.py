import base64
import importlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import cv2
import numpy as np
from PIL import Image, ImageOps

NDArrayUint8 = np.ndarray[Any, np.dtype[np.uint8]]


class _FitzPixmapProto(Protocol):
    samples: bytes
    height: int
    width: int


class _FitzPageProto(Protocol):
    def get_pixmap(self, *, dpi: int = 220, alpha: bool = False) -> _FitzPixmapProto: ...


class _FitzDocProto(Protocol):
    def __len__(self) -> int: ...
    def load_page(self, page_id: int) -> _FitzPageProto: ...
    def close(self) -> None: ...


def _open_pdf_document(file_path: str) -> _FitzDocProto:
    """Loads PyMuPDF (`pymupdf` or `fitz`) dynamically without missing-stub warnings."""
    for mod_name in ("pymupdf", "fitz"):
        try:
            mod = importlib.import_module(mod_name)
            open_fn = cast(Callable[[str], _FitzDocProto], getattr(mod, "open"))
            return open_fn(file_path)
        except ImportError:
            continue
    raise RuntimeError("PyMuPDF is required to rasterize PDF documents locally.")


@dataclass(frozen=True)
class TalonCropConfig:
    """Normalized [0.0 .. 1.0] Top-Left A-to-K bounding box on the scanned Talon image."""
    x1: float = 0.02
    y1: float = 0.02
    x2: float = 0.56
    y2: float = 0.62
    rotate_deg: int = 0
    redact_owner_c: bool = False
    c_y1: float = 0.0
    c_y2: float = 0.0


@dataclass(frozen=True)
class LocalScanPreview:
    warped_data_url: str
    scanned_full_data_url: str
    scanned_crop_data_url: str
    suggested_crop: TalonCropConfig
    detected_quad: bool
    detected_c_section: bool


def _load_file_as_bgr(file_path: str, page_index: int = 0, dpi: int = 240) -> NDArrayUint8:
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        doc = _open_pdf_document(file_path)
        try:
            if len(doc) == 0:
                raise ValueError("Uploaded PDF has no pages.")
            idx = max(0, min(page_index, len(doc) - 1))
            page = doc.load_page(idx)
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                (pix.height, pix.width, 3)
            )
            return cast(NDArrayUint8, cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))
        finally:
            doc.close()

    try:
        with Image.open(file_path) as pil_img:
            transposed = ImageOps.exif_transpose(pil_img)
            rgb = transposed.convert("RGB")
            arr = np.asarray(rgb, dtype=np.uint8)
            return cast(NDArrayUint8, cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
    except Exception:
        raw_bytes = np.fromfile(file_path, dtype=np.uint8)
        decoded = cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError(f"Could not decode image file: {file_path}")
        return cast(NDArrayUint8, decoded)


def _crisp_scan_enhance(bgr_or_gray: NDArrayUint8) -> NDArrayUint8:
    """
    Always-on 300 DPI monochrome flatbed scan filter:
    removes uneven lighting/shadows via smooth Gaussian illumination division,
    stretches 2%..98% contrast percentiles, and sharpens character edges.
    """
    if len(bgr_or_gray.shape) == 3:
        gray = cast(NDArrayUint8, cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY))
    else:
        gray = bgr_or_gray.copy()

    h, w = gray.shape[:2]
    if max(h, w) < 1200:
        scale = 1200.0 / float(max(h, w))
        gray = cast(
            NDArrayUint8,
            cv2.resize(
                gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC
            ),
        )

    illum = cv2.GaussianBlur(gray, (0, 0), sigmaX=45, sigmaY=45)
    norm = cast(NDArrayUint8, cv2.divide(gray, illum, scale=245))

    p2 = float(np.percentile(norm, 2))
    p98 = float(np.percentile(norm, 98))
    if p98 > p2 + 12.0:
        stretched_f = (norm.astype(np.float32) - p2) * (250.0 / (p98 - p2))
        stretched = cast(NDArrayUint8, np.clip(stretched_f, 0, 255).astype(np.uint8))
    else:
        stretched = norm

    blur = cv2.GaussianBlur(stretched, (0, 0), 0.9)
    return cast(NDArrayUint8, cv2.addWeighted(stretched, 1.35, blur, -0.35, 0))


def _to_png_data_url(img: NDArrayUint8, max_dim: int = 1350) -> str:
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        s = float(max_dim) / float(max(h, w))
        img = cast(NDArrayUint8, cv2.resize(img, (int(w * s), int(h * s))))
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode image to PNG.")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def render_document_pages(file_path: str, max_pages: int = 6) -> list[str]:
    """Renders PDF pages or an image into PNG Data URLs for the UI Split-Viewer."""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        doc = _open_pdf_document(file_path)
        try:
            count = min(len(doc), max_pages)
            urls: list[str] = []
            for idx in range(count):
                page = doc.load_page(idx)
                pix = page.get_pixmap(dpi=180, alpha=False)
                img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    (pix.height, pix.width, 3)
                )
                bgr = cast(NDArrayUint8, cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))
                urls.append(_to_png_data_url(bgr, max_dim=1400))
            return urls
        finally:
            doc.close()

    bgr = _load_file_as_bgr(file_path)
    return [_to_png_data_url(bgr, max_dim=1400)]


def build_local_scan_preview(
    file_path: str, custom_crop: TalonCropConfig | None = None
) -> LocalScanPreview:
    """
    Instantaneous (<80ms) local scan preview:
    Applies the monochrome scan filter to the full Talon image and returns it
    for interactive cropping and rotation in the browser `<canvas>`.
    """
    raw_bgr = _load_file_as_bgr(file_path)
    scanned_full = _crisp_scan_enhance(raw_bgr)
    sh, sw = scanned_full.shape[:2]

    aspect = float(sw) / float(max(sh, 1))
    default_cfg = (
        TalonCropConfig(x1=0.02, y1=0.02, x2=0.56, y2=0.62)
        if aspect > 1.15
        else TalonCropConfig(x1=0.02, y1=0.02, x2=0.94, y2=0.62)
    )
    cfg = custom_crop if custom_crop is not None else default_cfg

    x1_px = max(0, min(sw - 2, int(round(cfg.x1 * sw))))
    x2_px = max(x1_px + 1, min(sw, int(round(cfg.x2 * sw))))
    y1_px = max(0, min(sh - 2, int(round(cfg.y1 * sh))))
    y2_px = max(y1_px + 1, min(sh, int(round(cfg.y2 * sh))))
    scanned_crop = scanned_full[y1_px:y2_px, x1_px:x2_px]

    full_url = _to_png_data_url(scanned_full, max_dim=1350)
    crop_url = _to_png_data_url(scanned_crop, max_dim=1350)

    return LocalScanPreview(
        warped_data_url=full_url,
        scanned_full_data_url=full_url,
        scanned_crop_data_url=crop_url,
        suggested_crop=cfg,
        detected_quad=False,
        detected_c_section=False,
    )


def prepare_talon_for_ocr(
    file_path: str, custom_crop: TalonCropConfig | None = None
) -> str:
    """
    Ensures the image sent to Gemini always has the crisp monochrome scan filter applied.
    """
    bgr = _load_file_as_bgr(file_path)
    scanned = _crisp_scan_enhance(bgr)
    if custom_crop is not None:
        sh, sw = scanned.shape[:2]
        x1_px = max(0, min(sw - 2, int(round(min(custom_crop.x1, custom_crop.x2) * sw))))
        x2_px = max(x1_px + 1, min(sw, int(round(max(custom_crop.x1, custom_crop.x2) * sw))))
        y1_px = max(0, min(sh - 2, int(round(min(custom_crop.y1, custom_crop.y2) * sh))))
        y2_px = max(y1_px + 1, min(sh, int(round(max(custom_crop.y1, custom_crop.y2) * sh))))
        scanned = scanned[y1_px:y2_px, x1_px:x2_px]

    fd, out_png_path = tempfile.mkstemp(suffix=".png", prefix="talon_ak_scan_")
    os.close(fd)
    ok, buf = cv2.imencode(".png", scanned)
    if not ok:
        raise RuntimeError("Failed to encode cropped Talon scan to PNG.")
    Path(out_png_path).write_bytes(buf.tobytes())
    return out_png_path