"""Metric football-pitch geometry projected into normalized image coordinates.

The module uses only the Python standard library. Pitch coordinates are metres:
``x`` runs along the 105 m length and ``y`` across the 68 m width. Projected
points are ``[u, v]`` pairs in the renderer's normalized image coordinate space.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0
PENALTY_BOX_DEPTH_M = 16.5
PENALTY_BOX_WIDTH_M = 40.32
SIX_YARD_BOX_DEPTH_M = 5.5
SIX_YARD_BOX_WIDTH_M = 18.32
CENTRE_CIRCLE_RADIUS_M = 9.15
GOAL_WIDTH_M = 7.32
GOAL_HEIGHT_M = 2.44
GOAL_CENTRE_Y_M = PITCH_WIDTH_M / 2

# American spelling is useful to callers using the common "center circle" term.
CENTER_CIRCLE_RADIUS_M = CENTRE_CIRCLE_RADIUS_M
_EPS = 1e-12
_MAX_CLIP_MAGNITUDE = 1e12
Matrix3 = tuple[tuple[float, float, float], ...]


class PitchError(ValueError):
    """Raised for invalid pitch geometry or an unusable camera homography."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PitchError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise PitchError(f"{name} must be a finite number")
    return number


def _point(value: object, name: str = "point") -> tuple[float, float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise PitchError(f"{name} must be a pair of finite numbers")
    return _finite_number(value[0], name), _finite_number(value[1], name)


def _normalization(points: Sequence[tuple[float, float]]):
    centre_x = sum(point[0] for point in points) / len(points)
    centre_y = sum(point[1] for point in points) / len(points)
    mean_distance = sum(
        math.hypot(point[0] - centre_x, point[1] - centre_y) for point in points
    ) / len(points)
    if not math.isfinite(mean_distance) or mean_distance <= _EPS:
        raise PitchError("Correspondences are singular or ill-conditioned")
    scale = math.sqrt(2.0) / mean_distance
    transform = (
        (scale, 0.0, -scale * centre_x),
        (0.0, scale, -scale * centre_y),
        (0.0, 0.0, 1.0),
    )
    inverse = (
        (1.0 / scale, 0.0, centre_x),
        (0.0, 1.0 / scale, centre_y),
        (0.0, 0.0, 1.0),
    )
    normalized = [(scale * (x - centre_x), scale * (y - centre_y)) for x, y in points]
    return normalized, transform, inverse


def _collinear_triple(points: Sequence[tuple[float, float]]) -> bool:
    span = max(
        1.0,
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
    )
    tolerance = 1e-12 * span * span
    for first, second, third in itertools.combinations(points, 3):
        area_twice = (second[0] - first[0]) * (third[1] - first[1]) - (
            second[1] - first[1]
        ) * (third[0] - first[0])
        if abs(area_twice) <= tolerance:
            return True
    return False


def _norm_inf(matrix: Sequence[Sequence[float]]) -> float:
    return max(sum(abs(value) for value in row) for row in matrix)


def _solve_8x8(matrix: Sequence[Sequence[float]], rhs: Sequence[float]):
    """Solve an 8x8 system and estimate its condition using partial pivots."""
    size = 8
    augmented = [
        list(matrix[row])
        + [float(rhs[row])]
        + [1.0 if row == column else 0.0 for column in range(size)]
        for row in range(size)
    ]
    for column in range(size):
        pivot_row = max(
            range(column, size), key=lambda row: abs(augmented[row][column])
        )
        pivot = augmented[pivot_row][column]
        if not math.isfinite(pivot) or abs(pivot) <= 1e-14:
            raise PitchError("Correspondences are singular or ill-conditioned")
        augmented[column], augmented[pivot_row] = (
            augmented[pivot_row],
            augmented[column],
        )
        pivot = augmented[column][column]
        augmented[column] = [value / pivot for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [
                    value - factor * pivot_value
                    for value, pivot_value in zip(augmented[row], augmented[column])
                ]
    inverse = [row[size + 1 :] for row in augmented]
    condition = _norm_inf(matrix) * _norm_inf(inverse)
    if not math.isfinite(condition) or condition > 1e12:
        raise PitchError("Correspondences are singular or ill-conditioned")
    solution = [row[size] for row in augmented]
    if not all(math.isfinite(value) for value in solution):
        raise PitchError("Correspondences are singular or ill-conditioned")
    return solution


def _multiply3(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]
) -> Matrix3:
    return cast(
        Matrix3,
        tuple(
            tuple(
                sum(left[row][index] * right[index][column] for index in range(3))
                for column in range(3)
            )
            for row in range(3)
        ),
    )


def _inverse3(matrix: Sequence[Sequence[float]]) -> Matrix3:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    cofactors = (
        (e * i - f * h, c * h - b * i, b * f - c * e),
        (f * g - d * i, a * i - c * g, c * d - a * f),
        (d * h - e * g, b * g - a * h, a * e - b * d),
    )
    determinant = a * cofactors[0][0] + b * cofactors[1][0] + c * cofactors[2][0]
    scale = max(abs(value) for row in matrix for value in row)
    if (
        not math.isfinite(determinant)
        or scale == 0
        or abs(determinant) <= 1e-15 * scale**3
    ):
        raise PitchError("Homography is singular or ill-conditioned")
    inverse = cast(
        Matrix3, tuple(tuple(value / determinant for value in row) for row in cofactors)
    )
    condition = _norm_inf(matrix) * _norm_inf(inverse)
    if not math.isfinite(condition) or condition > 1e12:
        raise PitchError("Homography is singular or ill-conditioned")
    return inverse


def _apply_matrix(matrix: Sequence[Sequence[float]], x: float, y: float):
    result = [
        matrix[row][0] * x + matrix[row][1] * y + matrix[row][2] for row in range(3)
    ]
    denominator = result[2]
    if not math.isfinite(denominator) or denominator <= _EPS:
        raise PitchError("Point is at or behind the camera horizon")
    projected = result[0] / denominator, result[1] / denominator
    if not all(math.isfinite(value) for value in projected):
        raise PitchError("Projection produced a non-finite point")
    return projected


@dataclass(frozen=True, slots=True)
class Homography:
    """A calibrated pitch-to-image projective transform and its inverse."""

    _matrix: Matrix3
    _inverse: Matrix3

    @classmethod
    def from_correspondences(
        cls,
        pitch_points: Sequence[Sequence[float]],
        image_points: Sequence[Sequence[float]],
    ) -> Homography:
        """Fit a homography from exactly four pitch/image point correspondences.

        Image coordinates must already be normalized to [0, 1]. Each set must
        have no collinear triple. The normalized 8-parameter DLT is solved by
        Gaussian elimination with partial pivoting and condition checks.
        """
        if (
            not isinstance(pitch_points, Sequence)
            or isinstance(pitch_points, (str, bytes))
            or not isinstance(image_points, Sequence)
            or isinstance(image_points, (str, bytes))
            or len(pitch_points) != 4
            or len(image_points) != 4
        ):
            raise PitchError("Homography requires exactly four correspondences")
        pitch = [_point(value, "pitch point") for value in pitch_points]
        image = [_point(value, "image point") for value in image_points]
        if any(not (0 <= u <= 1 and 0 <= v <= 1) for u, v in image):
            raise PitchError("Image points must be normalized to [0, 1]")
        if _collinear_triple(pitch) or _collinear_triple(image):
            raise PitchError("Correspondences must have no three collinear points")

        normalized_pitch, source_transform, source_inverse = _normalization(pitch)
        normalized_image, image_transform, image_inverse = _normalization(image)
        coefficients = []
        values = []
        for (x, y), (u, v) in zip(normalized_pitch, normalized_image):
            coefficients.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
            values.append(u)
            coefficients.append([0, 0, 0, x, y, 1, -v * x, -v * y])
            values.append(v)
        solution = _solve_8x8(coefficients, values)
        normalized_h = (
            (solution[0], solution[1], solution[2]),
            (solution[3], solution[4], solution[5]),
            (solution[6], solution[7], 1.0),
        )
        normalized_h_inverse = _inverse3(normalized_h)
        matrix = _multiply3(image_inverse, _multiply3(normalized_h, source_transform))
        inverse = _multiply3(
            source_inverse, _multiply3(normalized_h_inverse, image_transform)
        )
        centre_x = sum(point[0] for point in pitch) / 4
        centre_y = sum(point[1] for point in pitch) / 4
        centre_w = matrix[2][0] * centre_x + matrix[2][1] * centre_y + matrix[2][2]
        if not math.isfinite(centre_w) or abs(centre_w) <= _EPS:
            raise PitchError("Homography is singular or ill-conditioned")
        scale = 1.0 / centre_w
        matrix = cast(
            Matrix3, tuple(tuple(value * scale for value in row) for row in matrix)
        )
        inverse = cast(
            Matrix3, tuple(tuple(value / scale for value in row) for row in inverse)
        )
        return cls(matrix, inverse)

    @property
    def matrix(self) -> Matrix3:
        """Return the calibrated 3x3 pitch-to-image matrix."""
        return self._matrix

    def to_image(self, x_m: float, y_m: float) -> tuple[float, float]:
        """Project one pitch point to normalized image coordinates."""
        x = _finite_number(x_m, "x_m")
        y = _finite_number(y_m, "y_m")
        return _apply_matrix(self._matrix, x, y)

    def to_pitch(self, u: float, v: float) -> tuple[float, float]:
        """Map a normalized image point back to pitch metres."""
        image_x = _finite_number(u, "u")
        image_y = _finite_number(v, "v")
        x, y = _apply_matrix(self._inverse, image_x, image_y)
        # The inverse matrix's homogeneous sign is arbitrary. Check visibility
        # with the forward transform, whose scale is canonicalized at pitch
        # calibration centre.
        _apply_matrix(self._matrix, x, y)
        return x, y


def _project_point(homography: Homography, point: Sequence[float]):
    x, y = _point(point, "pitch point")
    u, v = homography.to_image(x, y)
    if max(abs(u), abs(v)) > _MAX_CLIP_MAGNITUDE:
        raise PitchError("Projected point is too far outside the image to clip")
    return u, v


def _bounded(point: tuple[float, float]) -> list[float]:
    return [min(1.0, max(0.0, point[0])), min(1.0, max(0.0, point[1]))]


def _same_point(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return math.isclose(first[0], second[0], abs_tol=1e-15) and math.isclose(
        first[1], second[1], abs_tol=1e-15
    )


def _clip_polygon(points: Sequence[tuple[float, float]]) -> list[list[float]]:
    """Clip a projected polygon against the four normalized image boundaries."""
    output = list(points)
    boundaries = (
        (
            lambda point: point[0] >= 0,
            lambda a, b: (0.0, a[1] + (b[1] - a[1]) * (-a[0]) / (b[0] - a[0])),
        ),
        (
            lambda point: point[0] <= 1,
            lambda a, b: (1.0, a[1] + (b[1] - a[1]) * (1 - a[0]) / (b[0] - a[0])),
        ),
        (
            lambda point: point[1] >= 0,
            lambda a, b: (a[0] + (b[0] - a[0]) * (-a[1]) / (b[1] - a[1]), 0.0),
        ),
        (
            lambda point: point[1] <= 1,
            lambda a, b: (a[0] + (b[0] - a[0]) * (1 - a[1]) / (b[1] - a[1]), 1.0),
        ),
    )
    for is_inside, intersection in boundaries:
        if not output:
            break
        source = output
        output = []
        previous = source[-1]
        previous_inside = is_inside(previous)
        for current in source:
            current_inside = is_inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(intersection(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersection(previous, current))
            previous = current
            previous_inside = current_inside
    compact: list[tuple[float, float]] = []
    for point in output:
        bounded = (min(1.0, max(0.0, point[0])), min(1.0, max(0.0, point[1])))
        if not compact or not _same_point(compact[-1], bounded):
            compact.append(bounded)
    if len(compact) > 1 and _same_point(compact[0], compact[-1]):
        compact.pop()
    if len(compact) < 3:
        return []
    return [[x, y] for x, y in compact]


def _clip_segment(
    first: tuple[float, float], second: tuple[float, float]
) -> list[list[float]]:
    """Clip one projected line segment with the Liang-Barsky algorithm."""
    x0, y0 = first
    x1, y1 = second
    dx, dy = x1 - x0, y1 - y0
    lower, upper = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, 1 - x0), (-dy, y0), (dy, 1 - y0)):
        if p == 0:
            if q < 0:
                return []
            continue
        ratio = q / p
        if p < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return []
    clipped = [
        _bounded((x0 + lower * dx, y0 + lower * dy)),
        _bounded((x0 + upper * dx, y0 + upper * dy)),
    ]
    first_clipped = (clipped[0][0], clipped[0][1])
    second_clipped = (clipped[1][0], clipped[1][1])
    if _same_point(first_clipped, second_clipped):
        return []
    return clipped


def circle(
    homography: Homography,
    centre_m: Sequence[float],
    radius_m: float,
    n: int = 48,
) -> list[list[float]]:
    """Project a metric ground-plane circle as a clipped image-space polygon."""
    centre_x, centre_y = _point(centre_m, "centre_m")
    radius = _finite_number(radius_m, "radius_m")
    if radius <= 0:
        raise PitchError("radius_m must be positive")
    if type(n) is not int or not 3 <= n <= 512:
        raise PitchError("n must be an integer in [3, 512]")
    pitch_points = [
        (
            centre_x + radius * math.cos(2 * math.pi * index / n),
            centre_y + radius * math.sin(2 * math.pi * index / n),
        )
        for index in range(n)
    ]
    return _clip_polygon([_project_point(homography, point) for point in pitch_points])


def rectangle(
    homography: Homography,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> list[list[float]]:
    """Project and clip the rectangle with metric opposite corners."""
    first_x, first_y = _finite_number(x0, "x0"), _finite_number(y0, "y0")
    second_x, second_y = _finite_number(x1, "x1"), _finite_number(y1, "y1")
    low_x, high_x = sorted((first_x, second_x))
    low_y, high_y = sorted((first_y, second_y))
    if low_x == high_x or low_y == high_y:
        raise PitchError("Rectangle must have positive width and height")
    pitch_points = (
        (low_x, low_y),
        (high_x, low_y),
        (high_x, high_y),
        (low_x, high_y),
    )
    return _clip_polygon([_project_point(homography, point) for point in pitch_points])


def zone_grid(
    homography: Homography, columns: int, rows: int
) -> list[list[list[float]]]:
    """Return clipped interior grid lines dividing the full pitch into cells.

    ``columns`` divides pitch length and ``rows`` divides pitch width. Each
    returned item is one two-point polyline; the outer pitch boundary is omitted.
    """
    if type(columns) is not int or columns < 1:
        raise PitchError("columns must be a positive integer")
    if type(rows) is not int or rows < 1:
        raise PitchError("rows must be a positive integer")
    lines = []
    for column in range(1, columns):
        x = PITCH_LENGTH_M * column / columns
        line = _clip_segment(
            _project_point(homography, (x, 0)),
            _project_point(homography, (x, PITCH_WIDTH_M)),
        )
        if line:
            lines.append(line)
    for row in range(1, rows):
        y = PITCH_WIDTH_M * row / rows
        line = _clip_segment(
            _project_point(homography, (0, y)),
            _project_point(homography, (PITCH_LENGTH_M, y)),
        )
        if line:
            lines.append(line)
    return lines


def five_channels(homography: Homography) -> list[list[list[float]]]:
    """Return five clipped longitudinal pitch-channel polygons, ordered low-y to high-y."""
    lane_width = PITCH_WIDTH_M / 5
    return [
        rectangle(
            homography,
            0,
            index * lane_width,
            PITCH_LENGTH_M,
            (index + 1) * lane_width,
        )
        for index in range(5)
    ]


def half_spaces(homography: Homography, side: str = "both") -> list[list[list[float]]]:
    """Return one or both standard half-space corridors as clipped polygons.

    The 68 m width is split into five equal 13.6 m lanes. ``left`` selects the
    lower-y half-space, ``right`` the higher-y half-space, and ``both`` returns
    both in low-y to high-y order. These names refer to pitch coordinates, not
    screen orientation.
    """
    if side not in ("left", "right", "both"):
        raise PitchError("side must be 'left', 'right', or 'both'")
    lane_width = PITCH_WIDTH_M / 5
    indices = {"left": (1,), "right": (3,), "both": (1, 3)}[side]
    return [
        rectangle(
            homography,
            0,
            index * lane_width,
            PITCH_LENGTH_M,
            (index + 1) * lane_width,
        )
        for index in indices
    ]


def zone14(homography: Homography, attack_direction: int = 1) -> list[list[float]]:
    """Project Zone 14: the central box-width band, 22.5 m before the goal box.

    For attack_direction=1 this spans x=66..88.5 m and y=13.84..54.16 m.
    For attack_direction=-1 the x interval is mirrored to 16.5..39 m.
    """
    if type(attack_direction) is not int or attack_direction not in (-1, 1):
        raise PitchError("attack_direction must be 1 or -1")
    box_low_y = (PITCH_WIDTH_M - PENALTY_BOX_WIDTH_M) / 2
    box_high_y = PITCH_WIDTH_M - box_low_y
    if attack_direction == 1:
        x0, x1 = 66.0, 88.5
    else:
        x0, x1 = PITCH_LENGTH_M - 88.5, PITCH_LENGTH_M - 66.0
    return rectangle(homography, x0, box_low_y, x1, box_high_y)


def offside_line(homography: Homography, x_m: float) -> list[list[float]]:
    """Project the pitch-width line at ``x_m``; it is parallel to goal lines."""
    x = _finite_number(x_m, "x_m")
    return _clip_segment(
        _project_point(homography, (x, 0)),
        _project_point(homography, (x, PITCH_WIDTH_M)),
    )


def cover_shadow(
    homography: Homography,
    presser_m: Sequence[float],
    ball_m: Sequence[float],
    length_m: float,
    half_angle_deg: float,
) -> list[list[float]]:
    """Project a triangular ground fan extending from a presser away from the ball."""
    presser_x, presser_y = _point(presser_m, "presser_m")
    ball_x, ball_y = _point(ball_m, "ball_m")
    length = _finite_number(length_m, "length_m")
    half_angle = _finite_number(half_angle_deg, "half_angle_deg")
    if length <= 0:
        raise PitchError("length_m must be positive")
    if not 0 < half_angle <= 90:
        raise PitchError("half_angle_deg must be in (0, 90]")
    away_x, away_y = presser_x - ball_x, presser_y - ball_y
    distance = math.hypot(away_x, away_y)
    if distance <= _EPS:
        raise PitchError("presser_m and ball_m must be different points")
    unit_x, unit_y = away_x / distance, away_y / distance
    angle = math.radians(half_angle)
    far_points = []
    for sign in (1, -1):
        cosine, sine = math.cos(sign * angle), math.sin(sign * angle)
        direction_x = unit_x * cosine - unit_y * sine
        direction_y = unit_x * sine + unit_y * cosine
        far_points.append(
            (presser_x + length * direction_x, presser_y + length * direction_y)
        )
    pitch_points = [(presser_x, presser_y), *far_points]
    return _clip_polygon([_project_point(homography, point) for point in pitch_points])


def distance_m(p_m: Sequence[float], q_m: Sequence[float]) -> float:
    """Return Euclidean distance in metres between two pitch points."""
    p_x, p_y = _point(p_m, "p_m")
    q_x, q_y = _point(q_m, "q_m")
    return math.hypot(q_x - p_x, q_y - p_y)


def label_at_midpoint(
    homography: Homography,
    p_m: Sequence[float],
    q_m: Sequence[float],
) -> list[float]:
    """Return the clipped normalized image point halfway between two pitch points."""
    p_x, p_y = _point(p_m, "p_m")
    q_x, q_y = _point(q_m, "q_m")
    return _bounded(_project_point(homography, ((p_x + q_x) / 2, (p_y + q_y) / 2)))


def _geometry_list(geometries: object) -> list[Sequence[Sequence[float]]]:
    if not isinstance(geometries, Sequence) or isinstance(geometries, (str, bytes)):
        raise PitchError("geometries must be a point list or a list of point lists")
    if not geometries:
        return []
    first = geometries[0]
    if (
        isinstance(first, Sequence)
        and not isinstance(first, (str, bytes))
        and len(first) == 2
        and all(_is_number(value) for value in first)
    ):
        return [cast(Sequence[Sequence[float]], geometries)]
    result: list[Sequence[Sequence[float]]] = []
    for geometry in geometries:
        if not isinstance(geometry, Sequence) or isinstance(geometry, (str, bytes)):
            raise PitchError("Each geometry must be a list of normalized points")
        if geometry:
            result.append(cast(Sequence[Sequence[float]], geometry))
    return result


def to_drawings(
    geometries: object,
    *,
    kind: str,
    id: str,
    event_id: str,
    start_seconds: float,
    end_seconds: float,
    color: str = "#6DE0D4",
    opacity: float | None = None,
) -> list[dict]:
    """Build renderer-schema drawing dictionaries from one or more geometries.

    ``kind`` is one of ``area``, ``polyline``, ``polygon``, or ``line``. A
    single point-list creates one drawing; a list of point-lists creates one
    drawing per geometry, with stable IDs ``id``, ``id-2``, and so on.
    """
    allowed_kinds = {"area", "polyline", "polygon", "line"}
    if kind not in allowed_kinds:
        raise PitchError(f"kind must be one of {sorted(allowed_kinds)}")
    if not isinstance(id, str) or not id:
        raise PitchError("id must be nonempty text")
    if not isinstance(event_id, str) or not event_id:
        raise PitchError("event_id must be nonempty text")
    start = _finite_number(start_seconds, "start_seconds")
    end = _finite_number(end_seconds, "end_seconds")
    if start < 0 or end <= start:
        raise PitchError("drawing times must satisfy 0 <= start_seconds < end_seconds")
    if (
        not isinstance(color, str)
        or len(color) != 7
        or color[0] != "#"
        or any(character not in "0123456789abcdefABCDEF" for character in color[1:])
    ):
        raise PitchError("color must be a #RRGGBB value")
    if opacity is not None:
        opacity_value = _finite_number(opacity, "opacity")
        if kind != "area" or not 0.05 <= opacity_value <= 0.8:
            raise PitchError("opacity is only valid for area drawings in [0.05, 0.8]")
    else:
        opacity_value = None

    drawings = []
    for index, geometry in enumerate(_geometry_list(geometries)):
        points = [_point(value, "image point") for value in geometry]
        if kind == "line" and len(points) != 2:
            raise PitchError("line drawings require exactly two points")
        if kind == "polyline" and not 2 <= len(points) <= 32:
            raise PitchError("polyline drawings require 2 to 32 points")
        if kind == "area" and not 3 <= len(points) <= 32:
            raise PitchError("area drawings require 3 to 32 points")
        if kind == "polygon" and len(points) < 3:
            raise PitchError("polygon drawings require at least three points")
        if any(not 0 <= value <= 1 for point in points for value in point):
            raise PitchError("drawing image points must be normalized to [0, 1]")
        drawing = {
            "id": id if index == 0 else f"{id}-{index + 1}",
            "event_id": event_id,
            "kind": kind,
            "start_seconds": start,
            "end_seconds": end,
            "points": [[x, y] for x, y in points],
            "color": color,
        }
        if opacity_value is not None:
            drawing["opacity"] = opacity_value
        drawings.append(drawing)
    return drawings
