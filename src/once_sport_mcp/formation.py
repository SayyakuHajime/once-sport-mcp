"""Pure formation-plan composition from independent tracking results."""

import itertools
import math
import re

from . import rendering


class FormationError(ValueError):
    """Invalid tracking inputs or an invalid composed formation plan."""


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _box(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(_number(part) for part in value)
        or value[2] <= 0
        or value[3] <= 0
        or value[0] < 0
        or value[1] < 0
        or value[0] + value[2] > 1
        or value[1] + value[3] > 1
    ):
        raise FormationError("Bounding boxes must be normalized xywh boxes")
    return list(value)


def _iou(first, second):
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first[2] * first[3] + second[2] * second[3] - intersection
    return intersection / union if union else 0.0


def _sample_map(result):
    samples = result.get("samples")
    if not isinstance(samples, list) or not samples:
        raise FormationError("Each tracking result needs samples")
    mapped = {}
    previous = -math.inf
    for sample in samples:
        if not isinstance(sample, dict):
            raise FormationError("Tracking samples must be objects")
        frame = sample.get("source_frame_index")
        time = sample.get("source_seconds")
        if type(frame) is not int or frame < 0 or not _number(time) or time < previous:
            raise FormationError(
                "Tracking samples need ordered frame indices and times"
            )
        if frame in mapped:
            raise FormationError("Tracking sample frame indices must be unique")
        bbox = _box(sample.get("bbox"))
        anchor = sample.get("anchor")
        if (
            not isinstance(anchor, (list, tuple))
            or len(anchor) != 2
            or not all(_number(part) and 0 <= part <= 1 for part in anchor)
        ):
            raise FormationError("Tracking anchors must be normalized coordinate pairs")
        mapped[frame] = {"time": time, "bbox": bbox, "anchor": list(anchor)}
        previous = time
    return mapped


def _seed_bbox(result):
    seed = result.get("exact_seed")
    if not isinstance(seed, dict):
        raise FormationError("Each tracking result needs an exact seed")
    return _box(seed.get("bbox_normalized_xywh"))


def _default_links(seeds):
    order = sorted(
        range(len(seeds)),
        key=lambda index: (seeds[index][0] + seeds[index][2] / 2, index),
    )
    return [[first, second] for first, second in itertools.pairwise(order)]


def build_formation(
    results,
    *,
    media,
    event_id,
    id_prefix="formation",
    links=None,
    closed=False,
    rings=True,
    area=False,
    area_color="#6DE0D4",
    area_opacity=0.2,
    spotlights=False,
    color="#6DE0D4",
    ring_color="#FFD166",
    label=None,
    hold_at_seconds=None,
    hold_seconds=None,
    tags=None,
):
    """Compose tracked targets into editable line, ring, and optional label drawings."""
    if not isinstance(results, list) or not 2 <= len(results) <= 11:
        raise FormationError(
            "A formation requires between two and eleven tracking results"
        )
    if not isinstance(rings, bool):
        raise FormationError("rings must be a boolean")
    if tags is not None:
        if (
            not isinstance(tags, list)
            or len(tags) != len(results)
            or not all(tag is None or isinstance(tag, dict) for tag in tags)
        ):
            raise FormationError(
                "tags must be a list with one dict (or null) per tracked target"
            )
        for tag in tags:
            if tag is not None and any(
                not isinstance(value, str) for value in tag.values()
            ):
                raise FormationError("Tag position and name must be strings")
            if tag is not None and (
                set(tag) - {"position", "name"}
                or not (tag.get("position") or tag.get("name"))
            ):
                raise FormationError(
                    "Each tag needs a position and/or name and no other keys"
                )
    if not isinstance(area, bool) or not isinstance(spotlights, bool):
        raise FormationError("area and spotlights must be booleans")
    if spotlights and rings is not False:
        raise FormationError("spotlights require rings=False")
    if not isinstance(event_id, str) or not event_id:
        raise FormationError("event_id must be a nonempty string")
    if not isinstance(id_prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", id_prefix):
        raise FormationError(
            "id_prefix must contain only letters, digits, hyphens, or underscores"
        )
    if any(not isinstance(result, dict) for result in results):
        raise FormationError("Tracking results must be objects")
    requested = [result.get("requested_window") for result in results]
    if any(not isinstance(window, dict) for window in requested) or any(
        window != requested[0] for window in requested[1:]
    ):
        raise FormationError("Tracking results must have identical requested windows")
    window = requested[0]
    requested_start, requested_end = (
        window.get("start_seconds"),
        window.get("end_seconds"),
    )
    if not (
        _number(requested_start)
        and _number(requested_end)
        and 0 <= requested_start < requested_end
    ):
        raise FormationError("Invalid requested tracking window")
    metadata = [result.get("source_metadata") for result in results]
    if any(not isinstance(item, dict) for item in metadata):
        raise FormationError("Each tracking result needs source metadata")
    fps = metadata[0].get("fps")
    if (
        not _number(fps)
        or fps <= 0
        or any(item.get("fps") != fps for item in metadata[1:])
    ):
        raise FormationError("Tracking results must have the same valid frame rate")
    if not isinstance(media, dict):
        raise FormationError("media must be an object")
    width, height = media.get("width"), media.get("height")
    duration = media.get("duration")
    if (
        type(width) is not int
        or width <= 0
        or type(height) is not int
        or height <= 0
        or not _number(duration)
        or duration <= 0
    ):
        raise FormationError("Invalid media dimensions or duration")
    if label is None:
        if hold_at_seconds is not None or hold_seconds is not None:
            raise FormationError("hold_at_seconds and hold_seconds require a label")
    else:
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 120
            or any(ord(character) < 32 for character in label)
        ):
            raise FormationError(
                "label must be a nonempty single-line string of at most 120 characters"
            )
        if hold_at_seconds is not None and not _number(hold_at_seconds):
            raise FormationError("hold_at_seconds must be finite")
        if hold_seconds is not None and (
            not _number(hold_seconds) or not 0 < hold_seconds <= 10
        ):
            raise FormationError("hold_seconds must be finite and in (0, 10]")

    seeds = [_seed_bbox(result) for result in results]
    for first in range(len(seeds)):
        for second in range(first + 1, len(seeds)):
            if _iou(seeds[first], seeds[second]) > 0:
                raise FormationError("Seed bounding boxes must not overlap")

    sample_maps = [_sample_map(result) for result in results]
    first_frame = max(min(samples) for samples in sample_maps)
    common = set.intersection(*(set(samples) for samples in sample_maps))
    frame_indices = []
    frame = first_frame
    while frame in common:
        frame_indices.append(frame)
        frame += 1
    if len(frame_indices) < 2:
        raise FormationError("Formation is too short: fewer than two common frames")
    times = [sample_maps[0][frame]["time"] for frame in frame_indices]
    if any(not times[index] < times[index + 1] for index in range(len(times) - 1)):
        raise FormationError("Common sample times must be strictly increasing")

    conflict = None
    for frame, time in zip(frame_indices, times, strict=True):
        for first, second in itertools.combinations(range(len(results)), 2):
            first_box = sample_maps[first][frame]["bbox"]
            second_box = sample_maps[second][frame]["bbox"]
            first_center = (
                (first_box[0] + first_box[2] / 2) * width,
                (first_box[1] + first_box[3] / 2) * height,
            )
            second_center = (
                (second_box[0] + second_box[2] / 2) * width,
                (second_box[1] + second_box[3] / 2) * height,
            )
            center_distance = math.hypot(
                first_center[0] - second_center[0],
                first_center[1] - second_center[1],
            )
            if _iou(first_box, second_box) >= 0.15 or center_distance < 0.75 * min(
                first_box[2] * width, second_box[2] * width
            ):
                conflict = {
                    "frame_index": frame,
                    "time_seconds": time,
                    "targets": [first, second],
                }
                break
        if conflict is not None:
            break

    if conflict is not None:
        stop = frame_indices.index(conflict["frame_index"])
        frame_indices = frame_indices[:stop]
        times = times[:stop]
    if len(frame_indices) < 2:
        raise FormationError("Formation is too short after possible target conflict")

    start = times[0]
    end = min(times[-1] + 1 / fps, duration)
    if not start < end or end <= times[-1]:
        raise FormationError("Formation is too short within the media duration")

    if not isinstance(closed, bool):
        raise FormationError("closed must be a boolean")
    if links is None:
        selected_links = _default_links(seeds)
        if closed and len(results) >= 3:
            ordering = [index for pair in selected_links for index in pair[:1]]
            ordering.append(selected_links[-1][1])
            selected_links.append([ordering[-1], ordering[0]])
    else:
        if closed:
            raise FormationError("closed cannot be combined with explicit links")
        if not isinstance(links, list):
            raise FormationError("links must be a list of index pairs")
        selected_links = []
        seen_pairs = set()
        for pair in links:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(
                    type(index) is not int or not 0 <= index < len(results)
                    for index in pair
                )
                or pair[0] == pair[1]
            ):
                raise FormationError(
                    "Each link must contain two distinct valid target indices"
                )
            unordered = frozenset(pair)
            if unordered in seen_pairs:
                raise FormationError("Duplicate links are not allowed")
            seen_pairs.add(unordered)
            selected_links.append(list(pair))

    drawings = []
    for first, second in selected_links:
        keyframes = [
            {
                "time_seconds": time,
                "points": [
                    sample_maps[first][frame]["anchor"],
                    sample_maps[second][frame]["anchor"],
                ],
            }
            for frame, time in zip(frame_indices, times, strict=True)
        ]
        drawings.append(
            {
                "id": f"{id_prefix}-line-{first}-{second}",
                "event_id": event_id,
                "kind": "line",
                "start_seconds": start,
                "end_seconds": end,
                "points": keyframes[0]["points"],
                "color": color,
                "keyframes": keyframes,
            }
        )
    if area and len(results) >= 3:
        seed_anchors = [
            sample_maps[index][frame_indices[0]]["anchor"]
            for index in range(len(results))
        ]
        centroid = [
            sum(anchor[axis] for anchor in seed_anchors) / len(seed_anchors)
            for axis in range(2)
        ]
        order = sorted(
            range(len(results)),
            key=lambda index: math.atan2(
                seed_anchors[index][1] - centroid[1],
                seed_anchors[index][0] - centroid[0],
            ),
        )
        keyframes = [
            {
                "time_seconds": time,
                "points": [sample_maps[index][frame]["anchor"] for index in order],
            }
            for frame, time in zip(frame_indices, times, strict=True)
        ]
        drawings.append(
            {
                "id": f"{id_prefix}-area",
                "event_id": event_id,
                "kind": "area",
                "start_seconds": start,
                "end_seconds": end,
                "points": keyframes[0]["points"],
                "color": area_color,
                "opacity": area_opacity,
                "keyframes": keyframes,
            }
        )
    if rings or spotlights:
        for index, result in enumerate(results):
            radii = result.get("fixed_radii")
            if (
                not isinstance(radii, (list, tuple))
                or len(radii) != 2
                or not all(_number(radius) and 0 < radius <= 1 for radius in radii)
            ):
                raise FormationError(
                    "Each tracking result needs normalized fixed radii"
                )
            radius_pair = list(radii)
            keyframes = [
                {
                    "time_seconds": time,
                    "points": [sample_maps[index][frame]["anchor"], radius_pair],
                }
                for frame, time in zip(frame_indices, times, strict=True)
            ]
            drawings.append(
                {
                    "id": (
                        f"{id_prefix}-spot-{index}"
                        if spotlights
                        else f"{id_prefix}-ring-{index}"
                    ),
                    "event_id": event_id,
                    "kind": "spotlight" if spotlights else "ellipse",
                    "start_seconds": start,
                    "end_seconds": end,
                    "points": keyframes[0]["points"],
                    "color": ring_color,
                    "keyframes": keyframes,
                }
            )

    for index, tag in enumerate(tags or []):
        if tag is None:
            continue
        keyframes = [
            {
                "time_seconds": time,
                "points": [sample_maps[index][frame]["anchor"]],
            }
            for frame, time in zip(frame_indices, times, strict=True)
        ]
        mark = {
            "id": f"{id_prefix}-tag-{index}",
            "event_id": event_id,
            "kind": "nametag",
            "start_seconds": start,
            "end_seconds": end,
            "points": keyframes[0]["points"],
            "keyframes": keyframes,
        }
        if tag.get("position"):
            mark["position"] = tag["position"]
        if tag.get("name"):
            mark["text"] = tag["name"]
        drawings.append(mark)

    if label is not None:
        if hold_at_seconds is not None and hold_at_seconds < start:
            raise FormationError("hold_at_seconds cannot be before the common start")
        label_from = start if hold_at_seconds is None else hold_at_seconds
        label_time = next((time for time in times if time >= label_from), None)
        if label_time is None:
            raise FormationError("hold_at_seconds is beyond the last common frame")
        label_drawing = {
            "id": f"{id_prefix}-label",
            "event_id": event_id,
            "kind": "label",
            "start_seconds": label_time,
            "end_seconds": end,
            "points": [[0.03, 0.05]],
            "color": "#FFFFFF",
            "text": label,
        }
        if hold_seconds is not None:
            label_drawing["hold_seconds"] = hold_seconds
        drawings.append(label_drawing)

    interval = {
        "start_seconds": start,
        "end_seconds": end,
        "frame_count": len(frame_indices),
    }
    target_status = any(result.get("status") != "complete" for result in results)
    interval_short = end < requested_end
    limited_by = (
        "possible_overlap_or_identity_swap"
        if conflict is not None
        else "target_status"
        if target_status or interval_short
        else "requested_window"
    )
    warnings = []
    if conflict is not None:
        first, second = conflict["targets"]
        warnings.append(
            f"Possible target overlap or identity swap at frame {conflict['frame_index']} "
            f"between targets {first} and {second}; formation stops before that frame."
        )
    if target_status:
        warnings.append("At least one tracking target needs reseeding.")
    if interval_short and conflict is None:
        warnings.append("Common tracking coverage ends before the requested window.")
    targets = [
        {
            "index": index,
            "seed_bbox": seeds[index],
            "status": result.get("status"),
            "loss_reason": result.get("loss_reason"),
            "valid_end_seconds": result.get("valid_end_seconds"),
        }
        for index, result in enumerate(results)
    ]
    event = {
        "event_id": event_id,
        "start_seconds": start,
        "end_seconds": end,
        "team": "Formation",
        "phase": "Tracking",
        "action": "Formation",
    }
    # Paint order: area under links, links under rings/spotlights, tags and label on top.
    layer = {"area": 0, "line": 1, "nametag": 3, "label": 4}
    drawings.sort(key=lambda mark: layer.get(mark["kind"], 2))
    try:
        rendering.validate_plan([event], drawings, media)
    except rendering.RenderError as exc:
        raise FormationError(str(exc)) from exc
    return {
        "drawings": drawings,
        "interval": interval,
        "suggested_event": {
            "event_id": event_id,
            "start_seconds": start,
            "end_seconds": end,
        },
        "targets": targets,
        "links": selected_links,
        "limited_by": limited_by,
        "conflict": conflict,
        "needs_reseed": target_status or conflict is not None or interval_short,
        "warnings": warnings,
    }
