"""Mix a narration track into a finished reel, ducking the match audio.

Standalone helper (also `python -m once_sport_mcp.voiceover`). It never
overwrites an existing file, keeps the video stream untouched (stream copy)
and verifies the result with ffprobe before publishing it.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class VoiceoverError(ValueError):
    pass


def _probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,channels,duration,nb_frames:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise VoiceoverError(f"ffprobe could not read {path}")
    data = json.loads(result.stdout)
    kinds = [stream.get("codec_type") for stream in data.get("streams", [])]
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    channels = next(
        (
            s.get("channels")
            for s in data.get("streams", [])
            if s.get("codec_type") == "audio"
        ),
        None,
    )
    return {
        "video": "video" in kinds,
        "audio": "audio" in kinds,
        "duration": duration,
        "channels": channels,
    }


def mix_voiceover(
    reel,
    narration,
    destination,
    *,
    offset_seconds=0.0,
    duck_db=12.0,
    narration_gain_db=0.0,
):
    """Write `destination` = reel video + (match audio ducked under narration)."""
    reel, narration, destination = Path(reel), Path(narration), Path(destination)
    for name, value, low, high in (
        ("offset_seconds", offset_seconds, 0.0, 3600.0),
        ("duck_db", duck_db, 0.0, 30.0),
        ("narration_gain_db", narration_gain_db, -20.0, 20.0),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise VoiceoverError(f"{name} must be a number in [{low}, {high}]")
    if destination.exists():
        raise VoiceoverError("Destination already exists")
    for path in (reel, narration):
        if not path.is_file():
            raise VoiceoverError(f"{path} is not a file")
    reel_info, narration_info = _probe(reel), _probe(narration)
    if not reel_info["video"]:
        raise VoiceoverError("The reel has no video stream")
    if not narration_info["audio"]:
        raise VoiceoverError("The narration file has no audio stream")
    total = reel_info["duration"]
    if offset_seconds >= total:
        raise VoiceoverError("offset_seconds is beyond the end of the reel")
    delay_ms = round(offset_seconds * 1000)
    speech_start = offset_seconds
    speech_end = min(total, offset_seconds + narration_info["duration"])
    ramp = 0.15
    # Match audio gain: -duck_db while the narration plays, with smooth ramps
    # (no clicks). Exact and predictable, unlike a level-keyed compressor.
    gain = 10 ** (-duck_db / 20)
    envelope = (
        f"1-(1-{gain:.6f})*clip(min((t-{speech_start}+{ramp})/{ramp},"
        f"({speech_end}+{ramp}-t)/{ramp}),0,1)"
    )
    # Keep the match audio's channel layout so mixing adds no level change.
    layout = "mono" if reel_info["channels"] == 1 else "stereo"
    narration_chain = (
        f"[1:a]aformat=sample_rates=48000:channel_layouts={layout},"
        f"adelay={delay_ms}|{delay_ms},volume={narration_gain_db}dB,"
        f"apad,atrim=0:{total}"
    )
    if reel_info["audio"]:
        graph = (
            narration_chain
            + f"[nar];[0:a]aformat=sample_rates=48000:channel_layouts={layout},"
            f"volume='{envelope}':eval=frame[ducked];"
            "[ducked][nar]amix=inputs=2:duration=first:normalize=0[out]"
        )
    else:
        graph = narration_chain + "[out]"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="once-voiceover-", dir=destination.parent
    ) as work:
        staged = Path(work) / "mixed.mp4"
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(reel),
            "-i",
            str(narration),
            "-filter_complex",
            graph,
            "-map",
            "0:v:0",
            "-map",
            "[out]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{total}",
            str(staged),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not staged.is_file():
            raise VoiceoverError("ffmpeg failed: " + result.stderr.strip()[-300:])
        check = _probe(staged)
        if not check["video"] or not check["audio"]:
            raise VoiceoverError("Mixed file is missing a video or audio stream")
        if abs(check["duration"] - total) > 0.1:
            raise VoiceoverError("Mixed duration differs from the reel")
        try:
            os.link(staged, destination)
        except FileExistsError:
            raise VoiceoverError("Destination already exists") from None
        except OSError:
            shutil.copyfile(staged, destination)
    return {
        "destination": str(destination),
        "duration_seconds": check["duration"],
        "offset_seconds": offset_seconds,
        "duck_db": duck_db,
        "had_match_audio": reel_info["audio"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reel")
    parser.add_argument("narration")
    parser.add_argument("destination")
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--duck-db", type=float, default=12.0)
    parser.add_argument("--gain-db", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        result = mix_voiceover(
            args.reel,
            args.narration,
            args.destination,
            offset_seconds=args.offset,
            duck_db=args.duck_db,
            narration_gain_db=args.gain_db,
        )
    except VoiceoverError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
