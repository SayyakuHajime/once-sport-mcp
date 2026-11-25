"""Stateless MCP/CLI helper: calibrated pitch templates as renderer drawings.

The caller supplies four pitch points (metres) and where they appear in the
frame (normalized [0, 1]). Output drawings are ordinary screen-space drawings
projected through that fixed calibration: they suit a static camera or a short
shot. They do not follow camera pans (no camera-motion compensation yet).
"""

from . import pitch

_AREA_TEMPLATES = (
    "circle",
    "rectangle",
    "half_spaces",
    "five_channels",
    "zone14",
    "zone_grid",
    "cover_shadow",
)
TEMPLATES = (*_AREA_TEMPLATES, "offside_line")


def build_pitch_drawings(
    pitch_points,
    image_points,
    template,
    event_id,
    start_seconds,
    end_seconds,
    *,
    id_prefix="pitch",
    color="#6DE0D4",
    opacity=None,
    params=None,
):
    """Return {"drawings": [...]} for one template; raises pitch.PitchError."""
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise pitch.PitchError("params must be an object")
    if template not in TEMPLATES:
        raise pitch.PitchError(f"template must be one of {list(TEMPLATES)}")
    homography = pitch.Homography.from_correspondences(
        [tuple(p) for p in pitch_points], [tuple(p) for p in image_points]
    )
    try:
        if template == "circle":
            geometry = pitch.circle(
                homography, params["centre_m"], params["radius_m"], n=32
            )
        elif template == "rectangle":
            geometry = pitch.rectangle(
                homography, params["x0"], params["y0"], params["x1"], params["y1"]
            )
        elif template == "half_spaces":
            geometry = pitch.half_spaces(homography, params.get("side", "both"))
        elif template == "five_channels":
            geometry = pitch.five_channels(homography)
        elif template == "zone14":
            geometry = pitch.zone14(homography, params.get("attack_direction", 1))
        elif template == "zone_grid":
            geometry = pitch.zone_grid(homography, params["columns"], params["rows"])
        elif template == "cover_shadow":
            geometry = pitch.cover_shadow(
                homography,
                params["presser_m"],
                params["ball_m"],
                params["length_m"],
                params["half_angle_deg"],
            )
        else:
            geometry = pitch.offside_line(homography, params["x_m"])
    except KeyError as exc:
        raise pitch.PitchError(f"missing parameter {exc.args[0]!r}") from None
    kind = "line" if template in ("offside_line", "zone_grid") else "area"
    drawings = pitch.to_drawings(
        geometry,
        kind=kind,
        id=id_prefix,
        event_id=event_id,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        color=color,
        opacity=opacity if kind == "area" else None,
    )
    return {"drawings": drawings, "template": template}
