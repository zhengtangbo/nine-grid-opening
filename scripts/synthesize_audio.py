"""Rebuild the bundled percussion and synth cue without external samples."""

from array import array
import math
from pathlib import Path
import random
import sys
import wave


RATE = 44_100
DURATION = 20
BEAT = 60 / 124
OUTPUT = Path(__file__).resolve().parents[1] / "assets" / "opening-audio.wav"


def write_cue(output):
    count = RATE * DURATION
    channels = (array("f", [0]) * count, array("f", [0]) * count)
    rng = random.Random(52871)

    def add(start, length, sound, pan=0):
        left, right = channels
        start_frame = round(start * RATE)
        for i in range(min(round(length * RATE), count - start_frame)):
            value = sound(i / RATE)
            left[start_frame + i] += value * (1 - pan) * 0.74
            right[start_frame + i] += value * (1 + pan) * 0.74

    for beat in range(math.ceil(DURATION / BEAT)):
        start = beat * BEAT
        add(start, 0.16, lambda t: 0.39 * math.exp(-29 * t) *
            math.sin(math.tau * (48 * t + 3.2 * (1 - math.exp(-56 * t)))))
        if beat % 4 in (1, 3):
            pan = -0.15 if beat % 4 == 1 else 0.15
            add(start, 0.105, lambda t: math.exp(-47 * t) *
                (0.18 * rng.uniform(-1, 1) + 0.05 * math.sin(math.tau * 187 * t)), pan)
        for part in (0.25, 0.50, 0.75):
            level = 0.105 if part == 0.5 else 0.05
            add(start + part * BEAT, 0.052, lambda t: level * math.exp(-95 * t) *
                rng.uniform(-1, 1) * math.sin(math.tau * 4760 * t), 0.36 if part < 0.7 else -0.4)

        for delay, note in ((0.0, (36, 36, 39, 43, 41, 38, 34, 38)[beat % 8]),
                            (0.58, (48, 46, 51, 50, 53, 50, 46, 43)[beat % 8])):
            pitch = 440 * 2 ** ((note - 69) / 12)
            amp = 0.24 if delay == 0 else 0.12
            add(start + delay * BEAT, 0.20, lambda t: amp *
                min(1, t / 0.006) * math.exp(-12 * t) *
                (math.sin(math.tau * pitch * t) + 0.28 * math.sin(math.tau * pitch * 2 * t)),
                0 if delay == 0 else -0.22)

    samples = array("h")
    for i, (left, right) in enumerate(zip(*channels)):
        end = min(1, (count - 1 - i) / (0.04 * RATE))
        samples.extend(round(27_000 * math.tanh(value * 1.55) * end) for value in (left, right))
    if sys.byteorder != "little":
        samples.byteswap()
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(samples.tobytes())


if __name__ == "__main__":
    write_cue(OUTPUT)
