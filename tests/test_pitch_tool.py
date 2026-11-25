"""build_pitch_drawings helper, MCP/CLI wiring and clock offsets in timelines."""

import pytest

from once_sport_mcp import pitch, pitch_tool, rendering
from once_sport_mcp.tools import validate_timeline

PITCH = [[0, 0], [105, 0], [105, 68], [0, 68]]
IMAGE = [[0.3, 0.3], [0.7, 0.3], [0.95, 0.9], [0.05, 0.9]]
MEDIA = {
    "duration": 10.0,
    "width": 320,
    "height": 180,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": False,
}
EVENT = {
    "event_id": "e",
    "start_seconds": 0.0,
    "end_seconds": 5.0,
    "team": "A",
    "phase": "attacking",
    "action": "x",
}


@pytest.mark.parametrize(
    ("template", "params"),
    [
        ("circle", {"centre_m": [52.5, 34], "radius_m": 9.15}),
        ("rectangle", {"x0": 0, "y0": 13.84, "x1": 16.5, "y1": 54.16}),
        ("half_spaces", {}),
        ("five_channels", {}),
        ("zone14", {}),
        ("zone_grid", {"columns": 3, "rows": 3}),
        (
            "cover_shadow",
            {
                "presser_m": [60, 30],
                "ball_m": [50, 34],
                "length_m": 10,
                "half_angle_deg": 25,
            },
        ),
        ("offside_line", {"x_m": 70}),
    ],
)
def test_every_template_yields_renderer_valid_drawings(template, params):
    result = pitch_tool.build_pitch_drawings(
        PITCH, IMAGE, template, "e", 0.0, 4.0, params=params
    )
    assert result["drawings"]
    assert rendering.validate_plan([EVENT], result["drawings"], MEDIA) is None


def test_bad_inputs_raise_pitch_error():
    with pytest.raises(pitch.PitchError):
        pitch_tool.build_pitch_drawings(PITCH, IMAGE, "nope", "e", 0.0, 4.0)
    with pytest.raises(pitch.PitchError):
        pitch_tool.build_pitch_drawings(
            PITCH, IMAGE, "circle", "e", 0.0, 4.0, params={}
        )
    with pytest.raises(pitch.PitchError):
        pitch_tool.build_pitch_drawings(
            PITCH, [[0.1, 0.1]] * 4, "half_spaces", "e", 0.0, 4.0
        )


def test_timeline_accepts_a_clock_offset_and_rejects_bad_values():
    base = {
        "event_id": "e",
        "period": 2,
        "start_seconds": 0,
        "end_seconds": 5,
        "team": "A",
        "phase": "attacking",
        "action": "x",
    }
    assert validate_timeline([dict(base, clock_offset_seconds=2700)])["valid"]
    for bad in (-1, "10", float("inf"), True):
        assert not validate_timeline([dict(base, clock_offset_seconds=bad)])["valid"]
