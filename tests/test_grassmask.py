"""Tests for grass chroma-key masking and ground-layer compositing."""

import builtins
import math
import time

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from PIL import Image


def _make_pitch_scene(width=1280, height=720, *, night=False):
    """Create a synthetic striped pitch with painted lines and player blobs."""
    rng = np.random.default_rng(42)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    light = np.array([26, 76, 30] if night else [58, 138, 52], dtype=np.int16)
    dark = np.array([18, 56, 22] if night else [42, 108, 38], dtype=np.int16)

    stripe_w = max(24, width // 12)
    for x0 in range(0, width, stripe_w):
        x1 = min(width, x0 + stripe_w)
        base = light if (x0 // stripe_w) % 2 == 0 else dark
        frame[:, x0:x1] = np.clip(base, 0, 255).astype(np.uint8)

    noise = rng.integers(-4, 5, size=(height, width, 3), dtype=np.int16)
    frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    line_mask = np.zeros((height, width), dtype=bool)
    line_color = (165, 175, 165) if night else (245, 245, 245)
    line_u8 = np.zeros((height, width), dtype=np.uint8)
    thickness = max(2, round(width / 320))
    # Touchlines, halfway line, penalty box, and centre circle.
    margin_x = round(width * 0.05)
    margin_y = round(height * 0.06)
    cv2.rectangle(
        line_u8,
        (margin_x, margin_y),
        (width - margin_x, height - margin_y),
        255,
        thickness,
    )
    cv2.line(
        line_u8, (width // 2, margin_y), (width // 2, height - margin_y), 255, thickness
    )
    cv2.circle(line_u8, (width // 2, height // 2), round(height * 0.16), 255, thickness)
    cv2.rectangle(
        line_u8,
        (margin_x, round(height * 0.25)),
        (round(width * 0.2), round(height * 0.75)),
        255,
        thickness,
    )
    line_mask = line_u8 > 0
    frame[line_mask] = line_color

    # Player blobs: (x, y, w, h, jersey_rgb, shorts_rgb, skin_rgb)
    blobs = {}
    blob_mask = np.zeros((height, width), dtype=bool)

    def add_player(
        name, cx, cy, pw, ph, jersey_rgb, shorts_rgb, skin_rgb, green_speck=False
    ):
        pmask = np.zeros((height, width), dtype=bool)
        head_r = max(4, round(pw * 0.35))
        head_cy = cy - round(ph * 0.4)
        yy, xx = np.ogrid[:height, :width]
        head = (xx - cx) ** 2 + (yy - head_cy) ** 2 <= head_r**2
        torso = (
            (xx >= cx - pw // 2)
            & (xx < cx + pw // 2)
            & (yy >= head_cy + head_r - 1)
            & (yy < cy + round(ph * 0.15))
        )
        shorts = (
            (xx >= cx - pw // 2)
            & (xx < cx + pw // 2)
            & (yy >= cy + round(ph * 0.15))
            & (yy < cy + round(ph * 0.45))
        )
        frame[torso] = jersey_rgb
        frame[shorts] = shorts_rgb
        frame[head] = skin_rgb
        pmask = head | torso | shorts
        if green_speck:
            # Small 3x3 grass-coloured speck inside the torso.
            sy, sx = cy - 2, cx
            frame[sy - 1 : sy + 2, sx - 1 : sx + 2] = light.astype(np.uint8)
        blobs[name] = pmask
        blob_mask[:] |= pmask

    # Red kit player
    add_player(
        "red_kit",
        round(width * 0.28),
        round(height * 0.45),
        round(width * 0.022),
        round(height * 0.09),
        (210, 32, 32),
        (25, 25, 30),
        (225, 178, 140),
        green_speck=True,
    )
    # White kit player
    add_player(
        "white_kit",
        round(width * 0.62),
        round(height * 0.52),
        round(width * 0.024),
        round(height * 0.095),
        (244, 244, 246),
        (240, 240, 244),
        (205, 150, 110),
    )
    # Skin-heavy / yellow-orange kit player
    add_player(
        "blue_skin",
        round(width * 0.42),
        round(height * 0.68),
        round(width * 0.022),
        round(height * 0.09),
        (35, 80, 205),
        (215, 165, 128),
        (170, 118, 82),
    )

    grass_mask = (~blob_mask) & (~line_mask)
    return frame, grass_mask, line_mask, blob_mask, blobs


def test_ground_mask_includes_pitch_and_lines_and_excludes_players():
    from once_sport_mcp.grassmask import ground_mask

    frame, grass_gt, line_gt, blob_gt, blobs = _make_pitch_scene(1280, 720)
    mask, reliable = ground_mask(frame)

    assert reliable is True
    assert isinstance(mask, np.ndarray)
    assert mask.dtype == np.bool_
    assert mask.shape == (720, 1280)

    grass_recall = float(np.mean(mask[grass_gt]))
    blob_excluded = float(np.mean(~mask[blob_gt]))
    line_recall = float(np.mean(mask[line_gt & (~blob_gt)]))

    assert grass_recall >= 0.95
    assert blob_excluded >= 0.97
    assert line_recall >= 0.85
    for name, bmask in blobs.items():
        assert float(np.mean(~mask[bmask])) >= 0.97, (
            f"blob {name} leaked into ground mask"
        )


def test_ground_mask_handles_sunlit_and_shadow_halves():
    from once_sport_mcp.grassmask import ground_mask

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:, :320] = (95, 165, 70)
    frame[:, 320:] = (32, 88, 30)
    mask, reliable = ground_mask(frame)
    assert reliable is True
    assert float(np.mean(mask)) >= 0.99


def test_ground_mask_accepts_pil_image_and_handles_night_pitch():
    from once_sport_mcp.grassmask import ground_mask

    frame, grass_gt, line_gt, blob_gt, _ = _make_pitch_scene(1280, 720, night=True)
    pil_frame = Image.fromarray(frame, mode="RGB")
    mask, reliable = ground_mask(pil_frame)

    assert reliable is True
    assert mask.shape == (720, 1280)
    assert float(np.mean(mask[grass_gt])) >= 0.95
    assert float(np.mean(~mask[blob_gt])) >= 0.97
    assert float(np.mean(mask[line_gt & (~blob_gt)])) >= 0.80


def test_ground_mask_degenerate_frame_returns_unreliable_all_true():
    from once_sport_mcp.grassmask import ground_mask

    # Mostly-blue frame (e.g. graphic/indoor/close-up) -> reliable=False and all-True mask.
    blue_frame = np.full((360, 640, 3), (30, 80, 210), dtype=np.uint8)
    mask, reliable = ground_mask(blue_frame)
    assert reliable is False
    assert mask.shape == (360, 640)
    assert bool(np.all(mask)) is True

    # Frame with 5% green (< 8% threshold) also triggers fallback.
    sparse_green = np.full((360, 640, 3), (180, 40, 40), dtype=np.uint8)
    sparse_green[:18, :, :] = (50, 130, 45)
    mask2, reliable2 = ground_mask(sparse_green)
    assert reliable2 is False
    assert bool(np.all(mask2)) is True


def test_ground_mask_is_deterministic_and_fast_on_720p_cpu():
    from once_sport_mcp.grassmask import ground_mask

    frame, _, _, _, _ = _make_pitch_scene(1280, 720)
    # Warmup once for lazy imports, then time.
    first_mask, first_rel = ground_mask(frame)
    t0 = time.perf_counter()
    second_mask, second_rel = ground_mask(frame)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert first_rel is True and second_rel is True
    assert np.array_equal(first_mask, second_mask)
    assert elapsed_ms < 150.0


def test_apply_ground_layer_leaves_blob_untouched_and_tints_grass():
    from once_sport_mcp.grassmask import apply_ground_layer, ground_mask

    frame, grass_gt, _, blob_gt, _ = _make_pitch_scene(640, 360)
    mask, reliable = ground_mask(frame)
    assert reliable is True

    base_rgba = np.dstack([frame, np.full((360, 640), 255, dtype=np.uint8)])
    # Translucent yellow ground graphic across the entire frame.
    overlay_rgba = np.zeros((360, 640, 4), dtype=np.uint8)
    overlay_rgba[:, :] = (255, 220, 40, 140)

    comp_np = apply_ground_layer(base_rgba, overlay_rgba, mask, feather_px=1.5)
    assert isinstance(comp_np, np.ndarray)
    assert comp_np.shape == (360, 640, 4)
    assert comp_np.dtype == np.uint8

    # Excluded blob pixels remain identical to base.
    excluded_blobs = blob_gt & (~mask)
    assert np.any(excluded_blobs)
    assert np.array_equal(comp_np[excluded_blobs], base_rgba[excluded_blobs])

    # Masked grass pixels are tinted toward yellow (higher red channel).
    tinted_grass = grass_gt & mask
    assert (
        np.mean(comp_np[tinted_grass, 0].astype(float) - base_rgba[tinted_grass, 0])
        > 40.0
    )

    # Also works with PIL.Image RGBA inputs.
    comp_pil = apply_ground_layer(
        Image.fromarray(base_rgba, mode="RGBA"),
        Image.fromarray(overlay_rgba, mode="RGBA"),
        mask,
        feather_px=0.0,
    )
    assert isinstance(comp_pil, Image.Image)
    assert comp_pil.mode == "RGBA"
    pil_arr = np.asarray(comp_pil)
    assert np.array_equal(pil_arr[excluded_blobs], base_rgba[excluded_blobs])
    assert (
        np.mean(pil_arr[tinted_grass, 0].astype(float) - base_rgba[tinted_grass, 0])
        > 40.0
    )


def test_grassmask_validates_inputs_and_missing_dependencies(monkeypatch):
    from once_sport_mcp import grassmask

    with pytest.raises(grassmask.GrassMaskError):
        grassmask.ground_mask(np.zeros((100, 100), dtype=np.uint8))

    with pytest.raises(grassmask.GrassMaskError):
        grassmask.ground_mask(np.zeros((100, 100, 3), dtype=np.float32))

    base = np.zeros((20, 20, 4), dtype=np.uint8)
    layer = np.zeros((20, 20, 4), dtype=np.uint8)
    mask = np.ones((20, 20), dtype=bool)
    with pytest.raises(grassmask.GrassMaskError):
        grassmask.apply_ground_layer(base, layer, mask, feather_px=-1.0)
    with pytest.raises(grassmask.GrassMaskError):
        grassmask.apply_ground_layer(base, layer, mask, feather_px=math.nan)
    with pytest.raises(grassmask.GrassMaskError):
        grassmask.apply_ground_layer(base, layer, mask, feather_px=True)
    with pytest.raises(grassmask.GrassMaskError):
        grassmask.apply_ground_layer(base, layer, np.ones((10, 20), dtype=bool))

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cv2":
            raise ImportError("simulated missing cv2")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(grassmask.GrassMaskError, match="numpy and opencv"):
        grassmask.ground_mask(np.zeros((20, 20, 3), dtype=np.uint8))


def _striped_pitch(width=1920, height=1080):
    frame = np.zeros((height, width, 3), np.uint8)
    for column in range(0, width, 160):
        shade = (52, 128, 48) if (column // 160) % 2 else (46, 118, 42)
        frame[:, column : column + 160] = shade
    frame[500:504, :] = (235, 235, 235)  # a painted line
    return frame


def test_small_players_are_excluded_at_broadcast_scale():
    from once_sport_mcp.grassmask import ground_mask

    frame = _striped_pitch()
    players = {
        "white": ((600, 300), (240, 240, 240)),
        "dark_green": ((1200, 700), (20, 70, 30)),
        "red": ((900, 800), (200, 30, 30)),
    }
    for (x, y), colour in players.values():
        frame[y : y + 50, x : x + 18] = colour
    mask, reliable = ground_mask(frame)
    assert reliable
    for name, ((x, y), _) in players.items():
        core = mask[y + 10 : y + 40, x + 4 : x + 14]
        assert 1 - core.mean() >= 0.8, name
    assert mask[100:300, 100:400].mean() >= 0.99  # empty grass stays ground
    assert mask[500:504, 100:1800].mean() >= 0.95  # the line stays ground
