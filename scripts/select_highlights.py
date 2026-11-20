"""Rank short, continuous edit windows from local video, using only FFmpeg and Python."""

import argparse
from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import tempfile


SAMPLE_RATE = 5
TIMING_FILTER = (
    f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={1 / SAMPLE_RATE},"
    f"fps={SAMPLE_RATE}:round=near"
)


def number(value, fallback=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def ratio(value, fallback):
    try:
        result = Fraction(str(value).replace(":", "/"))
    except (ZeroDivisionError, ValueError):
        return fallback
    return result if result > 0 else fallback


def ffmpeg_error(result):
    return result.stderr.decode("utf-8", errors="replace").strip()


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(ffmpeg_error(result))
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next(
        (
            item for item in streams
            if item.get("codec_type") == "video"
            and not item.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video or not video.get("width") or not video.get("height"):
        raise ValueError("no playable video stream with known geometry")
    container = data.get("format", {})
    container_start = number(container.get("start_time"))
    video_start = number(video.get("start_time"), container_start)
    video_offset = video_start - container_start
    duration = number(video.get("duration"))
    if duration <= 0:
        duration = number(container.get("duration")) - max(0, video_offset)
    if duration <= 0:
        raise ValueError("no positive video duration")

    rotation = next(
        (number(item["rotation"]) for item in video.get("side_data_list", []) if "rotation" in item),
        number(video.get("tags", {}).get("rotate")),
    )
    aspect = ratio(video.get("sample_aspect_ratio"), Fraction(1))
    display_width = video["width"] * aspect
    display_height = video["height"]
    if round(rotation / 90) % 2:
        display_width, display_height = display_height, display_width

    return {
        "path": str(path),
        "stream_index": video["index"],
        "duration": duration,
        "video_offset": video_offset,
        "width": video["width"],
        "height": video["height"],
        "display_width": max(1, round(display_width)),
        "display_height": max(1, round(display_height)),
        "sample_aspect_ratio": f"{aspect.numerator}:{aspect.denominator}",
        "rotation": rotation,
        "color_transfer": video.get("color_transfer"),
        "fps": str(ratio(video.get("avg_frame_rate"), ratio(video.get("r_frame_rate"), Fraction(30)))),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
    }


def analysis_size(source, long_edge):
    width = source["display_width"]
    height = source["display_height"]
    factor = long_edge / max(width, height)
    return max(16, round(width * factor)), max(16, round(height * factor))


def raw_frames(source, filters, pixel_format, frame_bytes):
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", "3", "-filter_threads", "1", "-i", source["path"],
        "-map", f"0:{source['stream_index']}", "-an", "-sn", "-dn",
        "-vf", filters, "-fps_mode", "passthrough", "-threads", "1",
        "-pix_fmt", pixel_format, "-f", "rawvideo", "pipe:1",
    ]
    with tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr) as process:
            try:
                while frame := process.stdout.read(frame_bytes):
                    if len(frame) != frame_bytes:
                        raise RuntimeError("FFmpeg returned an incomplete analysis frame")
                    yield frame
                if process.wait():
                    stderr.seek(0)
                    error = stderr.read().decode("utf-8", errors="replace").strip()
                    raise RuntimeError(error or "FFmpeg could not decode the source")
            finally:
                if process.poll() is None:
                    process.kill()


def clamp(value, low=0.0, high=1.0):
    return max(low, min(value, high))


@dataclass
class Sample:
    quality: float
    detail: float
    contrast: float
    clipped: float
    motion: float = 0.0
    dx: int = 0
    dy: int = 0
    shift_known: bool = False
    cut: bool = False


def frame_quality(frame, width, height):
    histogram = [0] * 16
    for value in frame:
        histogram[value >> 4] += 1
    size = len(frame)
    cumulative = 0
    quantiles = []
    for bucket, count in enumerate(histogram):
        cumulative += count
        while len(quantiles) < 2 and cumulative >= size * (0.10, 0.90)[len(quantiles)]:
            quantiles.append(bucket * 16 + 8)
    contrast = clamp((quantiles[1] - quantiles[0]) / 112)
    clipped = (histogram[0] + histogram[-1]) / size
    mean = sum(frame) / size
    exposure = clamp(1 - max(0, 48 - mean, mean - 207) / 44)
    exposure *= 1 - 0.8 * clamp((clipped - 0.10) / 0.82)

    # Broad luma texture is reliable on a small image. It cannot identify the subject.
    edge_sum = 0
    center_edge_sum = 0
    edge_count = 0
    center_count = 0
    left, right = width / 5, 4 * width / 5
    top, bottom = height / 5, 4 * height / 5
    for y in range(1, height - 1, 2):
        for x in range(1, width - 1, 2):
            index = y * width + x
            edge = abs(frame[index - 1] + frame[index + 1] + frame[index - width]
                       + frame[index + width] - 4 * frame[index])
            edge_sum += min(96, edge)
            edge_count += 1
            if left <= x <= right and top <= y <= bottom:
                center_edge_sum += min(96, edge)
                center_count += 1
    detail = clamp(0.7 * (edge_sum / edge_count) / 24
                   + 0.3 * (center_edge_sum / center_count) / 26)
    quality = 0.45 * detail + 0.27 * contrast + 0.28 * exposure
    return Sample(quality, detail, contrast, clipped), histogram


def measure_change(sample, previous, frame, hist, last_hist, width, height, offsets):
    same = sum(abs(frame[index] - previous[index]) for index in offsets) / len(offsets)
    hist_change = sum(abs(a - b) for a, b in zip(hist, last_hist)) / (2 * len(frame))
    sample.cut = same > 47 or (same > 25 and hist_change > 0.35)
    sample.motion = same / 255
    if sample.cut or same < 1.5 or sample.detail < 0.08:
        return

    # Estimate camera translation on a sparse grid. Alternating displacement is
    # penalized below. Sustained one-way travel is common in useful camera moves.
    best = same
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            if dx == 0 and dy == 0:
                continue
            offset = dy * width + dx
            error = sum(abs(frame[index + offset] - previous[index]) for index in offsets)
            error /= len(offsets)
            if error < best:
                best, sample.dx, sample.dy = error, dx, dy
    sample.shift_known = same - best >= max(0.8, same * 0.10)


def analyze(source, target):
    width, height = analysis_size(source, 72)
    offsets = [y * width + x for y in range(3, height - 3, 4) for x in range(3, width - 3, 4)]
    # tpad + trim ensures one visual sample even for sub-frame-rate inputs.
    filters = (
        f"{TIMING_FILTER},trim=duration={max(source['duration'], 0.01):.9f},"
        f"scale={width}:{height}:flags=area:out_range=full,format=gray"
    )
    samples = []
    previous = None
    last_hist = None
    for frame in raw_frames(source, filters, "gray", width * height):
        sample, histogram = frame_quality(frame, width, height)
        if previous is not None:
            measure_change(sample, previous, frame, histogram, last_hist, width, height, offsets)
        samples.append(sample)
        previous, last_hist = frame, histogram
    if not samples:
        raise ValueError("decoded no analysis frames")

    duration = min(target, source["duration"])
    last_start = source["duration"] - duration
    starts = sorted({i / SAMPLE_RATE for i in range(int(last_start * SAMPLE_RATE) + 1)} | {last_start})
    times = [i / SAMPLE_RATE for i in range(len(samples))]
    cut_times = [time for time, sample in zip(times, samples) if sample.cut]
    source["cut_times"] = cut_times
    # A transition first detected at a sampled frame may lie up to half a
    # sample interval either side of that time after FPS rounding.
    cut_margin = 0.5 / SAMPLE_RATE
    windows = []
    for start in starts:
        first = min(bisect_left(times, start - 1e-8), len(samples) - 1)
        end = max(first + 1, bisect_left(times, start + duration - 1e-8))
        section = samples[first:end]
        motion_samples = samples[
            min(first + 1, len(samples)):min(end, len(samples))
        ]
        # Include the transition into the first sampled image only if the
        # preceding sample is before the requested start by less than one step.
        if first > 0 and times[first] - start > 1e-7:
            motion_samples = samples[first:first + 1] + motion_samples
        cuts = bisect_left(cut_times, start + duration + cut_margin - 1e-8)
        cuts -= bisect_left(cut_times, start - cut_margin + 1e-8)
        windows.append(score_window(start, duration, section, motion_samples, cuts))
    windows.sort(key=lambda item: (-item["score"], item["start"]))
    ranked = [windows[0]]
    # Give the review sheet distinct source intervals whenever the clip is long
    # enough. Relax spacing for short clips so it can still show alternatives.
    for spacing in (duration, 0.60 * duration):
        for item in windows:
            if all(abs(item["start"] - accepted["start"]) + 1e-8 >= spacing for accepted in ranked):
                ranked.append(item)
                if len(ranked) == 3:
                    return ranked, len(samples)
    return ranked, len(samples)


def score_window(start, duration, samples, change_samples, cuts):
    qualities = sorted(sample.quality for sample in samples)
    quality = 0.8 * median(qualities) + 0.2 * qualities[len(qualities) // 5]
    motion = median(sample.motion for sample in change_samples) if change_samples else 0
    reversal = 0
    for earlier, later in zip(change_samples, change_samples[1:]):
        if earlier.shift_known and later.shift_known:
            dot = earlier.dx * later.dx + earlier.dy * later.dy
            reversal += clamp(-dot / 4)
    shake = reversal / max(1, len(change_samples) - 1)
    activity = clamp(motion / 0.047)
    excess = clamp((motion - 0.09) / 0.11)
    score = 0.79 * quality + 0.21 * activity - 0.42 * cuts - 0.18 * shake - 0.24 * excess
    return {
        "start": round(start, 6),
        "duration": round(duration, 6),
        "score": round(score, 5),
        "metrics": {
            "detail": round(median(sample.detail for sample in samples), 3),
            "contrast": round(median(sample.contrast for sample in samples), 3),
            "clipped": round(median(sample.clipped for sample in samples), 3),
            "motion": round(motion, 3),
            "shake": round(shake, 3),
            "cuts": cuts,
        },
    }


def make_sheet(source, candidates, sample_count, output):
    width, height = analysis_size(source, 360)
    rows = []
    for candidate in candidates:
        frame_ids = []
        for position in (0.12, 0.50, 0.86):
            time = candidate["start"] + position * candidate["duration"]
            frame_ids.append(min(sample_count - 1, round(time * SAMPLE_RATE)))
        rows.append(frame_ids)
    keys = sorted({key for row in rows for key in row})
    select = "+".join(f"eq(n\\,{index})" for index in keys)
    filters = (
        f"{TIMING_FILTER},select={select},"
        f"scale={width}:{height}:flags=area,setsar=1"
    )
    frames = dict(zip(keys, raw_frames(source, filters, "rgb24", width * height * 3), strict=True))
    gutter = 6
    canvas_width = 3 * width + 4 * gutter
    canvas_height = len(rows) * height + (len(rows) + 1) * gutter
    canvas = bytearray(b"\x20" * (canvas_width * canvas_height * 3))
    for row_index, row in enumerate(rows):
        for column, key in enumerate(row):
            frame = frames[key]
            x = gutter + column * (width + gutter)
            y = gutter + row_index * (height + gutter)
            for line in range(height):
                begin = ((line + y) * canvas_width + x) * 3
                canvas[begin:begin + width * 3] = frame[line * width * 3:(line + 1) * width * 3]
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo",
         "-pixel_format", "rgb24", "-video_size", f"{canvas_width}x{canvas_height}",
         "-i", "pipe:0", "-frames:v", "1", "-q:v", "3", str(output)],
        input=canvas, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(ffmpeg_error(result))
    return [[round(index / SAMPLE_RATE, 3) for index in row] for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="write a UTF-8 JSON edit plan")
    parser.add_argument("--seconds", type=float, default=2.0, help="target window length (default: 2)")
    parser.add_argument("--sheet-dir", type=Path, help="write labeled-by-row candidate contact sheets")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be positive and finite")

    sources = []
    for index, input_path in enumerate(args.inputs, start=1):
        path = input_path.resolve()
        try:
            source = probe(path)
            candidates, sample_count = analyze(source, args.seconds)
            source["selected"] = candidates[0].copy()
            source["candidates"] = candidates
            if args.sheet_dir:
                args.sheet_dir.mkdir(parents=True, exist_ok=True)
                sheet = (args.sheet_dir / f"source_{index:02d}.jpg").resolve()
                source["contact_sheet"] = str(sheet)
                source["contact_sheet_times"] = make_sheet(source, candidates, sample_count, sheet)
            sources.append(source)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(1, f"{path}: {exc}\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"seconds": args.seconds, "sources": sources}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    for source in sources:
        selected = source["selected"]
        print(f"{Path(source['path']).name}: {selected['start']:.3f} s + {selected['duration']:.3f} s")


if __name__ == "__main__":
    main()
