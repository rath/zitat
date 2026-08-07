# zitat

YouTube 영상의 특정 구간을 다운로드하고, 자막을 생성·번역해서 영상에 입히는 CLI 도구.

> **zitat** — 독일어로 "인용(Zitat)"

## 설치

### 필수 도구

모두 시스템에 설치되어 있어야 합니다.

| 도구 | 설치 방법 (macOS) |
|------|-------------------|
| **yt-dlp** | `brew install yt-dlp` |
| **ffmpeg** | `brew install ffmpeg` |
| **whisper.cpp** | 아래 참고 |
| **Claude CLI** | `npm install -g @anthropic-ai/claude-code` |

### whisper.cpp 설치

```bash
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp

# 모델 다운로드
./models/download-ggml-model.sh large-v3-turbo

# 빌드
cmake -B build
cmake --build build --config Release
```

빌드 후 `.env.example`을 복사해서 경로를 설정하세요:

```bash
cp .env.example .env
```

```bash
# .env
WHISPER_BIN=~/whisper.cpp/build/bin/whisper-cli
WHISPER_MODEL=~/whisper.cpp/models/ggml-large-v3-turbo.bin
```

`--whisper-bin`, `--whisper-model` CLI 옵션이나 셸 환경변수로도 지정 가능합니다.

### 자막 폰트

기본 폰트는 **BM Dohyeon**(배민 도현체)입니다. 설치되어 있지 않으면 `--font` 옵션으로 다른 폰트를 지정하세요.

- [배민 도현체 다운로드](https://www.woowahan.com/fonts)

## 사용법

```bash
python zitat.py <youtube-url> [옵션]
```

### 옵션

| 옵션 | 기본값 | 환경변수 | 설명 |
|------|--------|----------|------|
| `-ss`, `--start` | `0` | — | 시작 시간 (ffmpeg 포맷: `0:01:30`, `90` 등) |
| `-t`, `--duration` | 전체 | — | 길이 (초 또는 ffmpeg 포맷) |
| `-o`, `--output` | `{video_id}_ko` | — | 출력 파일명 (.mp4 자동 추가) |
| `--lang` | `Korean` | — | 번역 대상 언어 |
| `--max-width` | `30` | `ZITAT_MAX_WIDTH` | 큐 최대 표시 칸 (한글 1자 = 2칸, 30 ≈ 15자) |
| `--min-cue-ms` | `800` | `ZITAT_MIN_CUE_MS` | 큐 최소 표시 시간(ms). `0`이면 비활성 |
| `--pause-gap-ms` | `400` | `ZITAT_PAUSE_GAP_MS` | 이 이상 벌어지면 원문 큐를 나눔(ms) |
| `--max-cue-ms` | `5000` | `ZITAT_MAX_CUE_MS` | 원문 큐 최대 길이(ms) |
| `--bridge-gap-ms` | `1500` | `ZITAT_BRIDGE_GAP_MS` | 이 길이 이하의 간격은 메워서 자막을 유지(ms). `0`이면 비활성 |
| `--no-split` | — | — | 5단계 전체(재분할 + 간격 메우기) 건너뛰기 |
| `--no-phrase-marks` | — | — | claude 구 경계 표시 없이 표시 폭만으로 재분할 |
| `--translate-batch` | `80` | `ZITAT_TRANSLATE_BATCH` | claude 호출당 큐 수. `0`이면 배치 안 함 |
| `--font` | `BM Dohyeon` | `ZITAT_FONT` | 자막 폰트 |
| `--font-size` | `22` | `ZITAT_FONT_SIZE` | 자막 크기 |
| `--style` | — | `ZITAT_STYLE` | libass `force_style`에 추가할 필드 |
| `--whisper-bin` | `whisper-cli` | `WHISPER_BIN` | whisper-cli 바이너리 경로 |
| `--whisper-model` | — | `WHISPER_MODEL` | whisper 모델 파일 경로 (필수) |
| `--dtw` | 모델명에서 유도 | `ZITAT_DTW` | whisper DTW 프리셋 |
| `--no-dtw` | — | — | DTW 단어 타임스탬프 비활성 |
| `--whisper-max-len` | `0` | `ZITAT_WHISPER_MAX_LEN` | whisper `-ml`. `0`이면 비활성 |
| `--no-review` | — | — | 자막 검수 단계 건너뛰기 |
| `--keep-tmp` | — | — | 임시 파일 보존 (디버깅용) |

### 자막 길이 조절

기본값은 **한 줄에 30칸(한글 약 15자)** 이다. 자막이 너무 길거나 짧게 느껴지면:

```bash
# 더 짧고 빠르게 (쇼츠 스타일)
python zitat.py <url> --max-width 22 --min-cue-ms 600

# 더 길게, 전환은 적게
python zitat.py <url> --max-width 40
```

자막 **내용**은 맞는데 **나오는 시점**이 어긋난다면 `--max-width`가 아니라
`--max-cue-ms`를 낮춰야 한다. 번역문은 원문 큐 안에서 글자 수에 비례해 시간을
나눠 갖기 때문에, 원문 큐가 길수록 어긋남이 커진다.

```bash
python zitat.py <url> --max-cue-ms 3000
```

### 구 경계 분할

분할기는 공백 기준이라 그대로 두면 "경험을 쌓은 뒤에 한 / 두 번째 일은"처럼 수식어와
피수식어 사이가 큐 경계로 갈라질 수 있다. 그래서 5단계는 자르기 전에 claude를 한 번 더
불러 번역문의 자연스러운 구 경계에 `|`를 표시하게 하고, 표시된 지점에서만 자른다.

표시 결과는 검증을 통과해야만 쓰인다. `|`를 떼고 다시 합친 결과가 원문과 정확히
일치해야 하므로, 텍스트 변형이나 어절 중간 절단은 구조적으로 불가능하다. 검증에 실패한
줄과 claude 호출 실패는 해당 줄만 폭 기준 분할로 조용히 열화된다 — 최악의 경우가 이
기능이 없던 때의 동작이다.

claude 호출이 한 번 늘어난다. 원치 않으면 `--no-phrase-marks`.

### 자막 사이 간격

큐 끝은 마지막 단어에 맞춰 잘리기 때문에, 그대로 두면 진짜 묵음이 아닌 곳에서도
자막이 잠깐씩 깜빡인다. `--bridge-gap-ms` 이하의 간격은 앞 자막을 다음 자막
시작까지 유지해서 메우고, 그보다 긴 간격만 실제 공백으로 남긴다. 덤으로 읽을
시간이 늘어난다.

```bash
# 쉼의 리듬을 더 남김 (공백이 자주 보임)
python zitat.py <url> --bridge-gap-ms 800

# 메우지 않음 — 발화 구간에만 정확히 표시
python zitat.py <url> --bridge-gap-ms 0
```

247초 인터뷰 클립에서 측정한 값이다. 이 클립의 가장 긴 간격이 1630ms라, 임계값을
올릴수록 화면에 자막이 상시 떠 있는 쪽으로 간다.

| `--bridge-gap-ms` | 표시 시간 | 남는 공백 | 최장 유지 |
|---|---|---|---|
| `0` | 80.6% | 74곳 | — |
| `800` | 91.6% | 18곳 | 790ms |
| `1200` | 96.7% | 5곳 | 1190ms |
| **`1500`** (기본) | **98.3%** | **2곳** | 1390ms |

0.8~1.5초짜리 공백은 "쉼"보다 깜빡임으로 읽히기 때문에 기본값을 `1500`으로 잡았다.
공백을 리듬으로 쓰고 싶으면 `800`~`1200`이 적당하다.

### 예시

```bash
# 영상 처음 50초를 한국어 자막과 함께 추출
python zitat.py "https://youtu.be/j190mwiVlwA" -ss 0 -t 50 -o peter_test

# 1분 30초부터 2분간, 일본어로 번역
python zitat.py "https://youtu.be/j190mwiVlwA" -ss 1:30 -t 120 --lang Japanese

# 영상 전체를 다운로드해서 자막 입히기
python zitat.py "https://youtu.be/j190mwiVlwA"

# 중간 파일 확인하면서 디버깅
python zitat.py "https://youtu.be/j190mwiVlwA" -t 30 --keep-tmp
```

## 파이프라인

```
YouTube URL
  │
  ▼
[1] yt-dlp 다운로드 (1024px 이하, --download-sections로 구간 지정)
  │
  ▼
[2] ffmpeg 오디오 추출 (16kHz mono WAV)
  │
  ▼
[3] whisper-cli 자막 생성 (SRT + JSON)
      단어 타임스탬프로 무음 지점에서 절 단위 큐 생성
  │
  ▼
[4] claude CLI 자막 번역 (번호 매긴 텍스트 왕복, 타임코드는 원본 유지)
  │
  ▼
[5] 자막 재분할 (claude 구 경계 표시 → 표시 폭 기준, --no-split으로 건너뛰기)
  │
  ▼
[6] $EDITOR 자막 검수 (--no-review로 건너뛰기)
  │
  ▼
[7] ffmpeg 자막 입히기 (burn-in)
  │
  ▼
출력.mp4
```

### 타이밍이 정확한 이유

whisper의 세그먼트 종료 시각은 다음 세그먼트 시작까지 늘어져서, 자막이 침묵 구간에도
화면에 남는다. zitat은 대신 `-ojf`로 받은 **단어 단위 타임스탬프**를 써서 큐 끝을
마지막 단어의 종료 시각에 맞추고, 단어 사이 무음이 `--pause-gap-ms`를 넘으면 큐를 나눈다.

모델 파일명이 whisper의 DTW 프리셋과 일치하면 `-dtw`가 자동으로 켜진다
(`ggml-large-v3-turbo.bin` → `large.v3.turbo`). DTW 온셋이 whisper 기본 토큰
타임스탬프보다 정확한데, 특히 세그먼트 시작 부분에서 차이가 크다 — 실측에서 기본
타임스탬프는 첫 단어를 실제 발화보다 1초 이르게 배치했다.

DTW는 flash attention과 함께 쓸 수 없어서(whisper가 조용히 비활성화한다) zitat이
`-nfa`를 함께 넘긴다. 전사 시간이 약 30% 늘어난다. 원치 않으면 `--no-dtw`를 쓰면 되고,
그래도 단어 타임스탬프 기반 동작은 그대로 유지된다.

## 라이선스

MIT License. 자세한 내용은 [LICENSE](LICENSE) 파일을 참고하세요.
