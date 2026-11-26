"""Grass chroma-key masking and ground-layer compositing for telestration."""

import math

_MAX_WORK_WIDTH = 1280
_BG_GRID_WIDTH = 240
_MIN_GROUND_FRACTION = 0.08
_KIT_CONTRAST = 16.0
_MIN_MODE_FRACTION = 0.04
_GREEN_HUE_MIN = 24
_GREEN_HUE_MAX = 92
_HUE_TOLERANCE = 15
_MIN_BLOB_CORE_AREA = 4
_MAX_GRASS_SPECK_AREA = 25
_MIN_LARGE_BLOB_AREA = 36


class GrassMaskError(ValueError):
    """Raised for invalid mask inputs or missing imaging dependencies."""


def _load_deps():
    """Lazily import numpy and OpenCV, raising GrassMaskError if missing."""
    try:
        import cv2
        import numpy as np
    except (ImportError, OSError) as exc:
        raise GrassMaskError(
            "Grass masking requires numpy and opencv-contrib-python-headless "
            "(`uv sync --extra tracking`)."
        ) from exc
    return np, cv2


def _is_pil_image(obj):
    """Return True when obj is a PIL Image instance without top-level import."""
    try:
        from PIL import Image
    except ImportError:
        return False
    return isinstance(obj, Image.Image)


def _to_rgb_array(frame_rgb, np):
    """Validate and convert a PIL Image or HxWx3 uint8 array to RGB ndarray."""
    if _is_pil_image(frame_rgb):
        img = frame_rgb if frame_rgb.mode == "RGB" else frame_rgb.convert("RGB")
        arr = np.asarray(img, dtype=np.uint8)
    elif isinstance(frame_rgb, np.ndarray):
        arr = frame_rgb
    else:
        raise GrassMaskError("frame_rgb must be a PIL.Image or numpy uint8 HxWx3 array")

    if arr.ndim != 3 or arr.shape[2] != 3 or arr.dtype != np.uint8:
        raise GrassMaskError("frame_rgb must have shape (H, W, 3) and dtype uint8")
    if arr.shape[0] < 1 or arr.shape[1] < 1:
        raise GrassMaskError("frame_rgb must have non-empty dimensions")
    return arr


def _to_rgba_array(image, np, name):
    """Validate and convert a PIL Image or numpy array to HxWx4 uint8 RGBA."""
    if _is_pil_image(image):
        img = image if image.mode == "RGBA" else image.convert("RGBA")
        return np.asarray(img, dtype=np.uint8), True
    if isinstance(image, np.ndarray):
        if image.ndim != 3 or image.shape[2] != 4 or image.dtype != np.uint8:
            raise GrassMaskError(f"{name} must have shape (H, W, 4) and dtype uint8")
        if image.shape[0] < 1 or image.shape[1] < 1:
            raise GrassMaskError(f"{name} must have non-empty dimensions")
        return image, False
    raise GrassMaskError(f"{name} must be a PIL.Image or numpy uint8 HxWx4 array")


def ground_mask(frame_rgb):
    """Return ``(mask, reliable)``: True where the pixel looks like pitch.

    Grass is the dominant green hue of this frame (both stripe shades).
    Painted lines are bright, thin and next to grass; white kits are bright
    but blob-shaped, so a size-scaled opening separates them. Everything
    else (kits, skin, ball, stands, boards) is excluded. Frames with too
    little grass (close-ups, graphics) return an all-True mask with
    ``reliable=False`` so callers can skip masking safely.
    """
    np, cv2 = _load_deps()
    rgb = _to_rgb_array(frame_rgb, np)
    height, width = rgb.shape[:2]
    scale = min(1.0, _MAX_WORK_WIDTH / width)
    work = (
        cv2.resize(
            rgb,
            (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
        if scale < 1.0
        else rgb
    )
    hsv = cv2.cvtColor(work, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    unit = max(1.0, work.shape[1] / 1920.0)  # pixel size relative to 1080p

    candidates = (
        (hue >= _GREEN_HUE_MIN) & (hue <= _GREEN_HUE_MAX) & (sat >= 28) & (val >= 22)
    )
    if candidates.mean() < _MIN_MODE_FRACTION:
        return np.ones((height, width), dtype=bool), False
    histogram = np.bincount(hue[candidates].ravel(), minlength=180).astype(np.float32)
    histogram = np.convolve(
        np.pad(histogram, 3, mode="wrap"), np.ones(7) / 7, mode="valid"
    )
    peak = int(np.argmax(histogram))
    near = candidates & (np.abs(hue.astype(np.int16) - peak) <= _HUE_TOLERANCE)
    if near.mean() < _MIN_MODE_FRACTION:
        return np.ones((height, width), dtype=bool), False
    s_lo = max(24, int(np.percentile(sat[near], 5)) - 25)
    v_lo = max(20, int(np.percentile(val[near], 5) * 0.78))
    v_hi = min(255, int(np.percentile(val[near], 95)) + 65)
    grass = near & (sat >= s_lo) & (val >= v_lo) & (val <= v_hi)

    # Painted lines: bright, low saturation, thin. White kits are bright blobs.
    white = ((sat < 70) & (val > 150)).astype(np.uint8)
    blob_size = max(5, round(7 * unit) | 1)
    blobs = cv2.morphologyEx(
        white, cv2.MORPH_OPEN, np.ones((blob_size, blob_size), np.uint8)
    )
    blobs = cv2.dilate(blobs, np.ones((blob_size, blob_size), np.uint8))
    near_grass = cv2.dilate(grass.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    lines = (white > 0) & (blobs == 0) & near_grass

    # Dark or green kits sit inside the grass hue range: find them by local
    # contrast against the surrounding grass (chroma-weighted so soft shadows,
    # which change lightness only, are not flagged).
    lab = cv2.cvtColor(work, cv2.COLOR_RGB2LAB).astype(np.float32)
    background = cv2.medianBlur(
        lab.astype(np.uint8), max(5, round(41 * unit) | 1)
    ).astype(np.float32)
    delta = lab - background
    contrast = np.sqrt(
        (0.45 * delta[:, :, 0]) ** 2 + delta[:, :, 1] ** 2 + delta[:, :, 2] ** 2
    )
    kit = (contrast > _KIT_CONTRAST) & ~((white > 0) & (blobs == 0))
    kit = (
        cv2.morphologyEx(
            kit.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)
        )
        > 0
    )
    ground = (grass | lines) & ~kit
    # Small closing joins line fragments; opening removes lone speckle.
    kernel = np.ones((3, 3), np.uint8)
    ground = cv2.morphologyEx(ground.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    # Tiny non-ground specks (noise, compression) are treated as ground again.
    inverse = (1 - ground).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(inverse, connectivity=8)
    small = np.zeros(count, dtype=bool)
    small[1:] = stats[1:, cv2.CC_STAT_AREA] <= max(6, int(10 * unit * unit))
    ground = (ground > 0) | small[labels]
    # Torsos are the most contrasting part; legs and heads are not. Grow
    # player-sized excluded blobs vertically so the whole player is covered.
    excluded_u8 = (~ground).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        excluded_u8, connectivity=8
    )
    max_w, max_h = 40 * unit, 90 * unit
    sized = np.zeros(count, dtype=bool)
    sized[1:] = (
        (stats[1:, cv2.CC_STAT_WIDTH] <= max_w)
        & (stats[1:, cv2.CC_STAT_HEIGHT] <= max_h)
        & (stats[1:, cv2.CC_STAT_AREA] >= 8 * unit * unit)
    )
    players = sized[labels].astype(np.uint8)
    reach = max(3, round(11 * unit) | 1)
    grown = cv2.dilate(
        players,
        cv2.getStructuringElement(
            cv2.MORPH_RECT, (max(3, round(3 * unit) | 1), reach * 2 + 1)
        ),
    )
    excluded_u8 = np.maximum(excluded_u8, grown)
    # Safety margin around excluded players (anti-aliased kit edges).
    excluded = cv2.dilate(excluded_u8, np.ones((3, 3), np.uint8)) > 0
    ground = ~excluded

    if ground.mean() < _MIN_GROUND_FRACTION:
        return np.ones((height, width), dtype=bool), False
    if scale < 1.0:
        up = cv2.resize(
            ground.astype(np.uint8) * 255,
            (width, height),
            interpolation=cv2.INTER_LINEAR,
        )
        return up >= 128, True
    return ground, True


def apply_ground_layer(base_rgba_frame, ground_layer_rgba, mask, feather_px=1.5):
    """Alpha-composite ``ground_layer_rgba`` onto ``base_rgba_frame`` inside ``mask``.

    Feathers the mask edge inward by ``feather_px`` so masked-out pixels remain
    untouched while pitch transitions stay smooth.
    """
    np, cv2 = _load_deps()
    if isinstance(feather_px, bool) or not isinstance(feather_px, (int, float)):
        raise GrassMaskError("feather_px must be a finite non-negative number")
    feather = float(feather_px)
    if not math.isfinite(feather) or feather < 0.0:
        raise GrassMaskError("feather_px must be a finite non-negative number")

    base_arr, base_is_pil = _to_rgba_array(base_rgba_frame, np, "base_rgba_frame")
    layer_arr, _ = _to_rgba_array(ground_layer_rgba, np, "ground_layer_rgba")
    if base_arr.shape[:2] != layer_arr.shape[:2]:
        raise GrassMaskError(
            "base_rgba_frame and ground_layer_rgba must have matching dimensions"
        )

    mask_arr = np.asarray(mask)
    if mask_arr.ndim != 2 or mask_arr.shape != base_arr.shape[:2]:
        raise GrassMaskError("mask must be a 2D array matching frame dimensions")
    mask_bool = mask_arr > 0

    mask_weight = mask_bool.astype(np.float32)
    if feather > 0.0:
        blurred = cv2.GaussianBlur(
            mask_weight,
            (0, 0),
            sigmaX=feather,
            sigmaY=feather,
            borderType=cv2.BORDER_REPLICATE,
        )
        mask_weight = blurred * mask_weight

    src_a = (layer_arr[:, :, 3].astype(np.float32) / 255.0) * mask_weight
    active = src_a > 0.0
    if not np.any(active):
        out_arr = base_arr.copy()
    else:
        base_f = base_arr.astype(np.float32) / 255.0
        layer_rgb = layer_arr[:, :, :3].astype(np.float32) / 255.0
        base_a = base_f[:, :, 3]
        inv_src_a = 1.0 - src_a
        out_a = src_a + base_a * inv_src_a
        safe_out_a = np.where(out_a > 1e-6, out_a, 1.0)
        out_rgb = (
            layer_rgb * src_a[:, :, None]
            + base_f[:, :, :3] * (base_a * inv_src_a)[:, :, None]
        ) / safe_out_a[:, :, None]
        out_f = np.dstack([out_rgb, out_a])
        out_arr = base_arr.copy()
        out_arr[active] = np.clip(np.rint(out_f[active] * 255.0), 0, 255).astype(
            np.uint8
        )

    if base_is_pil:
        from PIL import Image

        return Image.fromarray(out_arr, mode="RGBA")
    return out_arr
