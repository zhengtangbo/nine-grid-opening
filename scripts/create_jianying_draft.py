"""Build a layered Jianying project with its native 九宫格 entrance."""

import argparse
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid


US = 1_000_000
SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO = SKILL_ROOT / "assets" / "opening-audio.wav"
DEFAULT_AUDIO_TITLE = "九宫格节拍"
DEFAULT_HOLD = 1.5
FADE_IN_US = 500_000
FADE_OUT_US = 1_000_000


def default_drafts_root():
    relative = Path("JianyingPro", "User Data", "Projects", "com.lveditor.draft")
    standard = Path.home() / "AppData" / "Local" / relative
    local_data = os.getenv("LOCALAPPDATA")
    candidates = [standard]
    if local_data:
        candidates.insert(0, Path(local_data) / relative)
    return next((path for path in candidates if path.is_dir()), standard)


def prepare_video(source, output, width, height, fps, playing_frames, total_frames):
    """Create a canvas-sized input, preserving the last selected frame when needed."""
    start = float(source["selected"]["start"]) + float(source.get("video_offset", 0))
    hdr = source.get("color_transfer") in ("smpte2084", "arib-std-b67")
    tonemap = (
        "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
        "tonemap=mobius:desat=2,zscale=t=bt709:m=bt709:r=limited,"
        if hdr else ""
    )
    filtergraph = (
        f"{tonemap}scale={width}:{height}:force_original_aspect_ratio=increase:"
        "force_divisible_by=2:reset_sar=1:flags=lanczos,"
        f"crop={width}:{height},fps={fps},trim=end_frame={playing_frames},"
        f"tpad=stop_mode=clone:stop={total_frames},format=yuv420p"
    )
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-threads", "2", "-filter_threads", "2", "-ss", f"{start:.6f}",
        "-i", source["path"], "-map", f"0:{source['stream_index']}", "-an",
        "-vf", filtergraph, "-frames:v", str(total_frames + 1), "-c:v", "libx264",
        "-preset", "fast", "-crf", "16", "-threads", "4", "-movflags", "+faststart",
        "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
        str(output),
    ], check=True)


def closing_playable_seconds(source):
    """Find the first cut after the chosen window, allowing for sampling error."""
    start = float(source["selected"]["start"])
    end = start + float(source["selected"]["duration"])
    cut = next((t for t in source.get("cut_times", []) if t >= end), math.inf)
    return max(end, min(float(source["duration"]), cut - 0.1)) - start


def order_sources(sources, closing_input, animation, hold):
    if len(sources) == 1:
        return sources * 4, 1

    def closing_rank(source):
        selected = source["selected"]
        enough_motion = (float(selected["duration"]) + 0.001 >= animation and
                         closing_playable_seconds(source) + 0.001 >= animation + hold)
        return enough_motion, float(selected.get("score", 0))

    if closing_input is not None:
        index = closing_input - 1
        return sources[:index] + sources[index + 1:] + [sources[index]], closing_input

    finalists = []
    for index, source in enumerate(sources):
        ranked = source.get("candidates") or []
        finalists.append((index, source))
        if ranked and source["selected"] == ranked[0]:
            finalists.extend((index, {**source, "selected": candidate}) for candidate in ranked[1:])
    index, finisher = max(finalists, key=lambda option: closing_rank(option[1]))
    return sources[:index] + sources[index + 1:] + [finisher], index + 1


def can_use_original(source, path, width, height, length):
    return (path.suffix.lower() in (".mp4", ".mov", ".m4v")
            and source.get("stream_index") == 0 and abs(float(source.get("video_offset", 0))) < 0.001
            and not float(source.get("rotation", 0)) and source.get("sample_aspect_ratio") in (None, "1:1")
            and source.get("color_transfer") not in ("smpte2084", "arib-std-b67")
            and abs(float(source["width"]) / float(source["height"]) - width / height) < 0.002
            and float(source["duration"]) - float(source["selected"]["start"]) - 0.003 >= length)


def write_metadata(folder, destination, entrance, cache, width, height, duration):
    """Add the editor's known resource path and the new draft's identity."""
    content_file = folder / "draft_content.json"
    data = json.loads(content_file.read_text(encoding="utf-8"))
    project_id = str(uuid.uuid4()).upper()
    data["id"] = project_id
    data["name"] = destination.name
    animations = data["materials"]["material_animations"]
    for item in animations:
        for animation in item["animations"]:
            if cache and animation["resource_id"] == entrance.resource_id:
                animation["path"] = cache.as_posix()
    meta_file = folder / "draft_meta_info.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    now = time.time_ns() // 1000
    meta.update(draft_id=project_id, draft_name=destination.name,
                draft_fold_path=destination.as_posix(), draft_root_path=destination.parent.as_posix(),
                tm_draft_create=now, tm_draft_modified=now, tm_duration=duration)
    video_tracks = [item for item in data["tracks"] if item["type"] == "video"]
    if (len(video_tracks) != len(animations) or data["duration"] != duration or
            (data["canvas_config"]["width"], data["canvas_config"]["height"]) != (width, height)):
        raise RuntimeError("剪映草稿的轨道、时长或画布不符合开场计划。")
    if any(abs(video["width"] / video["height"] - width / height) > 0.002
           for video in data["materials"]["videos"]):
        raise RuntimeError("剪映草稿的片段比例不正确。")
    folder = folder.resolve()
    for material_type in ("videos", "audios"):
        for item in data["materials"][material_type]:
            path = Path(item["path"]).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise RuntimeError(f"剪映草稿的素材链接不正确：{path}")
            item["path"] = (destination / path.relative_to(folder)).as_posix()
    for track, item in zip(video_tracks, animations):
        segment, = track["segments"]
        if item["id"] not in segment["extra_material_refs"]:
            raise RuntimeError("入场动画与对应片段未关联。")
    content = json.dumps(data, ensure_ascii=False, indent=2)
    content_file.write_text(content, encoding="utf-8")
    (folder / "draft_info.json").write_text(content, encoding="utf-8")
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="select_highlights.py output JSON")
    parser.add_argument("--name", required=True, help="new draft name")
    parser.add_argument("--drafts-root", type=Path, help="override the active draft folder")
    parser.add_argument("--size", help="e.g. 1080x1920")
    parser.add_argument("--fps", type=int, help="project frame rate, an integer")
    parser.add_argument("--stagger", type=float, default=0.20)
    parser.add_argument("--animation", type=float, default=2.00)
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD)
    parser.add_argument("--closing-input", type=int, help="1-based index in the input plan")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--audio", "--sfx", type=Path, help="use a supplied opening audio file")
    audio.add_argument("--no-audio", "--no-sfx", action="store_true", help="create a silent draft")
    parser.add_argument("--audio-gain", type=float, default=1.0, help="opening audio linear gain")
    args = parser.parse_args()
    if not args.name or re.search(r'[<>:"/\\|?*\x00-\x1f]|\.{2,}', args.name) or args.name != args.name.strip(" ."):
        parser.error("--name must be a simple project name")
    if not all(math.isfinite(v) and v > 0 for v in (args.stagger, args.animation, args.hold)):
        parser.error("timings must be finite and positive")
    if not math.isfinite(args.audio_gain) or args.audio_gain < 0:
        parser.error("--audio-gain must be finite and nonnegative")
    audio_path = None if args.no_audio else (args.audio or DEFAULT_AUDIO).resolve()
    if audio_path and not audio_path.is_file():
        parser.error("opening audio not found; pass --audio with an existing file or --no-audio")

    sources = json.loads(args.plan.read_text(encoding="utf-8-sig"))["sources"]
    if not sources:
        parser.error("plan needs at least one source")
    source = sources[0]
    original_dims = (source.get("display_width") or source["width"],
                     source.get("display_height") or source["height"])
    scale = min(1, 1920 / max(original_dims), 1080 / min(original_dims))
    width, height = (max(2, 2 * round(dim * scale / 2)) for dim in original_dims)
    if args.size:
        if not re.fullmatch(r"\d+x\d+", args.size):
            parser.error("--size must be WxH")
        width, height = map(int, args.size.split("x"))
    if min(width, height) < 2 or width % 2 or height % 2:
        parser.error("canvas dimensions must be positive even integers")
    fps = args.fps if args.fps is not None else min(round(Fraction(source.get("fps") or 30)), 60)
    if fps <= 0:
        parser.error("fps must be positive")

    for source in sources:
        start = float(source["selected"]["start"])
        duration = float(source["selected"]["duration"])
        if not Path(source["path"]).is_file():
            parser.error(f"missing source {source['path']}")
        if not (0 <= start < float(source["duration"]) and
                0 < duration <= float(source["duration"]) - start + 0.001):
            parser.error(f"invalid source range {source['path']}")
    if args.closing_input is not None and not 1 <= args.closing_input <= len(sources):
        parser.error("--closing-input is outside the input list")
    sources, closing_input = order_sources(sources, args.closing_input, args.animation, args.hold)

    try:
        import pyJianYingDraft as draft
    except ImportError as exc:
        parser.exit(1, f"剪映草稿依赖不可用，请安装 requirements.txt：{exc}\n")
    app_root = default_drafts_root().resolve()
    root = (args.drafts_root or app_root).resolve()
    destination = root / args.name
    if destination.exists():
        parser.error("choose a new draft name")
    if args.drafts_root is None and not root.is_dir():
        parser.error("请先启动剪映或用 --drafts-root 指定剪映设置中的草稿位置")
    root.mkdir(parents=True, exist_ok=True)
    native = draft.IntroType.九宫格
    resource = native.value
    caches = [base.parent.parent / "Cache" / "effect" / asset_id / resource.md5
              for base in (app_root, root) for asset_id in (resource.resource_id, resource.effect_id)]
    cache = next((path for path in caches if (path / "config.json").is_file()), None)
    starts = [round(i * args.stagger * fps) for i in range(len(sources))]
    animation = max(1, round(args.animation * fps))
    tail = max(1, round(args.hold * fps))
    total = starts[-1] + animation + tail
    micro = lambda frames: round(frames * US / fps)

    with tempfile.TemporaryDirectory(prefix=".nine-grid-", dir=root) as scratch:
        scratch_dir = Path(scratch)
        clips = {}
        edit_clips = []
        for index, (source, offset) in enumerate(zip(sources, starts)):
            is_last = index == len(sources) - 1
            length = animation + (tail if is_last else starts[index + 1] - offset)
            taken_at = float(source["selected"]["start"])
            source_path = Path(source["path"])
            moving_seconds = float(source["selected"]["duration"])
            if is_last and round(moving_seconds * fps) >= animation:
                moving_seconds = min(moving_seconds + tail / fps, closing_playable_seconds(source))
            moving_seconds = min(moving_seconds, float(source["duration"]) - taken_at)
            n_frames = min(length, max(1, round(moving_seconds * fps)))
            if n_frames == length and can_use_original(source, source_path, width, height, length / fps):
                edit_clips.append((source_path, round(taken_at * US), offset, length))
                continue
            key = (source["path"], taken_at, n_frames, length)
            if key not in clips:
                path = scratch_dir / f"{index + 1:02d}_{Path(source['path']).stem}.mp4"
                prepare_video(source, path, width, height, fps, n_frames, length)
                clips[key] = path
            edit_clips.append((clips[key], 0, offset, length))

        script = draft.DraftFolder(scratch).create_draft(args.name, width, height, fps)
        folder = scratch_dir / args.name
        material_folder = folder / "materials"
        material_folder.mkdir()
        materials = {}
        for index, (path, source_start, offset, length) in enumerate(edit_clips):
            track = script.append_track(draft.TrackSpec(draft.TrackType.video, f"九宫格 {index + 1:02d}"))
            if path not in materials:
                saved = material_folder / f"{index + 1:02d}_{path.name}"
                shutil.copy2(path, saved)
                materials[path] = draft.VideoMaterial(str(saved))
                materials[path].local_material_id = saved.stem
            length_us = micro(offset + length) - micro(offset)
            segment = draft.VideoSegment(materials[path], draft.trange(micro(offset), length_us),
                                         source_timerange=draft.trange(source_start, length_us), volume=0)
            segment.add_animation(native, micro(animation))
            script.add_segment(segment, track)
        audio_length = 0
        if audio_path:
            staged_audio = material_folder / audio_path.name
            shutil.copy2(audio_path, staged_audio)
            audio_material = draft.AudioMaterial(str(staged_audio),
                                                 None if args.audio else DEFAULT_AUDIO_TITLE)
            audio_length = min(micro(total), audio_material.duration)
            fade_in = min(FADE_IN_US, audio_length)
            fade_out = min(FADE_OUT_US, audio_length - fade_in)
            audio_track = script.append_track(draft.TrackSpec(draft.TrackType.audio, "开场音轨"))
            segment = draft.AudioSegment(audio_material, draft.trange(0, audio_length),
                                         volume=args.audio_gain)
            segment.add_fade(fade_in, fade_out)
            script.add_segment(segment, audio_track)
        script.save()
        write_metadata(folder, destination, resource, cache, width, height, micro(total))
        folder.rename(destination)

    print(json.dumps({"draft": str(destination), "size": [width, height], "fps": fps,
                      "source_layers": len(starts), "duration": total / fps,
                      "layer_starts": [i / fps for i in starts], "animation": animation / fps,
                      "closing_input": closing_input, "fullscreen_at": (starts[-1] + animation) / fps,
                      "audio_length": audio_length / US,
                      "native_asset_cached": str(cache) if cache else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
