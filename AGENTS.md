# zitat

YouTube clip subtitle pipeline. Stdlib only, no external packages. Run with
`python -m zitat` from the repo root.

## Structure

```
zitat/
  __main__.py     # python -m zitat entry point
  cli.py          # argparse, config resolution (CLI > env > .env), pipeline wiring
  util.py         # load_dotenv (reads .env from the repo root), run(), env helpers
  subs.py         # cue model: SRT parse/format, display-width split, gap bridging
  words.py        # whisper JSON -> word timings -> clause-level source cues
  claude.py       # claude -p contracts: numbered-line translation + phrase marking
  steps.py        # the seven pipeline steps + libass burn style
```

## Pipeline (7 steps)

1. `yt-dlp` — Download video (max 1024px width, `--download-sections` + `--force-keyframes-at-cuts` when start/duration specified)
2. `ffmpeg` — Extract audio (16kHz mono WAV)
3. `whisper-cli` — Transcribe to SRT **and** JSON; word timings from the JSON are re-segmented into clause-level cues
4. `claude -p` — Translate subtitles (must filter out `CLAUDE_CODE_ENTRYPOINT` env var)
5. Split cues: `claude -p` marks natural phrase boundaries with `|`, then cues are
   split at the marks to the display-width budget (`--no-split` to skip the step,
   `--no-phrase-marks` to skip just the marking call)
6. `$EDITOR` — Human review of translated subtitles (`--no-review` to skip)
7. `ffmpeg` — Burn subtitles (libass subtitles filter)

## Configuration

Settings are managed via `.env` file (repo root). Priority: CLI options > shell env vars > `.env`.

## External tools

- `yt-dlp`, `ffmpeg`, `claude` — resolved from PATH
- `whisper-cli` — `WHISPER_BIN` (default: `whisper-cli` from PATH)
- Whisper model — `WHISPER_MODEL` (required, no default)

## Notes

- ffmpeg subtitles filter requires escaping `:` → `\:` in paths (libass parser)
- `force_style` is comma-separated inside single quotes in `-vf`, so `'` and `,` are
  rejected in `--font` (only `'` in `--style`, which is itself a field list)
- ASS colours are `&HAABBGGRR` — alpha first, RGB reversed, alpha inverted (`00` = opaque)
- Temp files go to `tempfile.mkdtemp(prefix="zitat_")`, cleaned up in `finally`
- `--keep-tmp` preserves intermediate files: `audio.srt`, `audio.json`,
  `translated.srt` (pre-split), `marks.json` (phrase segments), `final.srt` (burned)

## Subtitle timing

- **`-ojf` alone turns on token timestamps** (`cli.cpp:1185`) and also implies `-oj`
  (`cli.cpp:185`), so `-ml` is not needed to get word-level data.
- **`-dtw` requires `-nfa`.** whisper silently disables DTW when flash attention is on
  (`whisper.cpp:3708`), and flash attention defaults to true — the symptom is `t_dtw: -1`
  on every token. The DTW preset is derived from the model filename and validated against
  `DTW_PRESETS`; an unknown model just runs without DTW.
- `t_dtw` is a single moment, not a span, and is undefined unless DTW ran (`whisper.h:145`).
  It only refines word onsets; spans always come from the token `offsets`.
- Cue ends come from the **last word's** end, not the whisper segment end, which overshoots
  into the following silence.
- Clamping to the last word leaves sub-pause gaps that read as flicker, so `bridge_gaps()`
  holds a cue until the next one starts whenever the gap is `<= --bridge-gap-ms`. Only
  longer gaps survive as real silence. This is also what buys reading time — proportional
  allocation cannot create it.
- Korean translation is distributed *within* a fixed cue span by display width. Word
  timings never map Korean 어절 to English words — SVO vs SOV makes that unsound.

## Phrase-aware splitting

- Splitting is whitespace-greedy and knows no Korean phrase structure; the
  linguistic knowledge comes from a second `claude -p` call that inserts `|` at
  natural boundaries. Doing this properly offline would need a morphological
  analyser (external package), which the stdlib-only rule forbids.
- The equality gate is the whole safety story: a marked line is used only if
  stripping the `|`s reproduces the original text exactly, so marks can only
  sit on 어절 boundaries and any other edit is rejected. Failed lines degrade
  per-line to plain width splitting — worst case equals the markless behaviour.
- Marks are carried out-of-band as a `{cue index: [segments]}` dict, never
  embedded in cue text, so no early-return path can leak a `|` into review or
  the burn, and `display_width` never counts one.
- The marking call runs with `fatal=False`; it must never discard the
  download + transcription + translation work already done.
- `rebalance_lines`' floor covers the widest atom, so the narrowing sweep can
  never explode a phrase segment back into words — without that invariant it
  would reintroduce the very mid-phrase breaks the marks removed.
- Untranslated placeholder cues (still source-language) are excluded from
  marking via the missing-index set, not by language detection.

## SRT handling

- `parse_srt()` scans for timecode lines rather than splitting on blank lines, so it
  tolerates renumbering, missing indices, code fences, `.` vs `,`, CRLF, and BOM
- `format_srt()` always renumbers from 1, which is why entry indices are never validated
- Translation sends numbered plain-text lines and re-attaches the **source** timecodes,
  so timecode drift is structurally impossible; only index coverage is validated
- `wrap_text()` is greedy, which strips the remainder onto the last line and leaves
  three-syllable orphans on screen alone. `rebalance_lines()` re-wraps at the narrowest
  budget that yields the same line count, which is the most even one. It can only make
  lines narrower, so it never breaks the width budget or the line-count cap.
