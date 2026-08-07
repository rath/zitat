"""The seven pipeline steps and the burn style."""

import os
import subprocess
import sys

from zitat.claude import claude_env, translate_texts
from zitat.subs import (
    bridge_gaps,
    display_width,
    format_srt,
    format_ts,
    needs_marks,
    normalize_cues,
    parse_srt,
    split_cues,
)
from zitat.util import parse_time, run


def build_style(font, font_size, extra=None):
    """Build the libass force_style string."""
    # ASS colours are &HAABBGGRR: alpha first, RGB reversed, alpha inverted
    # (00 = opaque, FF = transparent).
    fields = [
        f"FontName={font}",
        f"FontSize={font_size}",
        "Alignment=2",
        "MarginV=28",
        "MarginL=40",
        "MarginR=40",
        "BorderStyle=1",
        "Outline=2",
        "Shadow=1",
        "PrimaryColour=&H00FFFFFF",
        "OutlineColour=&H00000000",
        # Without this the outline stays hairline-thin when libass scales the
        # script resolution up to the frame resolution.
        "ScaledBorderAndShadow=yes",
        # Smart wrap, not WrapStyle=2: the splitter guarantees columns, not
        # pixels, so libass must stay available as a safety net.
        "WrapStyle=0",
    ]
    if extra:
        fields.append(extra)
    return ",".join(fields)


def escape_srt_path(path):
    """Escape path for ffmpeg subtitles filter (libass)."""
    # libass requires escaping these characters
    path = path.replace("\\", "\\\\")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


def step_download(url, tmpdir, start="0", duration=None):
    """Step 1: Download video from YouTube (with optional section cut)."""
    needs_clip = start != "0" or duration is not None
    print("[1/7] Downloading video segment..." if needs_clip else "[1/7] Downloading video...")
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
        print("[1/7] Using local video file")
        return path
    print("[1/7] Cutting local video segment...")
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
    print("[2/7] Extracting audio...")
    output = os.path.join(tmpdir, "audio.wav")
    run([
        "ffmpeg", "-y",
        "-i", clip,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        output,
    ], "audio extraction")
    return output


def step_whisper(audio, tmpdir, whisper_bin, whisper_model, dtw, max_len):
    """Step 3: Generate subtitles with whisper."""
    print("[3/7] Transcribing audio...")
    output_stem = os.path.join(tmpdir, "audio")
    cmd = [
        whisper_bin,
        "-m", whisper_model,
        "-osrt",
        # -ojf also turns on -oj and token timestamps (cli.cpp:185, 1185)
        "-ojf",
        "-of", output_stem,
    ]
    if dtw:
        # DTW needs cross-attention weights that flash attention does not
        # expose; whisper silently disables DTW otherwise (whisper.cpp:3708).
        cmd += ["-dtw", dtw, "-nfa"]
    if max_len > 0:
        # Splitting mid-word is never wanted, so -sow always rides along.
        cmd += ["-ml", str(max_len), "-sow"]
    cmd.append(audio)
    run(cmd, "transcription")
    return output_stem + ".srt", output_stem + ".json"


def step_translate(cues, lang, tmpdir, batch_size, allow_partial):
    """Step 4: Translate cue text, keeping the source timecodes authoritative.

    Returns the translated cues plus the indices left untranslated, so later
    stages can treat the placeholder (source-language) cues differently.
    """
    print("[4/7] Translating subtitles...")
    env = claude_env()

    texts = [text for _, _, text in cues]
    total = len(texts)
    size = batch_size if batch_size > 0 else total
    translated = {}
    extra = 0

    for offset in range(0, total, size):
        batch = texts[offset:offset + size]
        for index, text in translate_texts(batch, lang, env).items():
            if 1 <= index <= len(batch):
                translated[offset + index - 1] = text
            else:
                extra += 1

    if not translated:
        print("  ERROR: translation returned nothing usable", file=sys.stderr)
        sys.exit(1)
    if extra:
        print(f"  WARNING: {extra} unexpected entr{'y' if extra == 1 else 'ies'} ignored")

    missing = [i for i in range(total) if i not in translated]
    if missing:
        print(f"  {len(missing)} cue(s) missing; retrying...")
        retry = translate_texts([texts[i] for i in missing], lang, env)
        for index, text in retry.items():
            if 1 <= index <= len(missing):
                translated[missing[index - 1]] = text
        missing = [i for i in range(total) if i not in translated]

    if missing:
        preview = ", ".join(format_ts(cues[i][0]) for i in missing[:5])
        if len(missing) > 5:
            preview += ", ..."
        if allow_partial:
            print(f"  WARNING: {len(missing)} cue(s) left untranslated at {preview}")
            print("  Source text kept as a placeholder — fix them in the review step.")
        else:
            print(f"  ERROR: {len(missing)} cue(s) left untranslated at {preview}",
                  file=sys.stderr)
            sys.exit(1)

    out = [(start, end, translated.get(i, text))
           for i, (start, end, text) in enumerate(cues)]

    # Written before anything else can fail, so --keep-tmp always has an artifact.
    path = os.path.join(tmpdir, "translated.srt")
    with open(path, "w") as f:
        f.write(format_srt(out))
    print(f"  Translated SRT written to {path}")
    return out, set(missing)


def step_split(cues, max_width, min_ms, bridge_ms, marker=None):
    """Step 5: Split translated cues into short single-line cues."""
    print("[5/7] Splitting cues...")
    segments_by_index = {}
    if marker:
        eligible = {i: text for i, (start, end, text) in enumerate(cues)
                    if needs_marks(start, end, text, max_width, min_ms)}
        if eligible:
            print(f"  marking phrase boundaries in {len(eligible)} cue(s)...")
            segments_by_index = marker(eligible)
    out = normalize_cues(split_cues(cues, max_width, min_ms, segments_by_index))
    print(f"  {len(cues)} cues -> {len(out)} cues")

    if bridge_ms > 0:
        held = bridge_gaps(out, bridge_ms)
        closed = sum(1 for a, b in zip(out, held) if a[1] != b[1])
        gained = sum(b[1] - a[1] for a, b in zip(out, held))
        if closed:
            print(f"  closed {closed} gap(s) up to {bridge_ms}ms "
                  f"(+{gained / 1000:.1f}s on screen)")
        out = held
    # A cue too short to hold two readable chunks keeps one over-budget line,
    # which libass will wrap. Say so rather than letting it pass silently.
    wide = sum(1 for _, _, text in out if display_width(text) > max_width)
    if wide:
        print(f"  NOTE: {wide} cue(s) exceed {max_width} columns — too short to "
              f"split at --min-cue-ms {min_ms}")
    return out


def step_review(srt_path):
    """Step 6: Open translated subtitles for human review."""
    print("[6/7] Reviewing subtitles...")
    editor = os.environ.get("EDITOR", "vim")
    print(f"  $ {editor} {srt_path}")
    subprocess.run([editor, srt_path])
    # The editor may leave overlapping or unsorted cues behind.
    with open(srt_path) as f:
        cues = parse_srt(f.read())
    if not cues:
        print("  ERROR: review left no usable subtitles", file=sys.stderr)
        sys.exit(1)
    with open(srt_path, "w") as f:
        f.write(format_srt(normalize_cues(cues)))


def step_burn(clip, srt_path, output_path, style):
    """Step 7: Burn subtitles into video."""
    print("[7/7] Burning subtitles into video...")
    escaped = escape_srt_path(srt_path)
    vf = f"subtitles={escaped}:force_style='{style}'"
    run([
        "ffmpeg", "-y",
        "-i", clip,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ], "subtitle burn")
    return output_path
