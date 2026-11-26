"""Narration mixing with match-audio ducking."""

import array
import subprocess

import pytest

from once_sport_mcp.voiceover import VoiceoverError, mix_voiceover


def ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)],
        check=True,
        capture_output=True,
    ).stdout


def rms(samples):
    return (sum(x * x for x in samples) / max(1, len(samples))) ** 0.5


@pytest.fixture
def reel(tmp_path):
    path = tmp_path / "reel.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=10:duration=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=300:sample_rate=48000:duration=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        path,
    )
    return path


@pytest.fixture
def narration(tmp_path):
    path = tmp_path / "voice.wav"
    ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=1.0", path
    )
    return path


def samples(path, start, end, low=None):
    raw = ffmpeg("-i", path, "-vn", "-ac", 1, "-ar", 48000, "-f", "s16le", "-")
    data = array.array("h", raw)
    return data[round(start * 48000) : round(end * 48000)]


def test_match_audio_is_ducked_during_speech_and_untouched_elsewhere(
    tmp_path, reel, narration
):
    out = tmp_path / "mixed.mp4"
    result = mix_voiceover(reel, narration, out, offset_seconds=1.0, duck_db=12)
    assert result["had_match_audio"]
    plain_before = rms(samples(reel, 0.3, 0.8))
    plain_after = rms(samples(reel, 3.0, 3.8))
    assert abs(rms(samples(out, 0.3, 0.8)) - plain_before) < 0.15 * plain_before
    assert abs(rms(samples(out, 3.0, 3.8)) - plain_after) < 0.15 * plain_after
    # During speech the match tone (300 Hz) is ~12 dB lower; measure it with a
    # narrow filter by comparing against a mix made without ducking.
    flat = tmp_path / "flat.mp4"
    mix_voiceover(reel, narration, flat, offset_seconds=1.0, duck_db=0)
    ducked = tmp_path / "ducked-only.wav"
    ffmpeg("-i", out, "-vn", "-af", "bandpass=f=300:width_type=h:w=60", ducked)
    flat_wav = tmp_path / "flat-only.wav"
    ffmpeg("-i", flat, "-vn", "-af", "bandpass=f=300:width_type=h:w=60", flat_wav)
    ratio = rms(samples(ducked, 1.2, 1.9)) / rms(samples(flat_wav, 1.2, 1.9))
    assert 0.2 < ratio < 0.34  # 12 dB is a factor of about 0.25
    assert rms(samples(out, 1.2, 1.9)) > 0.5 * plain_before  # narration audible


def test_video_stream_is_copied_and_duration_matches(tmp_path, reel, narration):
    out = tmp_path / "mixed.mp4"
    mix_voiceover(reel, narration, out)
    count = lambda p: subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            str(p),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert count(out) == count(reel) == "40"


def test_silent_reel_gets_the_narration_only(tmp_path, narration):
    silent = tmp_path / "silent.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=10:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        silent,
    )
    out = tmp_path / "voiced.mp4"
    result = mix_voiceover(silent, narration, out, offset_seconds=0.5)
    assert result["had_match_audio"] is False
    assert rms(samples(out, 0.6, 1.4)) > 1000
    assert rms(samples(out, 2.0, 2.8)) < 50


def test_rejects_bad_inputs_and_never_overwrites(tmp_path, reel, narration):
    out = tmp_path / "mixed.mp4"
    mix_voiceover(reel, narration, out)
    with pytest.raises(VoiceoverError, match="already exists"):
        mix_voiceover(reel, narration, out)
    for kwargs in (
        {"offset_seconds": -1},
        {"offset_seconds": 99},
        {"duck_db": 99},
        {"duck_db": True},
    ):
        with pytest.raises(VoiceoverError):
            mix_voiceover(reel, narration, tmp_path / "x.mp4", **kwargs)
    with pytest.raises(VoiceoverError):
        mix_voiceover(tmp_path / "missing.mp4", narration, tmp_path / "y.mp4")
    with pytest.raises(VoiceoverError):
        mix_voiceover(reel, reel.with_suffix(".txt"), tmp_path / "z.mp4")
