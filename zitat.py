#!/usr/bin/env python3
"""zitat — YouTube clip Korean subtitle pipeline."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile


def load_dotenv():
    """Load .env file from the same directory as this script."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def run(cmd, desc, capture=False, env=None):
    """Run a subprocess command with error handling."""
    print(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=capture,
            check=True,
            env=env,
        )
        return result
    except FileNotFoundError:
        print(f"  ERROR: '{cmd[0]}' not found. Is it installed?", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"  ERROR: {desc} failed (exit {e.returncode})", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(1)


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


def extract_srt(text):
    """Extract SRT content from Claude's response."""
    # Already valid SRT (starts with entry number + timestamp)
    if re.match(r'\s*1\s*\n\d{2}:\d{2}:', text.strip()):
        return text.strip()

    # Inside code fence
    m = re.search(r'```(?:srt)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Find first SRT entry pattern
    m = re.search(r'(1\n\d{2}:\d{2}:.*)', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    return text.strip()


def escape_srt_path(path):
    """Escape path for ffmpeg subtitles filter (libass)."""
    # libass requires escaping these characters
    path = path.replace("\\", "\\\\")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


def parse_time(t):
    """Parse time string (seconds, MM:SS, or HH:MM:SS) to float seconds."""
    try:
        return float(t)
    except ValueError:
        pass
    parts = t.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    raise ValueError(f"Cannot parse time: {t}")


def step_download(url, tmpdir, start="0", duration=None):
    """Step 1: Download video from YouTube (with optional section cut)."""
    needs_clip = start != "0" or duration is not None
    print("[1/6] Downloading video segment..." if needs_clip else "[1/6] Downloading video...")
    output = os.path.join(tmpdir, "source.mp4")
    cmd = [
        "yt-dlp",
        "-f", "bv[width<=1024]+ba/b[width<=1024]",
        "--merge-output-format", "mp4",
    ]
    if needs_clip:
        start_sec = parse_time(start)
        if duration is not None:
            end_sec = start_sec + parse_time(duration)
        else:
            end_sec = None
        section = f"*{start_sec}-{end_sec}" if end_sec is not None else f"*{start_sec}-inf"
        cmd += ["--download-sections", section, "--force-keyframes-at-cuts"]
    cmd += ["-o", output, url]
    run(cmd, "download")
    return output


def step_local(path, tmpdir, start="0", duration=None):
    """Step 1 (local file): Use video as-is, or cut the requested section."""
    needs_clip = start != "0" or duration is not None
    if not needs_clip:
        print("[1/6] Using local video file")
        return path
    print("[1/6] Cutting local video segment...")
    output = os.path.join(tmpdir, "source.mp4")
    cmd = ["ffmpeg", "-y", "-ss", str(parse_time(start)), "-i", path]
    if duration is not None:
        cmd += ["-t", str(parse_time(duration))]
    # Re-encode for frame-accurate cuts (mirrors --force-keyframes-at-cuts)
    cmd += ["-c:v", "libx264", "-c:a", "aac", output]
    run(cmd, "cut")
    return output


def step_audio(clip, tmpdir):
    """Step 2: Extract audio."""
    print("[2/6] Extracting audio...")
    output = os.path.join(tmpdir, "audio.wav")
    run([
        "ffmpeg", "-y",
        "-i", clip,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        output,
    ], "audio extraction")
    return output


def step_whisper(audio, tmpdir, whisper_bin, whisper_model):
    """Step 3: Generate subtitles with whisper."""
    print("[3/6] Transcribing audio...")
    output_stem = os.path.join(tmpdir, "audio")
    run([
        whisper_bin,
        "-m", whisper_model,
        "-osrt",
        "-of", output_stem,
        audio,
    ], "transcription")
    return output_stem + ".srt"


def step_translate(srt_path, lang, tmpdir):
    """Step 4: Translate subtitles using claude CLI."""
    print("[4/6] Translating subtitles...")
    with open(srt_path, "r") as f:
        srt_content = f.read()

    prompt = (
        f"다음 SRT 자막을 자연스러운 {lang}(으)로 번역해. "
        "SRT 포맷과 타임코드는 그대로 유지하고 텍스트만 번역해. "
        "SRT 내용만 출력하고 다른 설명은 붙이지 마.\n\n"
        f"{srt_content}"
    )

    # Filter out CLAUDE_CODE_ENTRYPOINT to avoid nested execution issues
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}

    result = run(
        ["claude", "-p", prompt],
        "translation",
        capture=True,
        env=env,
    )

    translated = extract_srt(result.stdout)
    output = os.path.join(tmpdir, "translated.srt")
    with open(output, "w") as f:
        f.write(translated)
    print(f"  Translated SRT written to {output}")
    return output


def step_review(srt_path):
    """Step 5: Open translated subtitles for human review."""
    print("[5/6] Reviewing subtitles...")
    editor = os.environ.get("EDITOR", "vim")
    print(f"  $ {editor} {srt_path}")
    subprocess.run([editor, srt_path])


def step_burn(clip, srt_path, output_path, font, font_size):
    """Step 6: Burn subtitles into video."""
    print("[6/6] Burning subtitles into video...")
    escaped = escape_srt_path(srt_path)
    vf = f"subtitles={escaped}:force_style='FontName={font},FontSize={font_size}'"
    run([
        "ffmpeg", "-y",
        "-i", clip,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ], "subtitle burn")
    return output_path


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="zitat — YouTube clip Korean subtitle pipeline",
    )
    parser.add_argument("url", help="YouTube URL or local video file")
    parser.add_argument("-ss", "--start", default="0", help="Start time (ffmpeg format)")
    parser.add_argument("-t", "--duration", default=None, help="Duration (seconds or ffmpeg format)")
    parser.add_argument("-o", "--output", default=None, help="Output filename (without .mp4)")
    parser.add_argument("--lang", default="Korean", help="Target language (default: Korean)")
    parser.add_argument("--font", default="BM Dohyeon", help="Subtitle font (default: BM Dohyeon)")
    parser.add_argument("--font-size", default="22", help="Subtitle font size (default: 22)")
    parser.add_argument("--whisper-bin", default=None, help="Path to whisper-cli (default: $WHISPER_BIN or 'whisper-cli')")
    parser.add_argument("--whisper-model", default=None, help="Path to whisper model (default: $WHISPER_MODEL)")
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
        srt = step_whisper(audio, tmpdir, whisper_bin, whisper_model)
        translated = step_translate(srt, args.lang, tmpdir)
        if not args.no_review:
            step_review(translated)
        else:
            print("[5/6] Skipping subtitle review")
        step_burn(source, translated, output_path, args.font, args.font_size)

        print(f"\nDone! Output: {output_path}")
    finally:
        if args.keep_tmp:
            print(f"Temp files kept at: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
