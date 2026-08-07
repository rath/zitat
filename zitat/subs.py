"""SRT cue model, display-width splitting, and gap bridging.

A cue is a plain (start_ms, end_ms, text) tuple. Integer milliseconds match
SRT's own resolution exactly, so time redistribution stays exact arithmetic.
"""

import re
import unicodedata

from zitat.util import FENCE_RE

SRT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


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


def explode_atoms(atoms, budget):
    """Break multi-word atoms that cannot fit the budget back into words."""
    out = []
    for atom in atoms:
        if " " in atom and display_width(atom) > budget:
            out.extend(atom.split())
        else:
            out.append(atom)
    return out


def wrap_atoms(atoms, budget):
    """Greedy wrap of atoms (words or phrase segments) to a column budget."""
    atoms = explode_atoms(atoms, budget)
    lines, current, width = [], [], 0

    def flush():
        nonlocal current, width
        if current:
            lines.append(" ".join(current))
            current, width = [], 0

    for atom in atoms:
        atom_width = display_width(atom)
        if atom_width > budget:
            flush()
            pieces = hard_break(atom, budget) if any(
                char_width(ch) == 2 for ch in atom) else [atom]
            # CJK breaks legally anywhere; a long Latin run is left whole and
            # allowed to overflow, since mid-word breaks read worse than a long
            # line and libass still wraps it as a safety net.
            lines.extend(pieces[:-1])
            current, width = [pieces[-1]], display_width(pieces[-1])
            continue
        added = atom_width if not current else atom_width + 1
        if width + added > budget:
            flush()
            current, width = [atom], atom_width
        else:
            current.append(atom)
            width += added
    flush()
    return lines


def wrap_text(text, budget):
    """Greedy whitespace wrap to a display-column budget."""
    return wrap_atoms(text.split(), budget) or [text.strip()]


def rebalance_lines(atoms, budget, lines):
    """Even out line widths without increasing the line count."""
    # Greedy wrapping fills the early lines and strips the remainder onto the
    # last one, which is what leaves a three-syllable orphan alone on screen.
    # The narrowest budget that still yields the same number of lines is the
    # most even one, and it can only ever be narrower than what we started with.
    n = len(lines)
    if n < 2 or not atoms:
        return lines
    # The floor covers the widest atom, so no sweep target can explode a
    # phrase segment back into words — narrowing must never reintroduce the
    # mid-phrase breaks the marks removed.
    floor = max(-(-display_width(" ".join(atoms)) // n),
                max(display_width(a) for a in atoms))
    for target in range(floor, budget):
        candidate = wrap_atoms(atoms, target)
        if len(candidate) <= n:
            return candidate
    return lines


def wrap_to_max_lines(atoms, budget, max_lines):
    """Wrap atoms, widening the budget until the chunk count fits."""
    max_lines = max(1, max_lines)
    total = display_width(" ".join(atoms))
    budget = max(budget, -(-total // max_lines))
    lines = wrap_atoms(atoms, budget)
    while len(lines) > max_lines:
        budget += 2
        lines = wrap_atoms(atoms, budget)
    # Rebalance over what actually got wrapped: an atom wider than the final
    # budget was exploded into words, and the unexploded original would push
    # the floor past the budget and silently skip rebalancing.
    return rebalance_lines(explode_atoms(atoms, budget), budget, lines)


def split_cue(start, end, text, max_width, min_ms, segments=None):
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
    atoms = segments if segments else text.split()
    chunks = wrap_to_max_lines(atoms, max_width, max_lines)
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


def split_cues(cues, max_width, min_ms, segments_by_index=None):
    """Split every cue to the display-width budget."""
    segments_by_index = segments_by_index or {}
    out = []
    for i, (start, end, text) in enumerate(cues):
        out.extend(split_cue(start, end, text, max_width, min_ms,
                             segments_by_index.get(i)))
    return out


def needs_marks(start, end, text, max_width, min_ms):
    """Whether a cue will actually split and can honour phrase marks."""
    text = " ".join(text.split())
    total = end - start
    # Mirror split_cue's early return exactly: these cues pass through whole.
    if total <= 0 or display_width(text) <= max_width:
        return False
    # max_lines caps at one chunk, so marks could never take effect.
    if min_ms > 0 and total < 2 * min_ms:
        return False
    # A literal pipe would make the marked echo ambiguous to parse.
    return "|" not in text


def bridge_gaps(cues, bridge_ms):
    """Hold a cue until the next one starts unless a real pause separates them."""
    # Cue ends sit on the last word, so anything short of a real pause shows up
    # as a blink of blank screen. Holding through it also buys reading time,
    # which is the one thing proportional allocation cannot create.
    out = list(cues)
    for i in range(len(out) - 1):
        start, end, text = out[i]
        nxt = out[i + 1][0]
        if 0 < nxt - end <= bridge_ms:
            out[i] = (start, nxt, text)
    return out
