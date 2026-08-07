#!/usr/bin/env python3
"""zitat — YouTube clip Korean subtitle pipeline."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata


# whisper.cpp --dtw presets (examples/cli/cli.cpp).
DTW_PRESETS = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large.v1", "large.v2", "large.v3", "large.v3.turbo",
}

SRT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)

# "12<TAB>text", "12. text", "12) text", "12: text", "12 text"
NUMBERED_RE = re.compile(r"^\s*(\d+)[\t ]*[.:|)\-]?[\t ]*(\S.*?)\s*$")

FENCE_RE = re.compile(r"^```[a-zA-Z]*$")

SENTENCE_END = (".", "?", "!", "。", "？", "！")

# How long a word stays audible past its DTW onset. Anything beyond this in the
# interval to the next onset is treated as silence, so the effective pause
# threshold on a DTW timeline is --pause-gap-ms + WORD_TAIL_MS.
WORD_TAIL_MS = 250


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


def env_str(name, fallback):
    """String setting from environment (or .env), with fallback."""
    value = os.environ.get(name)
    return value if value else fallback


def env_int(name, fallback):
    """Integer setting from environment (or .env), with fallback."""
    value = os.environ.get(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        print(f"ERROR: {name} must be an integer, got {value!r}", file=sys.stderr)
        sys.exit(1)


def run(cmd, desc, capture=False, env=None, stdin_text=None):
    """Run a subprocess command with error handling."""
    shown = [a if len(a) <= 80 else a[:77] + "..." for a in cmd]
    print(f"  $ {' '.join(shown)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=True,
            env=env,
            input=stdin_text,
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


# --- SRT model -------------------------------------------------------------
# A cue is a plain (start_ms, end_ms, text) tuple. Integer milliseconds match
# SRT's own resolution exactly, so time redistribution stays exact arithmetic.


def format_ts(ms):
    """Format milliseconds as an SRT timestamp."""
    ms = max(0, int(ms))
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _ts_from_groups(hours, minutes, seconds, frac):
    """Convert matched timestamp groups to milliseconds."""
    # ",5" means 500ms, not 5ms — pad the fraction on the right.
    ms = int(frac.ljust(3, "0"))
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + ms


def parse_srt(text):
    """Parse SRT text into a list of (start_ms, end_ms, text) cues."""
    # Scan for timecode lines rather than splitting on blank lines: entries may
    # be renumbered, unnumbered, or wrapped in prose by an LLM.
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(SRT_TIME_RE.finditer(text))
    cues = []
    for i, m in enumerate(matches):
        newline = text.find("\n", m.end())
        body_start = len(text) if newline == -1 else newline + 1
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        lines = text[body_start:body_end].split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        # A trailing digits-only line belongs to the *next* entry, not this one.
        if i + 1 < len(matches) and lines and lines[-1].strip().isdigit():
            lines.pop()
        kept = [
            line.strip() for line in lines
            if line.strip() and not FENCE_RE.match(line.strip())
        ]
        body = "\n".join(kept)
        start = _ts_from_groups(*m.group(1, 2, 3, 4))
        end = _ts_from_groups(*m.group(5, 6, 7, 8))
        if not body or end < start:
            continue
        cues.append((start, end, body))
    return cues


def format_srt(cues):
    """Render cues as SRT text, renumbered from 1."""
    blocks = [
        f"{i}\n{format_ts(start)} --> {format_ts(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


def normalize_cues(cues):
    """Drop empty cues, sort, and truncate overlaps so cues stay monotonic."""
    clean = []
    for start, end, text in cues:
        text = text.strip()
        if text and end >= start:
            clean.append((start, end, text))
    clean.sort(key=lambda c: (c[0], c[1]))

    out = []
    for cue in clean:
        while out and cue[0] <= out[-1][0]:
            print(f"  WARNING: dropping cue swallowed by overlap at {format_ts(out[-1][0])}")
            out.pop()
        if out and out[-1][1] > cue[0]:
            prev_start, _, prev_text = out[-1]
            out[-1] = (prev_start, cue[0], prev_text)
        out.append(cue)
    return out


# --- Word timings ----------------------------------------------------------


def dtw_preset_for(model_path):
    """Derive a whisper --dtw preset from the model filename, or None."""
    name = os.path.basename(model_path)
    name = re.sub(r"\.bin$", "", name)
    name = re.sub(r"^ggml-", "", name)
    # Quantized builds share their base model's alignment heads.
    name = re.sub(r"-q\d+_\w+$", "", name)
    preset = name.replace("-", ".")
    return preset if preset in DTW_PRESETS else None


def apply_dtw(words, limit):
    """Rebuild word spans from DTW onsets. Returns False if they are unusable."""
    # t_dtw is a single moment, not a span (whisper.h), so mixing it with the
    # token offsets collapses most words to zero length. Either the whole
    # timeline comes from DTW or none of it does.
    onsets = [w["dtw"] for w in words]
    if any(o is None for o in onsets):
        return False
    if any(b < a for a, b in zip(onsets, onsets[1:])):
        return False
    for i, word in enumerate(words):
        word["start"] = word["dtw"]
        nxt = words[i + 1]["dtw"] if i + 1 < len(words) else None
        # A word is audible for a while after its onset; beyond that the
        # remaining interval is silence, which is what pause detection wants.
        tail = word["start"] + WORD_TAIL_MS
        word["end"] = min(tail, nxt) if nxt is not None else tail
        # The tail of the final word must not run past the end of the audio.
        word["end"] = max(word["start"], min(word["end"], limit))
    return True


def load_words(json_path, use_dtw):
    """Reassemble whisper's BPE tokens into words with millisecond timings."""
    try:
        with open(json_path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  WARNING: cannot read word timings ({e})")
        return []

    words = []
    limit = 0
    for segment in data.get("transcription", []):
        limit = max(limit, (segment.get("offsets") or {}).get("to") or 0)
        current = None
        for token in segment.get("tokens", []):
            raw = token.get("text", "")
            if raw.startswith("[_") or not raw.strip():
                continue
            offsets = token.get("offsets") or {}
            start, end = offsets.get("from"), offsets.get("to")
            t_dtw = token.get("t_dtw")
            onset = int(t_dtw) * 10 if t_dtw is not None and t_dtw >= 0 else None
            if raw.startswith(" ") or current is None:
                if current is not None:
                    words.append(current)
                current = {"text": raw.strip(), "start": start, "end": end,
                           "dtw": onset}
            else:
                current["text"] += raw.strip()
                if current["start"] is None:
                    current["start"] = start
                if current["dtw"] is None:
                    current["dtw"] = onset
                if end is not None:
                    current["end"] = end
        if current is not None:
            words.append(current)

    words = [w for w in words if w["text"]]
    for i, word in enumerate(words):
        if word["start"] is None:
            word["start"] = words[i - 1]["end"] if i else 0
    for i in range(len(words) - 1, -1, -1):
        if words[i]["end"] is None:
            nxt = words[i + 1]["start"] if i + 1 < len(words) else None
            words[i]["end"] = nxt if nxt is not None else words[i]["start"]
    for word in words:
        if word["start"] is None or word["end"] is None:
            return []
        word["end"] = max(word["end"], word["start"])

    if use_dtw and words and not apply_dtw(words, limit):
        print("  WARNING: DTW onsets unusable; using token timestamps instead")
    return words


def segment_words(words, gap_ms, max_ms, min_ms):
    """Group words into clause-level cues, breaking on pauses and sentence ends."""
    cues = []
    group = []

    def flush():
        if group:
            cues.append((group[0]["start"], group[-1]["end"],
                         " ".join(w["text"] for w in group)))
            group.clear()

    for word in words:
        if group:
            gap = word["start"] - group[-1]["end"]
            span = group[-1]["end"] - group[0]["start"]
            ends_sentence = group[-1]["text"].endswith(SENTENCE_END)
            # Breaking here would leave a cue too short to read — a flash of
            # text — so keep accumulating until it can stand on its own.
            if span >= min_ms and (gap > gap_ms or ends_sentence):
                flush()
            elif word["end"] - group[0]["start"] > max_ms:
                flush()
        group.append(word)
    flush()

    # The last group has no successor to grow into; fold it back if too short.
    if len(cues) > 1 and cues[-1][1] - cues[-1][0] < min_ms:
        prev, last = cues[-2], cues[-1]
        cues[-2:] = [(prev[0], last[1], f"{prev[2]} {last[2]}")]
    return cues


def build_source_cues(srt_path, json_path, use_dtw, gap_ms, max_ms, min_ms):
    """Build clause-level cues from word timings, falling back to whisper's SRT."""
    words = load_words(json_path, use_dtw)
    if words:
        cues = normalize_cues(segment_words(words, gap_ms, max_ms, min_ms))
        if cues:
            return cues
    print("  WARNING: no usable word timings; falling back to whisper's SRT")
    with open(srt_path) as f:
        return normalize_cues(parse_srt(f.read()))


# --- Cue splitting ---------------------------------------------------------


def char_width(ch):
    """Display width of a single character in terminal-style columns."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s):
    """Display width in columns; CJK and fullwidth characters count as 2."""
    # Deliberately not tied to --lang: this is a per-character property, so a
    # mixed string is measured correctly and pure Latin degenerates to len().
    return sum(char_width(ch) for ch in s)


def hard_break(token, budget):
    """Break an over-budget token at character boundaries."""
    pieces, current, width = [], [], 0
    for ch in token:
        cw = char_width(ch)
        if current and width + cw > budget:
            pieces.append("".join(current))
            current, width = [], 0
        current.append(ch)
        width += cw
    if current:
        pieces.append("".join(current))
    return pieces


def wrap_text(text, budget):
    """Greedy whitespace wrap to a display-column budget."""
    lines, current, width = [], [], 0

    def flush():
        nonlocal current, width
        if current:
            lines.append(" ".join(current))
            current, width = [], 0

    for token in text.split():
        token_width = display_width(token)
        if token_width > budget:
            flush()
            pieces = hard_break(token, budget) if any(
                char_width(ch) == 2 for ch in token) else [token]
            # CJK breaks legally anywhere; a long Latin run is left whole and
            # allowed to overflow, since mid-word breaks read worse than a long
            # line and libass still wraps it as a safety net.
            lines.extend(pieces[:-1])
            current, width = [pieces[-1]], display_width(pieces[-1])
            continue
        added = token_width if not current else token_width + 1
        if width + added > budget:
            flush()
            current, width = [token], token_width
        else:
            current.append(token)
            width += added
    flush()
    return lines or [text.strip()]


def wrap_to_max_lines(text, budget, max_lines):
    """Wrap text, widening the budget until the chunk count fits."""
    max_lines = max(1, max_lines)
    total = display_width(text)
    budget = max(budget, -(-total // max_lines))
    lines = wrap_text(text, budget)
    while len(lines) > max_lines:
        budget += 2
        lines = wrap_text(text, budget)
    return lines


def split_cue(start, end, text, max_width, min_ms):
    """Split one cue into short single-line cues sharing its time span."""
    text = " ".join(text.split())
    if not text:
        return []
    total = end - start
    if total <= 0 or display_width(text) <= max_width:
        return [(start, end, text)]

    # Cap the chunk count first so a dense cue widens its lines instead of
    # producing a flicker-storm. This makes n * min_ms <= total an invariant.
    max_lines = total // min_ms if min_ms > 0 else len(text)
    chunks = wrap_to_max_lines(text, max_width, max_lines)
    n = len(chunks)
    if n == 1:
        return [(start, end, chunks[0])]

    # Allocate time proportionally to display width, which holds the required
    # reading rate constant across chunks. Cumulative integer boundaries keep
    # the split monotonic and make the last boundary land exactly on `end`.
    widths = [display_width(c) for c in chunks]
    total_width = sum(widths) or n
    bounds, acc = [start], 0
    for width in widths[:-1]:
        acc += width
        bounds.append(start + total * acc // total_width)
    bounds.append(end)

    if min_ms > 0:
        for i in range(n):
            if bounds[i + 1] - bounds[i] < min_ms:
                bounds[i + 1] = bounds[i] + min_ms
        bounds[n] = end
        for i in range(n - 1, 0, -1):
            if bounds[i + 1] - bounds[i] < min_ms:
                bounds[i] = bounds[i + 1] - min_ms

    return [(bounds[i], bounds[i + 1], chunks[i]) for i in range(n)]


def split_cues(cues, max_width, min_ms):
    """Split every cue to the display-width budget."""
    out = []
    for start, end, text in cues:
        out.extend(split_cue(start, end, text, max_width, min_ms))
    return out


# --- Subtitle style --------------------------------------------------------


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


# --- Pipeline steps --------------------------------------------------------


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


def translate_texts(texts, lang, env):
    """Translate numbered lines via claude; returns {1-based index: translation}."""
    # One line per entry is the whole contract, so a multi-line cue from the
    # SRT fallback path must be flattened before it corrupts the numbering.
    payload = "\n".join(f"{i}\t{' '.join(text.split())}"
                        for i, text in enumerate(texts, 1))
    n = len(texts)
    prompt = (
        f"stdin으로 번호가 매겨진 자막 줄들을 받는다. 각 줄은 '번호<TAB>원문' 형식이다. "
        f"이 원문들을 자연스러운 {lang}(으)로 번역해.\n"
        f"입력은 {n}개 항목이고 출력도 정확히 {n}개여야 한다.\n"
        "규칙:\n"
        "- 각 줄을 '번호<TAB>번역문' 형식으로 출력한다. 번호는 입력과 동일하게 유지한다.\n"
        "- 항목을 합치거나 나누거나 빼지 않는다.\n"
        "- 한 항목은 반드시 한 줄로 쓴다.\n"
        "- 번역문 외에 설명이나 코드 펜스를 붙이지 않는다.\n"
        "- 앞뒤 항목이 이어지는 한 문장일 수 있으니 전체 맥락을 보고 번역한다."
    )
    result = run(["claude", "-p", prompt], "translation",
                 capture=True, env=env, stdin_text=payload)

    out = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or FENCE_RE.match(line):
            continue
        m = NUMBERED_RE.match(line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def step_translate(cues, lang, tmpdir, batch_size, allow_partial):
    """Step 4: Translate cue text, keeping the source timecodes authoritative."""
    print("[4/7] Translating subtitles...")
    # Filter out CLAUDE_CODE_ENTRYPOINT to avoid nested execution issues
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}

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
    return out


def step_split(cues, max_width, min_ms):
    """Step 5: Split translated cues into short single-line cues."""
    print("[5/7] Splitting cues...")
    out = normalize_cues(split_cues(cues, max_width, min_ms))
    print(f"  {len(cues)} cues -> {len(out)} cues")
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
    parser.add_argument("--translate-batch", type=int, default=None,
                        help="Cues per claude call, 0 to never batch (default: 80)")
    parser.add_argument("--whisper-bin", default=None, help="Path to whisper-cli (default: $WHISPER_BIN or 'whisper-cli')")
    parser.add_argument("--whisper-model", default=None, help="Path to whisper model (default: $WHISPER_MODEL)")
    parser.add_argument("--whisper-max-len", type=int, default=None,
                        help="whisper -ml segment cap, 0 to disable (default: 0)")
    parser.add_argument("--dtw", default=None, help="whisper DTW preset (default: derived from model name)")
    parser.add_argument("--no-dtw", action="store_true", help="Disable DTW word timestamps")
    parser.add_argument("--no-split", action="store_true", help="Skip cue splitting")
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

        cues = step_translate(cues, args.lang, tmpdir, translate_batch,
                              allow_partial=not args.no_review)

        if args.no_split:
            print("[5/7] Skipping cue splitting")
        else:
            cues = step_split(cues, max_width, min_cue_ms)

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


if __name__ == "__main__":
    main()
