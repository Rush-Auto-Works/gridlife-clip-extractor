#!/usr/bin/env python3
"""
Detect GridLife race-class segments in long broadcast webm captures.

Heuristic: GridLife broadcast graphics show a teal sidebar in the upper-left
labelled "<SERIES> WARMUP" / "<SERIES> QUALIFYING" / "<SERIES> | RACE N"
while a session of that class is on screen. Supported series: rush (default),
gltc, glgt, trackbattle. We OCR every keyframe, match the per-series detector regex,
and merge contiguous hits into time ranges.

Usage:
    ./find_w2w_races.py scan day1.webm                     # rush only (default)
    ./find_w2w_races.py scan day1.webm --series gltc
    ./find_w2w_races.py scan day1.webm --series rush,gltc,glgt   # one OCR pass, three sidecars
    ./find_w2w_races.py scan day1.webm --series all              # same
    ./find_w2w_races.py snip day1.webm                     # rush
    ./find_w2w_races.py snip day1.webm --series gltc

Each scan writes a sidecar at <video>.<series>.json containing per-hit
OCR text plus merged time ranges. Snipping reads that sidecar.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path

# --- Tunables ---
# Crop covers the full upper banner so we catch every overlay variant the
# broadcast uses for one session: the in-race header (<SERIES> | RACE N),
# the late-race GAP TO LEADER / CHECKERED FLAG view, the RESULTS recap
# (which has a smaller <SERIES> | RACE N subheader top-left), the
# top-right RUSH SERIES sponsor badge, and the WINNER panel.
CROP_W, CROP_H = 1280, 250         # crop size after downscale — full top band
SCALE_W, SCALE_H = 1280, 720       # downscale target before crop
DEFAULT_GAP = 120                  # merge intra-session hit gaps within this many seconds
DEFAULT_PAD_PRE = 5                # tiny pre-pad — start is back-extended via GRIDLIFE banner
DEFAULT_PAD_POST = 15              # tiny post-pad — last hit ≈ end of results recap
DEFAULT_SESSION_GAP = 1800         # ranges further apart than this are different sessions (30min)
DEFAULT_MIN_HITS = 3               # drop ranges with fewer hits — usually OCR slips on sponsor logos
DEFAULT_COMMERCIAL_GAP = 30        # absence of GRIDLIFE banner this long → that's a commercial break
DEFAULT_MAX_LOOKBACK = 600         # cap session-start back-extension at 10min
TESSERACT_PSM = "6"
OCR_WORKERS = 24                   # parallel tesseract processes

# Series detectors. Each maps a CLI name → compiled regex matching the
# leading series word in the broadcast overlay. All series share the same
# overlay format ("<SERIES> | RACE 1", "<SERIES> WARMUP", etc.) so the same
# classify() works across them — only the first word changes.
SERIES = {
    "rush": re.compile(r"\bR[UV][SS5][HMNI]?\b", re.IGNORECASE),
    "gltc": re.compile(r"\bGL[TY]C\b", re.IGNORECASE),
    "glgt": re.compile(r"\bGLGT\b", re.IGNORECASE),
    # Matches qualifier sessions ("TRACKBATTLE Q1" etc.) AND the final
    # "PODIUM SPRINT" session, which drops the TRACKBATTLE prefix entirely.
    # No trailing \b — OCR frequently merges "TRACKBATTLE Q3" → "TRACKBATTLEQ3"
    # or "TRACKBATTLEO3" (Q misread as O), so a word-boundary after E would miss them.
    "trackbattle": re.compile(r"\bTRACKBATTLE|\bPODIUM\s*SPRINT\b", re.IGNORECASE),
}

# The "GRIDLIFE" event banner is on screen during *all* live broadcast
# coverage (cars on track, intros, lower thirds) but disappears during
# commercial breaks. We use its presence to back-extend a session start
# from the first series-leaderboard hit to the end of the previous
# commercial break — covering pre-leaderboard out-laps for qualifying
# and grid-walk content for races.
GRIDLIFE_RE = re.compile(r"GR[1IL][DOQ]L[1IL]F[E]?", re.IGNORECASE)

# "RACE 1" / "RACE  3" — pipe sometimes OCRs as the digit or a separator.
RACE_NUM_RE = re.compile(r"RACE\s*[|l\s]*\s*(\d+)", re.IGNORECASE)


def parse_series(arg: str) -> list:
    """Parse --series flag value (comma-list or 'all') into ordered list of names."""
    if arg.lower() == "all":
        return list(SERIES)
    out = []
    for name in arg.split(","):
        name = name.strip().lower()
        if name not in SERIES:
            raise SystemExit(f"unknown series '{name}'; choices: "
                             f"{', '.join(SERIES)} (or 'all')")
        if name not in out:
            out.append(name)
    return out


def sidecar_for(video: Path, series: str) -> Path:
    return video.with_suffix(video.suffix + f".{series}.json")


@dataclass
class Hit:
    t: float            # timestamp seconds
    text: str           # raw OCR text
    label: str          # detected label (QUALIFYING/RACE/UNKNOWN)


@dataclass
class Range:
    start: float
    end: float
    hits: int
    labels: list


def fmt_ts(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


def classify(text: str) -> str:
    """Return the session-kind label, including RACE_N when a number is present."""
    t = text.upper()
    if "QUAL" in t:
        return "QUALIFYING"
    if "RACE" in t:
        m = RACE_NUM_RE.search(t)
        if m:
            return f"RACE_{int(m.group(1))}"
        return "RACE"
    if "WARM" in t:
        return "WARMUP"
    if "PRACT" in t:
        return "PRACTICE"
    if "PODIUM" in t or "SPRINT" in t:
        return "PODIUM_SPRINT"
    m = re.search(r"\bQ(\d+)\b", t)
    if m:
        return f"Q{int(m.group(1))}"
    return "UNKNOWN"


def video_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], text=True).strip()
    return float(out)


def keyframe_times(video: Path) -> list:
    """Return list of keyframe timestamps (seconds) for the primary video stream."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=print_section=0", str(video)
    ], text=True)
    times = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return times


def extract_keyframes(video: Path, out_dir: Path):
    """Decode only keyframes, cropped to header. ~100× faster than full decode.

    -skip_frame nokey hints the decoder to skip non-keyframes (works well for
    AV1/dav1d). The select filter is a belt-and-suspenders guard for codecs
    (e.g. VP9) where -skip_frame nokey is ignored by the decoder — without it
    every frame would be output and the frame→timestamp pairing would break.
    """
    vf = f"select='eq(pict_type\\,I)',scale={SCALE_W}:{SCALE_H},crop={CROP_W}:{CROP_H}:0:0"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-skip_frame", "nokey",
        "-i", str(video),
        "-fps_mode", "vfr",
        "-vf", vf,
        "-y",
        str(out_dir / "k_%07d.png"),
    ]
    return subprocess.Popen(cmd)


def ocr_frame(path: Path) -> str:
    try:
        r = subprocess.run(
            ["tesseract", str(path), "-", "--psm", TESSERACT_PSM],
            capture_output=True, check=False
        )
        if r.returncode != 0 and os.environ.get("RUSH_DEBUG"):
            print(f"  [tesseract rc={r.returncode}] {r.stderr[:200]!r}",
                  file=sys.stderr)
            return ""
        try:
            return r.stdout.decode("utf-8")
        except UnicodeDecodeError:
            if os.environ.get("RUSH_DEBUG"):
                print(f"  [ocr non-utf8 stdout] file={path} "
                      f"bytes_len={len(r.stdout)} "
                      f"head={r.stdout[:40]!r}", file=sys.stderr)
            return r.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        if os.environ.get("RUSH_DEBUG"):
            print(f"  [ocr exception] {e}", file=sys.stderr)
        return ""


def merge_ranges(hits: list, gap: int, pad_pre: int, pad_post: int,
                 max_t: float, min_hits: int = DEFAULT_MIN_HITS) -> list:
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h.t)
    ranges = []
    cur = Range(start=hits[0].t, end=hits[0].t, hits=1, labels=[hits[0].label])
    for h in hits[1:]:
        if h.t - cur.end <= gap:
            cur.end = h.t
            cur.hits += 1
            cur.labels.append(h.label)
        else:
            ranges.append(cur)
            cur = Range(start=h.t, end=h.t, hits=1, labels=[h.label])
    ranges.append(cur)
    # Drop tiny ranges — usually false positives from sponsor logos containing
    # the series word elsewhere in the broadcast.
    ranges = [r for r in ranges if r.hits >= min_hits]
    if not ranges:
        return []
    for r in ranges:
        r.start = max(0.0, r.start - pad_pre)
        r.end = min(max_t, r.end + pad_post)
        # Pick the dominant label (most common, ignoring UNKNOWN if anything else exists).
        counts = Counter(r.labels)
        non_unknown = {k: v for k, v in counts.items() if k != "UNKNOWN"}
        if non_unknown:
            primary = max(non_unknown, key=lambda k: non_unknown[k])
            r.labels = [primary]
        else:
            r.labels = ["UNKNOWN"]
    merged = [ranges[0]]
    for r in ranges[1:]:
        if r.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, r.end)
            merged[-1].hits += r.hits
            merged[-1].labels = sorted(set(merged[-1].labels) | set(r.labels))
        else:
            merged.append(r)
    return merged


def back_extend_to_commercial_end(ranges: list, gridlife_times: list,
                                   commercial_gap: int = DEFAULT_COMMERCIAL_GAP,
                                   max_lookback: int = DEFAULT_MAX_LOOKBACK) -> list:
    """For each merged range, walk the start backward through GRIDLIFE-banner
    appearances. Stop when we hit a gap longer than commercial_gap (= a
    commercial break) or after max_lookback seconds. The new start is the
    earliest banner appearance in that connected-back-to-our-session chain."""
    if not gridlife_times or not ranges:
        return ranges
    gtimes = sorted(gridlife_times)
    for r in ranges:
        original_start = r.start
        floor_t = max(0.0, original_start - max_lookback)
        # GRIDLIFE banner appearances from the lookback window up to range start
        candidates = [t for t in gtimes if floor_t <= t < original_start]
        if not candidates:
            continue
        # Walk backward: keep extending start as long as adjacent GRIDLIFE
        # appearances are within commercial_gap of each other.
        new_start = original_start
        for t in reversed(candidates):
            if new_start - t <= commercial_gap:
                new_start = t
            else:
                break
        r.start = new_start
    return ranges


def group_into_sessions(ranges: list, session_gap: int) -> list:
    """Cluster merged ranges into sessions (gaps > session_gap mark a new session)."""
    if not ranges:
        return []
    sessions = [[ranges[0]]]
    for r in ranges[1:]:
        if r["start"] - sessions[-1][-1]["end"] > session_gap:
            sessions.append([r])
        else:
            sessions[-1].append(r)
    return sessions


def _ocr_one(args):
    idx, t, fp = args
    text = ocr_frame(fp)
    return idx, t, text


def scan(video: Path, series_list: list, gap: int, pad_pre: int, pad_post: int):
    from concurrent.futures import ThreadPoolExecutor

    duration = video_duration(video)
    print(f"[scan] {video.name}: duration={fmt_ts(duration)} "
          f"series={','.join(series_list)} "
          f"gap={gap}s pad=-{pad_pre}/+{pad_post}s", file=sys.stderr)

    states = {s: {"video": str(video), "series": s, "duration": duration,
                  "hits": [], "gridlife_times": [], "completed": False}
              for s in series_list}
    gridlife_times = []  # shared, copied to each state at end

    tmp_root = Path("/private/tmp/claude")
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="rush_scan_", dir=str(tmp_root)))
    try:
        t0 = time.time()
        print(f"[scan] probing keyframe timestamps ...", file=sys.stderr)
        kf_times = keyframe_times(video)
        print(f"[scan] {len(kf_times)} keyframes "
              f"(avg {duration/max(1,len(kf_times)):.1f}s apart)",
              file=sys.stderr)

        print(f"[scan] decoding keyframes → {tmpdir} ...", file=sys.stderr)
        proc = extract_keyframes(video, tmpdir)
        proc.wait()
        if proc.returncode != 0:
            sys.exit(f"ffmpeg failed (rc={proc.returncode})")
        frames = sorted(tmpdir.glob("k_*.png"))
        if len(frames) != len(kf_times):
            print(f"[scan] WARN frames={len(frames)} != kf_times={len(kf_times)}; "
                  f"using min length", file=sys.stderr)
        n = min(len(frames), len(kf_times))
        print(f"[scan] decoded {n} frames in {time.time()-t0:.0f}s; "
              f"OCRing with {OCR_WORKERS} workers ...", file=sys.stderr)

        tasks = [(i + 1, kf_times[i], frames[i]) for i in range(n)]
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
            for done, (idx, t, text) in enumerate(ex.map(_ocr_one, tasks), 1):
                # Track when the GRIDLIFE event banner is on screen — used to
                # back-extend session starts to the end of the prior commercial.
                if GRIDLIFE_RE.search(text):
                    gridlife_times.append(t)
                # Check each requested series against the same OCR text.
                for s in series_list:
                    if SERIES[s].search(text):
                        label = classify(text)
                        states[s]["hits"].append({
                            "t": t, "text": text.strip(), "label": label
                        })
                        print(f"  HIT  [{s.upper()}] t={fmt_ts(t)}  label={label}",
                              file=sys.stderr)
                if done % 200 == 0:
                    counts = " ".join(f"{s}={len(states[s]['hits'])}"
                                      for s in series_list)
                    print(f"  ... {done}/{n} (t≈{fmt_ts(t)}, {counts}, "
                          f"gridlife={len(gridlife_times)})", file=sys.stderr)
                    for s in series_list:
                        states[s]["gridlife_times"] = gridlife_times
                        sidecar_for(video, s).write_text(
                            json.dumps(states[s], indent=2))

        for s in series_list:
            st = states[s]
            st["completed"] = True
            st["gridlife_times"] = gridlife_times
            hits = [Hit(**h) for h in st["hits"]]
            ranges = merge_ranges(hits, gap, pad_pre, pad_post, duration)
            ranges = back_extend_to_commercial_end(ranges, gridlife_times)
            st["ranges"] = [asdict(r) for r in ranges]
            sidecar_for(video, s).write_text(json.dumps(st, indent=2))
            print(f"\n[scan] {s}: {len(st['hits'])} hits → {len(ranges)} ranges",
                  file=sys.stderr)
            for r in ranges:
                print(f"  [{s.upper()}] RANGE {fmt_ts(r.start)} → {fmt_ts(r.end)}  "
                      f"({r.hits} hits, {','.join(r.labels)})", file=sys.stderr)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _snip_one(video: Path, r: dict, out_path: Path, reencode: bool,
              aac_audio: bool = False):
    dur = r["end"] - r["start"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
           "-ss", fmt_ts(r["start"]), "-i", str(video),
           "-t", f"{dur:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k"]
    elif aac_audio:
        # AV1 video copy + AAC audio re-encode (universal MP4 audio compat).
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-y", str(out_path)]
    print(f"[snip] {out_path.name}  ({fmt_ts(r['start'])} +{dur:.0f}s)",
          file=sys.stderr)
    subprocess.check_call(cmd)


def snip(video: Path, series: str, out_dir: Path, reencode: bool, join: bool,
         session_gap: int, container: str = "mkv", aac_audio: bool = False):
    sidecar = sidecar_for(video, series)
    if not sidecar.exists():
        sys.exit(f"no scan results at {sidecar}; run scan first")
    state = json.loads(sidecar.read_text())
    ranges = state.get("ranges", [])
    if not ranges:
        print(f"[snip] {series}: no ranges found, nothing to snip",
              file=sys.stderr)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    base = video.stem
    sessions = group_into_sessions(ranges, session_gap)
    print(f"[snip] {series}: {len(ranges)} ranges → {len(sessions)} session(s) "
          f"(session-gap={session_gap}s)", file=sys.stderr)

    hits = state.get("hits", [])

    for s_idx, session in enumerate(sessions, 1):
        # Dominant label across all hits inside the session (handles OCR slips
        # where a single misread could otherwise push a stray RACE_2 into a
        # RACE_3 session's filename).
        s_lo = session[0]["start"]
        s_hi = session[-1]["end"]
        in_session = [classify(h["text"]) for h in hits if s_lo <= h["t"] <= s_hi]
        non_unknown = Counter(l for l in in_session if l != "UNKNOWN")
        labels_tag = (non_unknown.most_common(1)[0][0]
                      if non_unknown else "UNKNOWN")
        s_start = session[0]["start"]
        clip_paths = []
        for r_idx, r in enumerate(session, 1):
            name = (f"{base}_{series}_session{s_idx:02d}_part{r_idx:02d}"
                    f"_{labels_tag}_{int(r['start'])}s.{container}")
            out_path = out_dir / name
            _snip_one(video, r, out_path, reencode, aac_audio)
            clip_paths.append(out_path)

        if join:
            joined_name = (f"{base}_{series}_session{s_idx:02d}_{labels_tag}"
                           f"_{int(s_start)}s.{container}")
            joined_path = out_dir / joined_name
            if len(clip_paths) == 1:
                clip_paths[0].rename(joined_path)
                print(f"[join] single-range session → {joined_path.name}",
                      file=sys.stderr)
            else:
                list_path = out_dir / f".{base}_{series}_session{s_idx:02d}_concat.txt"
                list_path.write_text(
                    "".join(f"file '{p.resolve()}'\n" for p in clip_paths)
                )
                print(f"[join] concatenating {len(clip_paths)} clips → "
                      f"{joined_path.name}", file=sys.stderr)
                subprocess.check_call([
                    "ffmpeg", "-hide_banner", "-loglevel", "warning",
                    "-f", "concat", "-safe", "0", "-i", str(list_path),
                    "-c", "copy", "-y", str(joined_path),
                ])
                list_path.unlink(missing_ok=True)
                for p in clip_paths:
                    p.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan")
    sp.add_argument("video", type=Path)
    sp.add_argument("--series", default="rush",
                    help="comma-list of rush/gltc/glgt/trackbattle, or 'all' (default: rush)")
    sp.add_argument("--gap", type=int, default=DEFAULT_GAP)
    sp.add_argument("--pad-pre", type=int, default=DEFAULT_PAD_PRE,
                    help="seconds prepended to each merged range")
    sp.add_argument("--pad-post", type=int, default=DEFAULT_PAD_POST,
                    help="seconds appended (covers cooldown lap + results + highlights)")

    sn = sub.add_parser("snip")
    sn.add_argument("video", type=Path)
    sn.add_argument("--series", default="rush",
                    help="comma-list of rush/gltc/glgt/trackbattle, or 'all' (default: rush)")
    sn.add_argument("--out", type=Path, default=None,
                    help="output directory (default: <series>_clips, or 'clips' for multi)")
    sn.add_argument("--reencode", action="store_true",
                    help="re-encode for frame-accurate cuts (slow); default is stream-copy")
    sn.add_argument("--no-join", dest="join", action="store_false",
                    help="emit individual range clips instead of one joined clip per session")
    sn.add_argument("--session-gap", type=int, default=DEFAULT_SESSION_GAP,
                    help="seconds of inactivity that mark a different session (default 1800 = 30min)")
    sn.add_argument("--container", choices=["mp4", "mkv", "webm"], default="mp4",
                    help="output container (default: mp4 — most compatible)")
    sn.add_argument("--aac-audio", action="store_true",
                    help="re-encode audio to AAC (helps Opus-in-MP4 compatibility for "
                         "QuickTime / iOS / older players); video stays AV1 stream-copy")
    sn.set_defaults(join=True)

    args = ap.parse_args()
    if args.cmd == "scan":
        series_list = parse_series(args.series)
        scan(args.video, series_list, args.gap, args.pad_pre, args.pad_post)
    elif args.cmd == "snip":
        series_list = parse_series(args.series)
        for s in series_list:
            out = args.out or Path(
                f"{s}_clips" if len(series_list) == 1 else "clips"
            )
            snip(args.video, s, out, args.reencode, args.join, args.session_gap,
                 args.container, args.aac_audio)


if __name__ == "__main__":
    main()
