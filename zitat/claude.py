"""The claude CLI contracts: numbered-line translation and phrase marking."""

import os
import re

from zitat.util import FENCE_RE, run

# "12<TAB>text", "12. text", "12) text", "12: text", "12 text"
NUMBERED_RE = re.compile(r"^\s*(\d+)[\t ]*[.:|)\-]?[\t ]*(\S.*?)\s*$")

# Strict echo of the numbered contract. NUMBERED_RE's separator class would
# eat a leading '.', '-' or '|' out of the text itself, which breaks the
# equality gate on phrase-marked lines, so marking tries this parse first.
MARK_TAB_RE = re.compile(r"^\s*(\d+)\t[\t ]*(\S.*?)\s*$")


def claude_env():
    """Environment for claude subprocess calls."""
    # Filter out CLAUDE_CODE_ENTRYPOINT to avoid nested execution issues
    return {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}


def numbered_call(texts, prompt, desc, env, fatal=True):
    """Send numbered lines to claude on stdin; returns stdout or None."""
    # One line per entry is the whole contract, so a multi-line cue from the
    # SRT fallback path must be flattened before it corrupts the numbering.
    payload = "\n".join(f"{i}\t{' '.join(text.split())}"
                        for i, text in enumerate(texts, 1))
    result = run(["claude", "-p", prompt], desc,
                 capture=True, env=env, stdin_text=payload, fatal=fatal)
    return None if result is None else result.stdout


def translate_texts(texts, lang, env):
    """Translate numbered lines via claude; returns {1-based index: translation}."""
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
    stdout = numbered_call(texts, prompt, "translation", env)

    out = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or FENCE_RE.match(line):
            continue
        m = NUMBERED_RE.match(line)
        # Keep-first: a translation wrapped onto a second physical line that
        # happens to start with a numeral must not overwrite a real entry.
        if m and int(m.group(1)) not in out:
            out[int(m.group(1))] = m.group(2)
    return out


MARK_PROMPT = (
    "stdin으로 번호가 매겨진 자막 줄들을 받는다. 각 줄은 '번호<TAB>문장' 형식이다. "
    "각 문장 안에, 자막이 여기서 끊겨도 자연스러운 지점마다 '|'를 삽입해.\n"
    "규칙:\n"
    "- 출력은 입력과 글자 하나까지 동일해야 하며 '|' 삽입만 허용된다.\n"
    "- '|'는 단어 사이(공백 위치)에만 넣는다.\n"
    "- 의미 단위(구)가 끝나는 곳마다 촘촘히(2~4어절 간격) 넣는다.\n"
    "- 수식어와 수식받는 말 사이는 절대 나누지 않는다.\n"
    "- '번호<TAB>결과' 형식으로, 항목을 합치거나 나누거나 빼지 말고 "
    "한 항목은 한 줄로 출력한다.\n"
    "- 설명이나 코드 펜스를 붙이지 않는다."
)


def mark_segments(line, original):
    """Validate one marked line against its original; segments or None."""
    segments = [" ".join(s.split()) for s in line.split("|")]
    segments = [s for s in segments if s]
    # The equality gate is the whole safety story: it forces every mark onto
    # an existing word boundary and rejects any other edit. A markless echo
    # passes it but adds nothing, so it goes to the retry instead.
    if len(segments) > 1 and " ".join(segments) == original:
        return segments
    return None


def parse_marks(stdout, originals):
    """Parse marked lines; keeps the first candidate that survives validation.

    originals maps 1-based payload index to normalized text. Returns
    {index: [segments]}.
    """
    out = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or FENCE_RE.match(line):
            continue
        for m in (MARK_TAB_RE.match(line), NUMBERED_RE.match(line)):
            if not m:
                continue
            index = int(m.group(1))
            # Keep-first: a later garbage line (say, a wrapped continuation
            # starting with a numeral) must not hijack a validated slot.
            if index in out or index not in originals:
                continue
            segments = mark_segments(m.group(2), originals[index])
            if segments:
                out[index] = segments
                break
    return out


def mark_texts(texts_by_index, env, batch_size):
    """Ask claude to mark phrase boundaries; returns {cue index: [segments]}."""
    indices = sorted(texts_by_index)
    normalized = {i: " ".join(texts_by_index[i].split()) for i in indices}
    out = {}
    size = batch_size if batch_size > 0 else len(indices)

    def request(batch):
        stdout = numbered_call([normalized[i] for i in batch], MARK_PROMPT,
                               "phrase marking", env, fatal=False)
        if stdout is None:
            return
        originals = {k: normalized[i] for k, i in enumerate(batch, 1)}
        for k, segments in parse_marks(stdout, originals).items():
            out[batch[k - 1]] = segments

    for offset in range(0, len(indices), size):
        request(indices[offset:offset + size])

    # One retry covers missing and validation-failed lines alike; whatever
    # still fails degrades per-line to plain width splitting.
    failed = [i for i in indices if i not in out]
    if failed:
        request(failed)
        failed = [i for i in indices if i not in out]
    if failed:
        print(f"  NOTE: {len(failed)} cue(s) unmarked; splitting by width alone")
    return out
