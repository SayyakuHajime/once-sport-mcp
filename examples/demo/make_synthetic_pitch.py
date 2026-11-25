"""Generate a small synthetic pitch video (own content, no footage). Usage: python make_synthetic_pitch.py OUTDIR"""

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

W, H, FPS, DUR = 1280, 720, 30, 12
if len(sys.argv) != 2:
    sys.exit("usage: python make_synthetic_pitch.py OUTDIR")
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
src = OUT / "synthetic-pitch.mp4"


def pos(i, t):
    # four blue defenders (line) and four red attackers; slow lateral drift
    if i < 4:
        base = [(520, 250), (430, 380), (430, 510), (520, 620)][i]
        return base[0] + 40 * math.sin(t * 0.5 + i) + t * 12, base[1] + 10 * math.sin(
            t * 0.9 + i * 2
        )
    j = i - 4
    base = [(700, 230), (720, 350), (690, 470), (740, 590)][j]
    return base[0] - t * 18 + 25 * math.sin(t * 0.7 + j), base[1] + 12 * math.cos(
        t * 0.6 + j
    )


def frame(n):
    t = n / FPS
    im = Image.new("RGB", (W, H), (46, 125, 50))
    d = ImageDraw.Draw(im)
    for k in range(0, W, 160):
        d.rectangle((k, 0, k + 80, H), fill=(52, 136, 56))
    d.rectangle((40, 40, W - 40, H - 40), outline=(235, 235, 235), width=3)
    d.line((W // 2, 40, W // 2, H - 40), fill=(235, 235, 235), width=3)
    d.ellipse(
        (W // 2 - 90, H // 2 - 90, W // 2 + 90, H // 2 + 90),
        outline=(235, 235, 235),
        width=3,
    )
    d.rectangle((40, 220, 230, 500), outline=(235, 235, 235), width=3)
    d.rectangle((W - 230, 220, W - 40, 500), outline=(235, 235, 235), width=3)
    for i in range(8):
        x, y = pos(i, t)
        col = (30, 90, 225) if i < 4 else (215, 45, 45)
        d.ellipse(
            (x - 13, y - 48, x + 13, y - 22), fill=(238, 200, 160), outline=(20, 20, 20)
        )
        d.rectangle((x - 14, y - 22, x + 14, y + 8), fill=col, outline=(255, 255, 255))
        d.rectangle(
            (x - 10, y + 8, x + 10, y + 30),
            fill=(240, 240, 240) if i < 4 else (30, 30, 30),
            outline=(20, 20, 20),
        )
    bx, by = pos(4, t)
    d.ellipse(
        (bx - 60 - 7, by + 20 - 7, bx - 60 + 7, by + 20 + 7),
        fill=(255, 255, 255),
        outline=(0, 0, 0),
    )
    return im


# noise "crowd" with a swell so slow motion / limiter are audible
sr = 48000
rng = np.random.default_rng(7)
n = sr * DUR
x = rng.standard_normal(n)
env = (
    0.04
    + 0.02 * np.sin(np.linspace(0, 12, n))
    + 0.25 * np.exp(-((np.linspace(0, DUR, n) - 9.5) ** 2) / 0.8)
)
audio = (np.convolve(x, np.ones(30) / 30, mode="same") * env * 6).clip(-1, 1)
wav = OUT / "crowd.wav"
import wave

with wave.open(str(wav), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes((audio * 32767).astype("<i2").tobytes())
p = subprocess.Popen(
    [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-i",
        str(wav),
        "-c:v",
        "libx264",
        "-crf",
        "14",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(src),
    ],
    stdin=subprocess.PIPE,
)
for n in range(FPS * DUR):
    p.stdin.write(frame(n).tobytes())
p.stdin.close()
if p.wait() != 0:
    sys.exit("ffmpeg failed")
wav.unlink(missing_ok=True)
print("ok", src)
