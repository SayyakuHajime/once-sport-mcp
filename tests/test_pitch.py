"""Tests for metric pitch geometry projected into normalized image space."""

import math

import pytest

from once_sport_mcp.pitch import (
    CENTRE_CIRCLE_RADIUS_M,
    GOAL_WIDTH_M,
    PENALTY_BOX_DEPTH_M,
    PENALTY_BOX_WIDTH_M,
    PITCH_LENGTH_M,
    PITCH_WIDTH_M,
    SIX_YARD_BOX_DEPTH_M,
    SIX_YARD_BOX_WIDTH_M,
    Homography,
    PitchError,
    circle,
    cover_shadow,
    distance_m,
    five_channels,
    half_spaces,
    label_at_midpoint,
    offside_line,
    rectangle,
    to_drawings,
    zone14,
    zone_grid,
)
from once_sport_mcp.rendering import validate_plan

MEDIA = {
    "duration": 3.0,
    "width": 320,
    "height": 180,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": False,
}
EVENT = {
    "event_id": "one",
    "start_seconds": 0.0,
    "end_seconds": 2.0,
    "team": "Home",
    "phase": "Attack",
    "action": "Pass",
}

PITCH_CORNERS = [(0, 0), (105, 0), (105, 68), (0, 68)]
OBLIQUE_IMAGE_CORNERS = [
    (0.12, 0.64),
    (0.88, 0.585),
    (0.78, 0.36),
    (0.18, 0.405),
]


def oblique_homography():
    return Homography.from_correspondences(PITCH_CORNERS, OBLIQUE_IMAGE_CORNERS)


def identity_homography():
    return Homography.from_correspondences(
        PITCH_CORNERS, [(0, 0), (1, 0), (1, 1), (0, 1)]
    )


def unproject_all(h, points):
    return [h.to_pitch(*point) for point in points]


def test_homography_round_trip_is_precise_for_oblique_camera():
    h = oblique_homography()
    for pitch_point in ((0, 0), (105, 68), (23.25, 17.75), (77.5, 49.125)):
        image_point = h.to_image(*pitch_point)
        assert all(0 <= value <= 1 for value in image_point)
        recovered = h.to_pitch(*image_point)
        assert recovered == pytest.approx(pitch_point, abs=1e-9)


def test_homography_rejects_bad_correspondence_sets():
    with pytest.raises(PitchError, match="exactly four"):
        Homography.from_correspondences(PITCH_CORNERS[:3], OBLIQUE_IMAGE_CORNERS[:3])

    collinear_pitch = [(0, 0), (1, 1), (2, 2), (0, 3)]
    with pytest.raises(PitchError, match="collinear"):
        Homography.from_correspondences(collinear_pitch, OBLIQUE_IMAGE_CORNERS)

    collinear_image = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.8, 0.1)]
    with pytest.raises(PitchError, match="collinear"):
        Homography.from_correspondences(PITCH_CORNERS, collinear_image)

    out_of_range = [(0.1, 0.1), (1.1, 0.1), (0.8, 0.8), (0.1, 0.8)]
    with pytest.raises(PitchError, match="normalized"):
        Homography.from_correspondences(PITCH_CORNERS, out_of_range)


def test_circle_projects_as_a_wide_ellipse_for_oblique_camera():
    points = circle(oblique_homography(), (52.5, 34), CENTRE_CIRCLE_RADIUS_M)
    width = max(point[0] for point in points) - min(point[0] for point in points)
    height = max(point[1] for point in points) - min(point[1] for point in points)
    assert len(points) == 48
    assert width > height
    assert all(0 <= coordinate <= 1 for point in points for coordinate in point)


def test_ground_polygons_are_clipped_to_the_normalized_image():
    h = oblique_homography()
    points = rectangle(h, -30, 20, 20, 50)
    assert len(points) >= 3
    assert any(x == 0 for x, _ in points)
    assert all(0 <= coordinate <= 1 for point in points for coordinate in point)
    assert rectangle(h, -1000, 10, -900, 20) == []


def test_offside_line_projects_across_pitch_parallel_to_goal_line():
    h = oblique_homography()
    image_points = offside_line(h, 55)
    pitch_points = unproject_all(h, image_points)
    assert len(image_points) == 2
    assert pitch_points[0][0] == pytest.approx(55, abs=1e-9)
    assert pitch_points[1][0] == pytest.approx(55, abs=1e-9)
    assert {round(point[1], 8) for point in pitch_points} == {0.0, 68.0}


def test_five_channels_and_half_spaces_use_equal_13_point_6_m_lanes():
    h = identity_homography()
    channels = five_channels(h)
    assert len(channels) == 5
    for channel in channels:
        pitch_points = unproject_all(h, channel)
        ys = [point[1] for point in pitch_points]
        assert max(ys) - min(ys) == pytest.approx(13.6, abs=1e-9)

    left = half_spaces(h, "left")
    right = half_spaces(h, "right")
    both = half_spaces(h, "both")
    assert len(left) == len(right) == 1
    assert len(both) == 2
    for corridor in [*left, *right]:
        pitch_points = unproject_all(h, corridor)
        ys = [point[1] for point in pitch_points]
        assert max(ys) - min(ys) == pytest.approx(13.6, abs=1e-9)


def test_zone14_attack_direction_and_grid_geometry():
    h = identity_homography()
    attacking_right = unproject_all(h, zone14(h, attack_direction=1))
    attacking_left = unproject_all(h, zone14(h, attack_direction=-1))
    assert {round(point[0], 8) for point in attacking_right} == {66.0, 88.5}
    assert {round(point[0], 8) for point in attacking_left} == {16.5, 39.0}
    assert len(zone_grid(h, columns=3, rows=2)) == 3


def test_cover_shadow_fans_away_from_the_ball_and_measurement_helpers():
    h = identity_homography()
    shadow = cover_shadow(h, (50, 34), (45, 34), length_m=12, half_angle_deg=30)
    pitch_points = unproject_all(h, shadow)
    assert len(pitch_points) == 3
    assert pitch_points[0] == pytest.approx((50, 34), abs=1e-9)
    assert pitch_points[1][0] > 50 and pitch_points[2][0] > 50
    assert distance_m((0, 0), (3, 4)) == pytest.approx(5)
    assert label_at_midpoint(h, (0, 0), (10, 0)) == pytest.approx((5 / 105, 0))


def test_projected_primitives_convert_to_valid_rendering_drawings():
    h = identity_homography()
    shapes = [
        to_drawings(
            rectangle(h, 10, 10, 30, 30),
            kind="area",
            id="zone",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#6DE0D4",
        ),
        to_drawings(
            offside_line(h, 50),
            kind="line",
            id="offside",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#FF2020",
        ),
        to_drawings(
            zone_grid(h, 2, 2),
            kind="polyline",
            id="grid",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#6DE0D4",
        ),
    ]
    drawings = [drawing for group in shapes for drawing in group]
    assert validate_plan([EVENT], drawings, MEDIA) is None


def test_pitch_constants_and_template_drawings_validate():
    assert (PITCH_LENGTH_M, PITCH_WIDTH_M) == (105.0, 68.0)
    assert (PENALTY_BOX_DEPTH_M, PENALTY_BOX_WIDTH_M) == (16.5, 40.32)
    assert (SIX_YARD_BOX_DEPTH_M, SIX_YARD_BOX_WIDTH_M) == (5.5, 18.32)
    assert GOAL_WIDTH_M == pytest.approx(7.32)

    h = oblique_homography()
    drawings = [
        *to_drawings(
            circle(h, (52.5, 34), CENTRE_CIRCLE_RADIUS_M),
            kind="polygon",
            id="centre-circle",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#6DE0D4",
        ),
        *to_drawings(
            half_spaces(h, "both"),
            kind="area",
            id="half-space",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#FFD166",
            opacity=0.25,
        ),
        *to_drawings(
            cover_shadow(h, (50, 34), (45, 34), length_m=12, half_angle_deg=25),
            kind="area",
            id="shadow",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#FF6B6B",
        ),
        *to_drawings(
            zone14(h, attack_direction=1),
            kind="area",
            id="zone14",
            event_id="one",
            start_seconds=0,
            end_seconds=2,
            color="#3DDC97",
        ),
    ]
    assert validate_plan([EVENT], drawings, MEDIA) is None


def test_projection_rejects_points_at_or_behind_the_camera_horizon():
    def project(x, y):
        denominator = 1 - 0.02 * x
        u, v = x / denominator, y / denominator
        return (u + 100) / 100, (v + 70) / 140

    image_corners = [project(x, y) for x, y in PITCH_CORNERS]
    h = Homography.from_correspondences(PITCH_CORNERS, image_corners)
    with pytest.raises(PitchError, match="camera horizon"):
        h.to_image(0, 34)
    with pytest.raises(PitchError, match="camera horizon"):
        h.to_pitch(0.5, 0.5)
    assert all(math.isfinite(value) for value in h.to_image(80, 34))
