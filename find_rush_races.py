#!/usr/bin/env python3
"""
Detect Rush SR race segments in long webm captures.

Heuristic: GridLife broadcast graphics show a teal sidebar in the upper-left
labelled "RUSH QUALIFYING" or "RUSH RACE" while a Rush session is on screen.
We sample frames at a fixed interval, crop the upper-left header, OCR it,
and merge contiguous "RUSH" hits into time ranges (with a configurable gap
tolerance and padding).

Usage:
    ./find_rush_races.py scan day1.webm
    ./find_rush_races.py scan day1.webm --interval 10 --gap 120 --pad 60
    ./find_rush_races.py snip day1.webm   # uses ranges from prior scan
    ./find_rush_races.py scan day1.webm --resume   # continue interrupted scan

Outputs JSON sidecar at <video>.rush.json with detections + merged ranges.
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
CROP_W, CROP_H = 300, 120          # crop size after downscale (px)
SCALE_W, SCALE_H = 1280, 720       # downscale target before crop
DEFAULT_GAP = 120                  # merge intra-session hit gaps within this many seconds
DEFAULT_PAD_PRE = 60               # seconds prepended to each merged range
DEFAULT_PAD_POST = 180             # seconds appended (covers cooldown lap + results + highlights)
DEFAULT_SESSION_GAP = 1800         # ranges further apart than this are different sessions (30min)
TESSERACT_PSM = "6"
OCR_WORKERS = 24                   # parallel tesseract processes

# Match RUSH with common OCR slips (V/U, I/H, 0/O). Anchored to whole word.
RUSH_RE = re.compile(r"\bR[UV][SS5][HMNI]?\b", re.IGNORECASE)
# "RACE 1" / "RACE  3" — pipe sometimes OCRs as the digit or a separator.
RACE_NUM_RE = re.compile(r"RACE\s*[|l\s]*\s*(\d+)", re.IGNORECASE)


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
    """Decode only keyframes, cropped to header. ~100× faster than full decode."""
    vf = f"scale={SCALE_W}:{SCALE_H},crop={CROP_W}:{CROP_H}:0:0"
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
                 max_t: float) -> list:
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


def scan(video: Path, gap: int, pad_pre: int, pad_post: int):
    from concurrent.futures import ThreadPoolExecutor

    sidecar = video.with_suffix(video.suffix + ".rush.json")
    duration = video_duration(video)
    print(f"[scan] {video.name}: duration={fmt_ts(duration)} "
          f"gap={gap}s pad=-{pad_pre}/+{pad_post}s", file=sys.stderr)

    state = {"video": str(video), "duration": duration,
             "hits": [], "completed": False}

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
                if RUSH_RE.search(text):
                    label = classify(text)
                    state["hits"].append({"t": t, "text": text.strip(), "label": label})
                    print(f"  HIT  t={fmt_ts(t)}  label={label}", file=sys.stderr)
                elif os.environ.get("RUSH_DEBUG"):
                    print(f"  miss t={fmt_ts(t)} | {text.strip()[:60]!r}",
                          file=sys.stderr)
                if done % 200 == 0:
                    print(f"  ... {done}/{n} (t≈{fmt_ts(t)}, "
                          f"hits={len(state['hits'])})", file=sys.stderr)
                    sidecar.write_text(json.dumps(state, indent=2))

        state["completed"] = True
        hits = [Hit(**h) for h in state["hits"]]
        ranges = merge_ranges(hits, gap, pad_pre, pad_post, duration)
        state["ranges"] = [asdict(r) for r in ranges]
        sidecar.write_text(json.dumps(state, indent=2))
        print(f"\n[scan] done. {len(state['hits'])} hits → {len(ranges)} ranges",
              file=sys.stderr)
        for r in ranges:
            print(f"  RANGE {fmt_ts(r.start)} → {fmt_ts(r.end)}  "
                  f"({r.hits} hits, {','.join(r.labels)})", file=sys.stderr)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _snip_one(video: Path, r: dict, out_path: Path, reencode: bool):
    dur = r["end"] - r["start"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
           "-ss", fmt_ts(r["start"]), "-i", str(video),
           "-t", f"{dur:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-y", str(out_path)]
    print(f"[snip] {out_path.name}  ({fmt_ts(r['start'])} +{dur:.0f}s)",
          file=sys.stderr)
    subprocess.check_call(cmd)


def snip(video: Path, out_dir: Path, reencode: bool, join: bool,
         session_gap: int):
    sidecar = video.with_suffix(video.suffix + ".rush.json")
    if not sidecar.exists():
        sys.exit(f"no scan results at {sidecar}; run scan first")
    state = json.loads(sidecar.read_text())
    ranges = state.get("ranges", [])
    if not ranges:
        sys.exit("no ranges in scan result")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = video.stem
    sessions = group_into_sessions(ranges, session_gap)
    print(f"[snip] {len(ranges)} ranges → {len(sessions)} session(s) "
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
            name = (f"{base}_session{s_idx:02d}_part{r_idx:02d}"
                    f"_{labels_tag}_{int(r['start'])}s.mkv")
            out_path = out_dir / name
            _snip_one(video, r, out_path, reencode)
            clip_paths.append(out_path)

        if join:
            joined_name = (f"{base}_session{s_idx:02d}_{labels_tag}"
                           f"_{int(s_start)}s.mkv")
            joined_path = out_dir / joined_name
            if len(clip_paths) == 1:
                # Single range — just rename the part file
                clip_paths[0].rename(joined_path)
                print(f"[join] single-range session → {joined_path.name}",
                      file=sys.stderr)
            else:
                list_path = out_dir / f".{base}_session{s_idx:02d}_concat.txt"
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
                # Tidy: drop the per-part files now that the combined exists
                for p in clip_paths:
                    p.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan")
    sp.add_argument("video", type=Path)
    sp.add_argument("--gap", type=int, default=DEFAULT_GAP)
    sp.add_argument("--pad-pre", type=int, default=DEFAULT_PAD_PRE,
                    help="seconds prepended to each merged range")
    sp.add_argument("--pad-post", type=int, default=DEFAULT_PAD_POST,
                    help="seconds appended (covers cooldown lap + results + highlights)")

    sn = sub.add_parser("snip")
    sn.add_argument("video", type=Path)
    sn.add_argument("--out", type=Path, default=Path("rush_clips"))
    sn.add_argument("--reencode", action="store_true",
                    help="re-encode for frame-accurate cuts (slow); default is stream-copy")
    sn.add_argument("--no-join", dest="join", action="store_false",
                    help="emit individual range clips instead of one joined clip per session")
    sn.add_argument("--session-gap", type=int, default=DEFAULT_SESSION_GAP,
                    help="seconds of inactivity that mark a different session (default 1800 = 30min)")
    sn.set_defaults(join=True)

    args = ap.parse_args()
    if args.cmd == "scan":
        scan(args.video, args.gap, args.pad_pre, args.pad_post)
    elif args.cmd == "snip":
        snip(args.video, args.out, args.reencode, args.join, args.session_gap)


if __name__ == "__main__":
    main()
