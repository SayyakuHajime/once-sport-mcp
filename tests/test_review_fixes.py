"""Regression tests for the independent review findings."""

import pytest
from PIL import Image, ImageFont
from test_arrows_nametags import drawing, paint
from test_formation import MEDIA as FORMATION_MEDIA
from test_formation import moving_boxes, tracking_result
from test_speed_segments import EVENT, MEDIA, speed_drawing

from once_sport_mcp import rendering, stats_export
from once_sport_mcp.formation import FormationError, build_formation


def test_font_helper_falls_back_when_no_truetype_font_exists(monkeypatch):
    real = ImageFont.truetype

    def missing(font, *args, **kwargs):
        if isinstance(font, str):
            raise OSError("missing")
        return real(font, *args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", missing)
    assert rendering._font(20, bold=True) is not None


def test_stopwatch_rounds_to_the_next_minute():
    minutes, tenths = divmod(round(59.97 * 10), 600)
    assert f"{minutes}:{tenths / 10:04.1f}" == "1:00.0"


def test_sub_frame_speed_segment_is_rejected_before_ffmpeg():
    tiny = speed_drawing(speed=4)
    tiny["start_seconds"] = EVENT["start_seconds"] + 0.01
    tiny["end_seconds"] = tiny["start_seconds"] + 0.01
    with pytest.raises(rendering.RenderError):
        rendering.validate_plan([EVENT], [tiny], MEDIA)


def test_formation_orders_area_below_links_and_validates_inputs():
    results = [tracking_result(moving_boxes(0.1 + 0.2 * i)) for i in range(3)]
    plan = build_formation(results, media=FORMATION_MEDIA, event_id="z", area=True)
    kinds = [mark["kind"] for mark in plan["drawings"]]
    assert kinds.index("area") < kinds.index("line")
    with pytest.raises(FormationError):
        build_formation(results, media=FORMATION_MEDIA, event_id="z", rings=1)
    with pytest.raises(FormationError):
        build_formation(
            results,
            media=FORMATION_MEDIA,
            event_id="z",
            tags=[{"position": "CB", "name": 0}, None, None],
        )
    with pytest.raises(FormationError):
        build_formation(
            results,
            media=FORMATION_MEDIA,
            event_id="z",
            label="x",
            hold_at_seconds=-1.0,
        )


def test_nametag_at_the_frame_edge_still_renders(tmp_path):
    mark = drawing("nametag", [[0.01, 0.8]], position="CF", text="Edge")
    assert isinstance(paint(tmp_path, mark, "#000000"), Image.Image)


def test_xml_text_keeps_whitespace_as_spaces_and_xlsx_has_gray125_fill():
    assert stats_export._clean_xml_text("a\nb\tc") == "a b c"
    with open(stats_export.__file__, encoding="utf-8") as handle:
        assert "gray125" in handle.read()


def test_verified_json_reader_rejects_symlinks_duplicates_and_changes(tmp_path):
    import hashlib

    from once_sport_mcp.workflow import _read_verified_json

    def artifact(path, raw):
        path.write_bytes(raw)
        return {
            "path": str(path),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    good = artifact(tmp_path / "a.json", b'{"a": 1}')
    assert _read_verified_json(good) == {"a": 1}
    for raw in (b'{"a": 1, "a": 2}', b'{"a": NaN}'):
        with pytest.raises(ValueError):
            _read_verified_json(artifact(tmp_path / "b.json", raw))
    stale = dict(good, sha256="0" * 64)
    with pytest.raises(ValueError):
        _read_verified_json(stale)
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "a.json")
    with pytest.raises(OSError):
        _read_verified_json(dict(good, path=str(link)))
