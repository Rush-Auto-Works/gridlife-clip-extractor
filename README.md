# GridLife Rush SR — broadcast clip extractor

Tools for pulling Rush SR sessions out of multi-hour GridLife broadcast streams.

## What this does

GridLife event broadcasts are usually 8-12 hours of mixed-class race coverage.
The Rush SR sessions appear on screen with a distinctive teal sidebar in the
upper-left labelled `RUSH WARMUP`, `RUSH QUALIFYING`, or `RUSH | RACE N`.

This toolset:

1. Downloads the day-by-day broadcast videos from the GridLife YouTube channel.
2. Scans each video, OCRing a small upper-left crop to detect when the
   Rush sidebar is on screen.
3. Merges adjacent detections, trims commercials, and snips one clip per
   session (warmup, qualifying, race 1, race 2, etc.) using stream-copy.

## One-time setup

```bash
brew install ffmpeg yt-dlp tesseract
python3 -m venv /private/tmp/claude/venv   # any venv works; this one matches
                                           # the path used by /CLAUDE.md
```

(Hardware AV1 decode via VideoToolbox is currently broken in upstream ffmpeg
for these streams — the dav1d software decode is plenty fast on Apple Silicon
when combined with the `-skip_frame nokey` trick the script uses.)

## Per-event workflow

```bash
# 1. Make a folder for the event
mkdir "2026-05 GridLife Mid-Ohio"
cd    "2026-05 GridLife Mid-Ohio"

# 2. Find the streams on the channel and download
#    https://www.youtube.com/@Gridlife/streams
#    Look for 2+hr videos labelled by day and locale.
../scripts/download_gridlife_streams.sh --list                # browse recent
../scripts/download_gridlife_streams.sh URL1 URL2 URL3        # download N days

# 3. Scan each day for Rush sessions (one .json sidecar per video)
for v in day*.webm; do ../scripts/find_rush_races.py scan "$v"; done

# 4. Snip per-session clips (joined per session by default)
for v in day*.webm; do ../scripts/find_rush_races.py snip "$v"; done

# Output: rush_clips/dayN_sessionXX_<LABEL>_<startSec>s.mkv
```

Typical output for a three-day event:

```
day1_session01_QUALIFYING_2625s.mkv
day2_session01_WARMUP_4639s.mkv
day2_session02_RACE_1_19283s.mkv
day2_session03_RACE_2_31206s.mkv
day3_session01_WARMUP_5150s.mkv
day3_session02_RACE_3_16844s.mkv
day3_session03_RACE_4_25808s.mkv
```

## Scripts

### `download_gridlife_streams.sh`

```
download_gridlife_streams.sh URL1 [URL2 ...]   # download as day1.webm, day2.webm, ...
download_gridlife_streams.sh --list            # show 25 most recent streams from channel
download_gridlife_streams.sh --auto 3          # auto-download 3 most recent streams
```

Pulls the highest-quality WebM (AV1 + Opus) into the current directory.
Skips files that already exist.

### `find_rush_races.py scan <video>`

Detects RUSH sessions by OCRing the upper-left header on every keyframe.
Writes a JSON sidecar `<video>.rush.json` containing per-hit OCR text plus
merged time ranges. Fast: ~30s per hour of 4K AV1 input on M-series.

Tunables:

| Flag          | Default | Notes                                                   |
|---------------|---------|---------------------------------------------------------|
| `--gap`       | 120     | Merge hits within this many seconds into one range      |
| `--pad-pre`   | 60      | Seconds prepended to each merged range                  |
| `--pad-post`  | 180     | Seconds appended (covers cooldown lap + results screen) |

### `find_rush_races.py snip <video>`

Reads the sidecar from `scan` and writes one stream-copied MKV per session.

Tunables:

| Flag             | Default       | Notes                                          |
|------------------|---------------|------------------------------------------------|
| `--out`          | `rush_clips`  | Output directory                               |
| `--session-gap`  | 1800 (30min)  | Ranges further apart go in separate sessions   |
| `--no-join`      | (off)         | Emit per-range files instead of joined session |
| `--reencode`     | (off)         | Re-encode for frame-accurate cuts (slow)       |

## How the heuristic works

1. **Keyframe-only decode.** `ffmpeg -skip_frame nokey` decodes only
   keyframes (typically every 3-5s). Avoids 99%+ of the decode work.
2. **Crop + downscale before OCR.** 4K → 1280×720, then crop
   the top-left 300×120 pixels containing the sidebar header.
3. **Tesseract** with PSM 6, looking for `RUSH` (with OCR slip tolerance).
4. **Classify** the surrounding text — `WARMUP`, `QUALIFYING`,
   `RACE_N`, `PRACTICE`. Race numbers come from `RACE\s*[|]?\s*(\d+)`.
5. **Merge** adjacent hits within `--gap` seconds → range.
   Apply asymmetric padding (`--pad-pre` / `--pad-post`).
   Stray ranges (overlapping after pad) get re-merged.
6. **Group ranges into sessions** (`--session-gap`).
7. **Snip + concat** each session via stream copy.

Tesseract is "good enough" for the high-contrast broadcast graphic; for
lower-quality footage, swapping in Apple Vision (`pyobjc-framework-Vision`)
would cut OCR errors but the current detector is robust to most slips.

## Output naming convention

```
<videoBasename>_session<NN>_<LABEL>_<startSec>s.mkv
```

`startSec` is seconds from the beginning of the source video. Useful for
re-finding the segment in the original.

## Re-running with new defaults

The JSON sidecar contains all OCR hits plus the computed ranges. If you
change defaults (e.g. `--pad-post`) and re-run `scan`, the sidecar is
overwritten with new ranges. If you only changed `snip` flags
(`--session-gap`, `--no-join`), no re-scan is needed.

To re-classify hits without re-scanning (e.g. after updating the
`classify()` regex):

```python
import json
from find_rush_races import classify
from collections import Counter
d = json.loads(open('day1.webm.rush.json').read())
for h in d['hits']:
    h['label'] = classify(h['text'])
for r in d['ranges']:
    rls = [classify(h['text']) for h in d['hits']
           if r['start']-200 <= h['t'] <= r['end']+200]
    nu = Counter(l for l in rls if l != 'UNKNOWN')
    r['labels'] = [nu.most_common(1)[0][0]] if nu else ['UNKNOWN']
open('day1.webm.rush.json', 'w').write(json.dumps(d, indent=2))
```
