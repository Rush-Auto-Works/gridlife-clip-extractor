# Claude Code instructions — GridLife clip extractor

You are working in `GridLife Photo & Video/`. Each event has its own
subfolder named `yyyy-mm GridLife <track>` containing the raw broadcast
videos as `dayN.webm`. The reusable tooling lives in `scripts/` (a clone
of the `gridlife-clip-extractor` repo on GitHub).

Default series is `rush`. The same tooling also handles `gltc` and
`glgt` — same overlay format, different leading word. Pass `--series`
to scan/snip for those, or `--series all` to do them in a single OCR
pass and emit per-series sidecars.

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
   for v in day*.webm; do ../scripts/find_w2w_races.py scan "$v"; done
   ```
   Each video gets a `<video>.rush.json` sidecar with all detections.
   Expect ~30s per hour of 4K AV1 input on Apple Silicon. Add
   `--series all` for rush+gltc+glgt at the same speed (one OCR pass).

3. **Snip per-session clips.**
   ```bash
   for v in day*.webm; do ../scripts/find_w2w_races.py snip "$v"; done
   ```
   Output lands in `rush_clips/`, one MKV per session
   (warmup, qualifying, race 1..N), stream-copied — no re-encode.

## When something looks off

| Symptom                              | Likely fix                                     |
|--------------------------------------|-----------------------------------------------|
| Race ends too early                  | Bump `scan --pad-post` (default 15)            |
| Race starts too early (commercials)  | Lower `--commercial-gap` is hardcoded; tune in script (default 30s gap = commercial) |
| Race starts too late                 | Likely the GRIDLIFE banner OCR is missing some hits — check the sidecar's `gridlife_times` |
| Two adjacent races get joined        | Lower `snip --session-gap` (def 1800)          |
| One race split into two clips        | Raise `scan --gap` (default 120)               |
| Filename says `UNKNOWN`              | Add the new keyword to `classify()`            |
| Filename has wrong race number       | OCR slip — confirmed by inspecting JSON        |
| Stray short range with sponsor-logo hit | Raise `--min-hits` (default 3)              |

After tweaking `classify()` only (no scan params), you don't need to
re-scan — see README "Re-running with new defaults" for the in-place
re-classify snippet.

## How the start/end is decided (subtle, easy to break)

The full-width 1280×250 top crop is what catches every overlay variant
the broadcast switches between during one session: in-race header, the
"GAP TO LEADER" / "CHECKERED FLAG" view, the RESULTS recap with its
smaller subheader, the top-right RUSH SERIES sponsor badge, and the
WINNER panel. Don't shrink the crop without re-checking that all of
these still produce hits.

The session **start** is back-extended through the GRIDLIFE banner
trail. Banner appears during all live coverage but vanishes during
commercials. We walk back from the first series hit through banner
appearances and stop at a >30s gap (= commercial). This gets us back
to where the prior commercial ended — the actual session start, not
just the leaderboard appearance.

The session **end** uses tiny pad_post (15s) because the broader crop
already catches the RESULTS recap. The recap shows the smaller
"<series> | RACE N" subheader, which our detector picks up.

If the GRIDLIFE detector ever stops working (e.g. broadcast rebrands),
the start back-extension silently degrades to pad_pre and clips will
miss the warm-up. Watch for that.

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

## Tunables in `find_w2w_races.py`

```python
CROP_W, CROP_H = 1280, 250         # full top band — catches every overlay variant
SCALE_W, SCALE_H = 1280, 720       # downscale before crop
DEFAULT_GAP = 120                  # merge intra-session hit gaps
DEFAULT_PAD_PRE = 5                # tiny — start mostly comes from GRIDLIFE banner trail
DEFAULT_PAD_POST = 15              # tiny — last hit ≈ end of results recap
DEFAULT_SESSION_GAP = 1800         # 30min — separates true sessions
DEFAULT_MIN_HITS = 3               # drop sub-threshold ranges (sponsor-logo OCR slips)
DEFAULT_COMMERCIAL_GAP = 30        # GRIDLIFE banner gap > this → commercial break
DEFAULT_MAX_LOOKBACK = 600         # cap session-start back-extension at 10min
OCR_WORKERS = 24                   # parallel tesseract threads
```

The 24-worker default suits an M5 Max. On smaller chips, drop it to the
P-core count.

## File outputs you should expect

For a typical 3-day event:

```
day1.webm                                                  (raw, ~10-15 GB)
day2.webm
day3.webm
day1.webm.rush.json                                        (scan sidecar)
day2.webm.rush.json
day3.webm.rush.json
rush_clips/
    day1_rush_session01_QUALIFYING_NNNNs.mkv
    day2_rush_session01_WARMUP_NNNNs.mkv
    day2_rush_session02_RACE_1_NNNNs.mkv
    day2_rush_session03_RACE_2_NNNNs.mkv
    day3_rush_session01_WARMUP_NNNNs.mkv
    day3_rush_session02_RACE_3_NNNNs.mkv
    day3_rush_session03_RACE_4_NNNNs.mkv
```

With `--series all`, sidecars are `<video>.<series>.json` (rush, gltc,
glgt) and clips land in `clips/` with `<base>_<series>_session...` names.

`NNNN` is the start time in seconds from the source video — handy for
re-finding the segment in the original.
