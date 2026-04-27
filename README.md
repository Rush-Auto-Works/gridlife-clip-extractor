# GridLife clip extractor

Pulls per-class race sessions out of multi-hour GridLife broadcast streams.
Built for **Rush SR** (default) but also handles **GLTC** and **GLGT** —
they share the same broadcast overlay format, only the leading series word
differs.

## What this does

GridLife event broadcasts are usually 8-12 hours of mixed-class race coverage.
A given series' sessions appear on screen with a distinctive teal sidebar
in the upper-left, labelled e.g. `RUSH WARMUP`, `RUSH | RACE 2`,
`GLTC | RACE 1`, `GLGT | RACE 2`.

This toolset:

1. Downloads the day-by-day broadcast videos from the GridLife YouTube channel.
2. Scans each video, OCRing a small upper-left crop to detect when the
   target series' sidebar is on screen. One OCR pass detects all requested
   series at once.
3. Merges adjacent detections, trims commercials, and snips one clip per
   session (warmup / qualifying / race 1 / race 2 / etc.) using stream-copy.

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

# 3. Scan each day. Default is Rush only; pass --series for others.
for v in day*.webm; do ../scripts/find_w2w_races.py scan "$v"; done
# Or scan all three classes in one OCR pass:
for v in day*.webm; do ../scripts/find_w2w_races.py scan "$v" --series all; done

# 4. Snip per-session clips (joined per session by default)
for v in day*.webm; do ../scripts/find_w2w_races.py snip "$v"; done
# For multiple series, snip writes to clips/ instead of <series>_clips/
for v in day*.webm; do ../scripts/find_w2w_races.py snip "$v" --series all; done

# Output: rush_clips/dayN_rush_sessionXX_<LABEL>_<startSec>s.mkv
#         clips/dayN_<series>_sessionXX_<LABEL>_<startSec>s.mkv (multi-series)
```

Typical output for a three-day event with `--series all`:

```
day1_rush_session01_QUALIFYING_2625s.mkv
day2_rush_session01_WARMUP_4639s.mkv
day2_rush_session02_RACE_1_19283s.mkv
day2_rush_session03_RACE_2_31206s.mkv
day2_gltc_session01_PRACTICE_5437s.mkv
day2_gltc_session02_RACE_1_20844s.mkv
day2_gltc_session03_RACE_2_32713s.mkv
day2_glgt_session01_PRACTICE_5509s.mkv
day2_glgt_session02_RACE_1_14091s.mkv
day2_glgt_session03_RACE_2_34388s.mkv
day3_rush_session01_WARMUP_5150s.mkv
...
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

### `find_w2w_races.py scan <video>`

OCRs the full upper banner of every keyframe and matches it against the
detector regex(es) for the requested series. Also tracks GRIDLIFE banner
appearances so session starts can be back-extended to the prior commercial
break. Writes a sidecar per series: `<video>.<series>.json`.
Fast: ~30s per hour of 4K AV1 input on M-series for one series;
multi-series is the same speed (single OCR pass).

Tunables:

| Flag          | Default | Notes                                                       |
|---------------|---------|-------------------------------------------------------------|
| `--series`    | `rush`  | Comma-list of `rush,gltc,glgt`, or `all`                    |
| `--gap`       | 120     | Merge hits within this many seconds into one range          |
| `--pad-pre`   | 5       | Seconds prepended to each merged range (start mostly comes from banner trail) |
| `--pad-post`  | 15      | Seconds appended (last hit ≈ end of results recap)          |

### `find_w2w_races.py snip <video>`

Reads the per-series sidecar(s) and writes one stream-copied MKV per session.

Tunables:

| Flag             | Default               | Notes                                                |
|------------------|-----------------------|------------------------------------------------------|
| `--series`       | `rush`                | Same syntax as `scan`                                |
| `--out`          | `<series>_clips` or `clips` | Output directory; `clips/` when multi-series   |
| `--session-gap`  | 1800 (30min)          | Ranges further apart go in separate sessions         |
| `--no-join`      | (off)                 | Emit per-range files instead of joined session       |
| `--reencode`     | (off)                 | Re-encode for frame-accurate cuts (slow)             |

## How the heuristic works

1. **Keyframe-only decode.** `ffmpeg -skip_frame nokey` decodes only
   keyframes (typically every 3-5s). Avoids 99%+ of the decode work.
2. **Downscale + full-width top crop.** 4K → 1280×720, then crop the
   full upper banner (1280×250). The wider crop catches every overlay
   variant the broadcast uses for one session: in-race header
   (`<SERIES> | RACE N`), late-race "GAP TO LEADER" / "CHECKERED FLAG"
   view, RESULTS recap (smaller subheader), top-right "RUSH SERIES"
   sponsor badge, and WINNER panel.
3. **Tesseract** with PSM 6, matching the per-series detector regex
   on every cropped frame (with OCR slip tolerance).
4. **Classify** the surrounding text — `WARMUP`, `QUALIFYING`,
   `RACE_N`, `PRACTICE`. Race numbers come from `RACE\s*[|]?\s*(\d+)`.
5. **Track GRIDLIFE banner appearances** in the same OCR pass. The
   event banner is on screen during all live coverage but vanishes
   during commercials.
6. **Merge** adjacent hits within `--gap` seconds → range.
   Drop ranges with `<--min-hits` (default 3) — usually OCR slips on
   sponsor logos. Apply tiny asymmetric padding
   (`--pad-pre` / `--pad-post`). Stray ranges that overlap after pad
   get re-merged.
7. **Back-extend session start to the end of the prior commercial.**
   For each range, walk the start backward through the GRIDLIFE
   banner trail; stop at the first gap > 30s (= a real commercial
   break) or after 10min lookback. Captures pre-leaderboard out-laps
   for qualifying and grid-walk content for races.
8. **Group ranges into sessions** (`--session-gap`).
9. **Snip + concat** each session via stream copy.

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
from find_w2w_races import classify
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
