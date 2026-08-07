"""Argument parsing, configuration resolution, and pipeline wiring."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

from zitat.claude import claude_env, mark_texts
from zitat.steps import (
    build_style,
    step_audio,
    step_burn,
    step_download,
    step_local,
    step_review,
    step_split,
    step_translate,
    step_whisper,
)
from zitat.subs import format_srt
from zitat.util import env_int, env_str, load_dotenv
from zitat.words import DTW_PRESETS, build_source_cues, dtw_preset_for


def extract_video_id(url):
    """Extract YouTube video ID from URL."""
    patterns = [
        r'(?:youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:v=)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return "clip"


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="zitat",
        description="zitat — YouTube clip Korean subtitle pipeline",
    )
    parser.add_argument("url", help="YouTube URL or local video file")
    parser.add_argument("-ss", "--start", default="0", help="Start time (ffmpeg format)")
    parser.add_argument("-t", "--duration", default=None, help="Duration (seconds or ffmpeg format)")
    parser.add_argument("-o", "--output", default=None, help="Output filename (without .mp4)")
    parser.add_argument("--lang", default="Korean", help="Target language (default: Korean)")
    parser.add_argument("--font", default=None, help="Subtitle font (default: BM Dohyeon)")
    parser.add_argument("--font-size", type=int, default=None, help="Subtitle font size (default: 22)")
    parser.add_argument("--style", default=None, help="Extra libass force_style fields")
    parser.add_argument("--max-width", type=int, default=None,
                        help="Max display columns per cue, CJK counts 2 (default: 30)")
    parser.add_argument("--min-cue-ms", type=int, default=None,
                        help="Minimum cue duration in ms, 0 to disable (default: 800)")
    parser.add_argument("--pause-gap-ms", type=int, default=None,
                        help="Word gap that starts a new source cue (default: 400)")
    parser.add_argument("--max-cue-ms", type=int, default=None,
                        help="Maximum source cue length in ms (default: 5000)")
    parser.add_argument("--bridge-gap-ms", type=int, default=None,
                        help="Hold a cue across gaps up to this long, 0 to disable (default: 1500)")
    parser.add_argument("--translate-batch", type=int, default=None,
                        help="Cues per claude call, 0 to never batch (default: 80)")
    parser.add_argument("--whisper-bin", default=None, help="Path to whisper-cli (default: $WHISPER_BIN or 'whisper-cli')")
    parser.add_argument("--whisper-model", default=None, help="Path to whisper model (default: $WHISPER_MODEL)")
    parser.add_argument("--whisper-max-len", type=int, default=None,
                        help="whisper -ml segment cap, 0 to disable (default: 0)")
    parser.add_argument("--dtw", default=None, help="whisper DTW preset (default: derived from model name)")
    parser.add_argument("--no-dtw", action="store_true", help="Disable DTW word timestamps")
    parser.add_argument("--no-split", action="store_true", help="Skip cue splitting")
    parser.add_argument("--no-phrase-marks", action="store_true",
                        help="Split by width alone, without claude phrase marking")
    parser.add_argument("--no-review", action="store_true", help="Skip subtitle review step")
    parser.add_argument("--keep-tmp", action="store_true", help="Keep temporary files")

    args = parser.parse_args()

    whisper_bin = args.whisper_bin or os.environ.get("WHISPER_BIN", "whisper-cli")
    whisper_model = args.whisper_model or os.environ.get("WHISPER_MODEL")
    if not whisper_model:
        print("ERROR: whisper model path required. Set --whisper-model or $WHISPER_MODEL.", file=sys.stderr)
        sys.exit(1)
    whisper_bin = os.path.expanduser(whisper_bin)
    whisper_model = os.path.expanduser(whisper_model)

    # `or` would swallow a deliberate 0, so resolve these explicitly.
    max_width = args.max_width if args.max_width is not None else env_int("ZITAT_MAX_WIDTH", 30)
    min_cue_ms = args.min_cue_ms if args.min_cue_ms is not None else env_int("ZITAT_MIN_CUE_MS", 800)
    pause_gap_ms = args.pause_gap_ms if args.pause_gap_ms is not None else env_int("ZITAT_PAUSE_GAP_MS", 400)
    max_cue_ms = args.max_cue_ms if args.max_cue_ms is not None else env_int("ZITAT_MAX_CUE_MS", 5000)
    bridge_gap_ms = args.bridge_gap_ms if args.bridge_gap_ms is not None else env_int("ZITAT_BRIDGE_GAP_MS", 1500)
    translate_batch = args.translate_batch if args.translate_batch is not None else env_int("ZITAT_TRANSLATE_BATCH", 80)
    whisper_max_len = args.whisper_max_len if args.whisper_max_len is not None else env_int("ZITAT_WHISPER_MAX_LEN", 0)
    font = args.font if args.font is not None else env_str("ZITAT_FONT", "BM Dohyeon")
    font_size = args.font_size if args.font_size is not None else env_int("ZITAT_FONT_SIZE", 22)
    style_extra = args.style if args.style is not None else env_str("ZITAT_STYLE", "")

    if max_width < 1:
        print("ERROR: --max-width must be at least 1", file=sys.stderr)
        sys.exit(1)
    # force_style is comma-separated and sits inside single quotes in -vf.
    if "'" in font or "," in font:
        print("ERROR: --font must not contain ' or ,", file=sys.stderr)
        sys.exit(1)
    if "'" in style_extra:
        print("ERROR: --style must not contain '", file=sys.stderr)
        sys.exit(1)

    dtw = None
    if not args.no_dtw:
        requested = args.dtw or env_str("ZITAT_DTW", "")
        if requested:
            if requested in DTW_PRESETS:
                dtw = requested
            else:
                print(f"WARNING: unknown DTW preset {requested!r}; continuing without DTW",
                      file=sys.stderr)
        else:
            dtw = dtw_preset_for(whisper_model)
            if dtw is None:
                print(f"WARNING: no DTW preset matches {os.path.basename(whisper_model)}; "
                      "continuing without DTW", file=sys.stderr)

    local_path = os.path.abspath(os.path.expanduser(args.url))
    if not os.path.isfile(local_path):
        local_path = None

    if local_path:
        video_id = os.path.splitext(os.path.basename(local_path))[0]
    else:
        video_id = extract_video_id(args.url)
    output_name = args.output or f"{video_id}_ko"
    if not output_name.endswith(".mp4"):
        output_name += ".mp4"
    output_path = os.path.abspath(output_name)

    tmpdir = tempfile.mkdtemp(prefix="zitat_")
    print(f"Temp dir: {tmpdir}")

    try:
        if local_path:
            source = step_local(local_path, tmpdir, args.start, args.duration)
        else:
            source = step_download(args.url, tmpdir, args.start, args.duration)
        audio = step_audio(source, tmpdir)
        srt, srt_json = step_whisper(audio, tmpdir, whisper_bin, whisper_model,
                                     dtw, whisper_max_len)
        cues = build_source_cues(srt, srt_json, dtw is not None,
                                 pause_gap_ms, max_cue_ms, min_cue_ms)
        if not cues:
            print("ERROR: no speech detected", file=sys.stderr)
            sys.exit(1)

        cues, untranslated = step_translate(cues, args.lang, tmpdir, translate_batch,
                                            allow_partial=not args.no_review)

        if args.no_split:
            print("[5/7] Skipping cue splitting")
        else:
            marker = None
            if not args.no_phrase_marks:
                def marker(eligible):
                    # Placeholder cues still hold source-language text; a
                    # phrase-boundary prompt has nothing to say about them.
                    eligible = {i: t for i, t in eligible.items()
                                if i not in untranslated}
                    segments = mark_texts(eligible, claude_env(), translate_batch)
                    with open(os.path.join(tmpdir, "marks.json"), "w") as f:
                        json.dump(segments, f, ensure_ascii=False, indent=1)
                    return segments
            cues = step_split(cues, max_width, min_cue_ms, bridge_gap_ms, marker)

        final = os.path.join(tmpdir, "final.srt")
        with open(final, "w") as f:
            f.write(format_srt(cues))

        if not args.no_review:
            step_review(final)
        else:
            print("[6/7] Skipping subtitle review")

        step_burn(source, final, output_path, build_style(font, font_size, style_extra))

        print(f"\nDone! Output: {output_path}")
    finally:
        if args.keep_tmp:
            print(f"Temp files kept at: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
