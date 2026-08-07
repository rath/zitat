# zitat

YouTube clip subtitle pipeline. Single file `zitat.py`, stdlib only, no external packages.

## Structure

```
zitat.py          # Entire pipeline (stdlib only)
```

## Pipeline (7 steps)

1. `yt-dlp` — Download video (max 1024px width, `--download-sections` + `--force-keyframes-at-cuts` when start/duration specified)
2. `ffmpeg` — Extract audio (16kHz mono WAV)
3. `whisper-cli` — Transcribe to SRT **and** JSON; word timings from the JSON are re-segmented into clause-level cues
4. `claude -p` — Translate subtitles (must filter out `CLAUDE_CODE_ENTRYPOINT` env var)
5. Split cues to the display-width budget (`--no-split` to skip)
6. `$EDITOR` — Human review of translated subtitles (`--no-review` to skip)
7. `ffmpeg` — Burn subtitles (libass subtitles filter)

## Configuration

Settings are managed via `.env` file (same directory as script). Priority: CLI options > shell env vars > `.env`.

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
  `translated.srt` (pre-split), `final.srt` (burned)

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
- Korean translation is distributed *within* a fixed cue span by display width. Word
  timings never map Korean 어절 to English words — SVO vs SOV makes that unsound.

## SRT handling

- `parse_srt()` scans for timecode lines rather than splitting on blank lines, so it
  tolerates renumbering, missing indices, code fences, `.` vs `,`, CRLF, and BOM
- `format_srt()` always renumbers from 1, which is why entry indices are never validated
- Translation sends numbered plain-text lines and re-attaches the **source** timecodes,
  so timecode drift is structurally impossible; only index coverage is validated
