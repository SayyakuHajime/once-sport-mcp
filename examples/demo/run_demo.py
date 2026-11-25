"""Track four players, build a formation and render the README demo. Usage: python run_demo.py OUTDIR (after make_synthetic_pitch.py)"""

import json
import math
import sys
from pathlib import Path

from once_sport_mcp import formation, rendering, tracking

D = Path(sys.argv[1])
src = D / "synthetic-pitch.mp4"
W, H = 1280, 720


def pos(i, t):
    base = [(520, 250), (430, 380), (430, 510), (520, 620)][i]
    return base[0] + 40 * math.sin(t * 0.5 + i) + t * 12, base[1] + 10 * math.sin(
        t * 0.9 + i * 2
    )


media = rendering.probe_media(src)
results = []
for i in range(4):
    x, y = pos(i, 1.0)
    bbox = [(x - 14) / W, (y - 48) / H, 28 / W, 78 / H]
    out = D / f"track-{i}"
    out.mkdir(exist_ok=True)
    results.append(tracking.track_target(src, 1.0, 5.5, bbox, out))
print([(r["status"], r["frame_count"]) for r in results])
built = formation.build_formation(
    results,
    media=media,
    event_id="defending-shape",
    rings=False,
    spotlights=True,
    area=True,
    links=[[0, 1], [1, 2], [2, 3]],
    label="Defensive line: time stop",
    hold_at_seconds=3.0,
    hold_seconds=2.0,
    tags=[
        {"position": "LB", "name": "2"},
        {"position": "LCB", "name": "4"},
        {"position": "RCB", "name": "5"},
        {"position": "RB", "name": "3"},
    ],
)
print(built["limited_by"], built["needs_reseed"], built["interval"])
s = built["suggested_event"]
e1 = {
    "event_id": "defending-shape",
    "period": 1,
    "start_seconds": s["start_seconds"],
    "end_seconds": s["end_seconds"],
    "team": "Blue",
    "phase": "defending",
    "action": "Back four holds a line",
}
e2 = {
    "event_id": "attack-run",
    "period": 1,
    "start_seconds": 6.0,
    "end_seconds": 11.5,
    "team": "Red",
    "phase": "attacking",
    "action": "Run, switch and shot",
}
d = built["drawings"] + [
    {
        "id": "run",
        "event_id": "attack-run",
        "kind": "polyline",
        "start_seconds": 6.0,
        "end_seconds": 11.5,
        "points": [[0.62, 0.34], [0.54, 0.4], [0.46, 0.34], [0.36, 0.42]],
        "arrow": True,
        "smooth": True,
        "color": "#FFD166",
    },
    {
        "id": "offball",
        "event_id": "attack-run",
        "kind": "arrow",
        "start_seconds": 6.0,
        "end_seconds": 11.5,
        "points": [[0.66, 0.2], [0.5, 0.14]],
        "dash": True,
        "color": "#FF8FA3",
    },
    {
        "id": "switch",
        "event_id": "attack-run",
        "kind": "curve",
        "start_seconds": 6.0,
        "end_seconds": 11.5,
        "points": [[0.6, 0.78], [0.45, 0.62], [0.3, 0.7]],
        "color": "#6DE0D4",
    },
    {
        "id": "clock",
        "event_id": "attack-run",
        "kind": "stopwatch",
        "start_seconds": 6.0,
        "end_seconds": 11.5,
        "points": [[0.82, 0.08]],
        "text": "T+",
    },
    {
        "id": "slow",
        "event_id": "attack-run",
        "kind": "speed",
        "start_seconds": 8.0,
        "end_seconds": 10.0,
        "points": [],
        "speed": 0.5,
    },
]
out = D / "demo.mp4"
for f in (out, out.with_suffix(".drawings.json")):
    f.unlink(missing_ok=True)
m = rendering.render_reel(src, [e1, e2], d, out, fps=30)
print(
    m["output"]["frame_count"], [(c["event_id"], c["frame_count"]) for c in m["clips"]]
)
info = {
    "hold": m["clips"][0]["hold"],
    "speed": m["clips"][1]["speed"],
    "clips": m["clips"],
}
(D / "demo-info.json").write_text(json.dumps(info, indent=1))
