# Claude Code instructions — GridLife Rush SR clip extractor

You are working in `GridLife Photo & Video/`. Each event has its own
subfolder named `yyyy-mm GridLife <track>` containing the raw broadcast
videos as `dayN.webm`. The reusable tooling lives in `scripts/`.

## Standard workflow when the user has new raw footage

1. **Confirm the videos.** Check that `dayN.webm` files exist in the
   event folder. If missing, the user can:
   ```bash
   ../scripts/download_gridlife_streams.sh URL1 URL2 URL3
   ```
   GridLife streams live at https://www.youtube.com/@Gridlife/streams,
   labelled by day and locale (CMP, Mid-Ohio, etc.).

2. **Scan each video.**
   ```bash
   for v in day*.webm; do ../scripts/find_rush_races.py scan "$v"; done
   ```
   Each video gets a `<video>.rush.json` sidecar with all detections.
   Expect ~30s per hour of 4K AV1 input on Apple Silicon.

3. **Snip per-session clips.**
   ```bash
   for v in day*.webm; do ../scripts/find_rush_races.py snip "$v"; done
   ```
   Output lands in `rush_clips/`, one MKV per session
   (warmup, qualifying, race 1..N), stream-copied — no re-encode.

## When something looks off

| Symptom                              | Likely fix                              |
|--------------------------------------|-----------------------------------------|
| Race ends too early                  | Bump `scan --pad-post` (default 180)    |
| Two adjacent races get joined        | Lower `snip --session-gap` (def 1800)   |
| One race split into two clips        | Raise `scan --gap` (default 120)        |
| Filename says `UNKNOWN`              | Add the new keyword to `classify()`     |
| Filename has wrong race number       | OCR slip — confirmed by inspecting JSON |

After tweaking `classify()` only (no scan params), you don't need to
re-scan — see README "Re-running with new defaults" for the in-place
re-classify snippet.

## Sandbox quirks (don't get bitten again)

📌 `tempfile.mkdtemp()` under the Claude Code sandbox returns
`/tmp/claude-501/...` which is **not visible to subprocesses** (tesseract,
ffmpeg). The script pins its tempdir to `/private/tmp/claude/` for that
reason. Don't change it back.

📌 `mkdir` and writes outside the current event folder are blocked by
the default sandbox. To write to `scripts/` from inside an event folder,
pass `dangerouslyDisableSandbox: true` (the user already authorized this
when setting up `scripts/`).

📌 Hardware AV1 decode (`-hwaccel videotoolbox`) returns
"VideoToolbox malfunction" on this build of ffmpeg even though the
M-series chip supports it. The dav1d software decoder + `-skip_frame nokey`
gets us within a few seconds per hour of input — fast enough that hwdec
isn't worth chasing.

## Python environment

The script needs only stdlib + ffmpeg + tesseract on PATH. Use
`/private/tmp/claude/venv/bin/python` if it exists; any Python 3.10+
works. Don't `pip install` globally — the script has no third-party deps.

## Tunables in `find_rush_races.py`

```python
CROP_W, CROP_H = 300, 120          # crop size after downscale
SCALE_W, SCALE_H = 1280, 720       # downscale before crop
DEFAULT_GAP = 120                  # merge intra-session hit gaps
DEFAULT_PAD_PRE = 60               # seconds prepended to each range
DEFAULT_PAD_POST = 180             # seconds appended (cooldown + results)
DEFAULT_SESSION_GAP = 1800         # 30min — separates true sessions
OCR_WORKERS = 24                   # parallel tesseract threads
```

The 24-worker default suits an M5 Max. On smaller chips, drop it to the
P-core count.

## File outputs you should expect

For a typical 3-day event:

```
day1.webm                               (raw, ~10-15 GB)
day2.webm
day3.webm
day1.webm.rush.json                     (scan sidecar)
day2.webm.rush.json
day3.webm.rush.json
rush_clips/
    day1_session01_QUALIFYING_NNNNs.mkv
    day2_session01_WARMUP_NNNNs.mkv
    day2_session02_RACE_1_NNNNs.mkv
    day2_session03_RACE_2_NNNNs.mkv
    day3_session01_WARMUP_NNNNs.mkv
    day3_session02_RACE_3_NNNNs.mkv
    day3_session03_RACE_4_NNNNs.mkv
```

`NNNN` is the start time in seconds from the source video — handy for
re-finding the segment in the original.
