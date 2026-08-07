"""Whisper word timings: DTW presets, token reassembly, clause segmentation."""

import json
import os
import re

from zitat.subs import normalize_cues, parse_srt

# whisper.cpp --dtw presets (examples/cli/cli.cpp).
DTW_PRESETS = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large.v1", "large.v2", "large.v3", "large.v3.turbo",
}

SENTENCE_END = (".", "?", "!", "。", "？", "！")

# How long a word stays audible past its DTW onset. Anything beyond this in the
# interval to the next onset is treated as silence, so the effective pause
# threshold on a DTW timeline is --pause-gap-ms + WORD_TAIL_MS.
WORD_TAIL_MS = 250


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
