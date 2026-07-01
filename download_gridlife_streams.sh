#!/usr/bin/env bash
# Download GridLife event broadcast streams from YouTube using yt-dlp.
#
# Streams live at: https://www.youtube.com/@Gridlife/streams
# Each event weekend usually publishes 2-4 multi-hour videos labelled by
# day and locale (e.g. "GridLife CMP 2026 Day 1", "GridLife Midwest Day 2").
#
# Usage:
#   download_gridlife_streams.sh <url-or-id> [<url-or-id> ...]
#   download_gridlife_streams.sh --list           # show recent streams from the channel
#   download_gridlife_streams.sh --auto N         # download the N most recent streams
#
# Files land in $PWD as dayN.webm (named in the order given on the command line).
# Use --start N to begin numbering at a day other than 1 (e.g. when re-downloading
# a later day after day1 already exists).
# Run this from inside the event's folder, e.g.
#   cd "2026-04 GridLife CMP" && ../scripts/download_gridlife_streams.sh URL1 URL2 URL3
#   cd "2026-04 GridLife CMP" && ../scripts/download_gridlife_streams.sh --start 2 URL2
#
# Defaults to the best available format (typically 4K AV1/VP9 + Opus). After
# each download we verify the file integrity with ffprobe — if the duration
# doesn't match what YouTube reported (e.g. resume corruption), the file is
# deleted and re-downloaded fresh. Override with FORMAT env var if you want
# to cap quality (e.g. FORMAT='bv*[height<=1080]+ba/b[height<=1080]').
#
set -euo pipefail

CHANNEL="https://www.youtube.com/@Gridlife/streams"

if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "yt-dlp not found. Install with: brew install yt-dlp" >&2
    exit 1
fi

START=1
case "${1:-}" in
    "")
        sed -n '2,17p' "$0" | sed 's/^# *//'
        exit 0
        ;;
    --start)
        START="${2:?'--start requires a number'}"
        shift 2
        ;;
    --list)
        # List the channel's recent streams (id + title) without downloading
        yt-dlp --flat-playlist --print "%(id)s  %(duration_string)s  %(title)s" \
               --playlist-end 25 "$CHANNEL"
        exit 0
        ;;
    --auto)
        shift
        n="${1:-3}"
        echo "Fetching $n most recent streams from $CHANNEL ..."
        mapfile -t URLS < <(yt-dlp --flat-playlist --print "https://youtube.com/watch?v=%(id)s" \
                                   --playlist-end "$n" "$CHANNEL")
        set -- "${URLS[@]}"
        ;;
esac

# Manually merge the separate video+audio streams that yt-dlp leaves behind
# when its own postprocessor fails (which it sometimes does after a partial
# download was resumed and finished). Returns 0 on success.
manual_merge() {
    local out="$1"
    # yt-dlp names them day<N>.f<vfmt>.webm + day<N>.f<afmt>.webm
    local vid afid
    vid=$(ls -1 "${out%.webm}".f*.webm 2>/dev/null | head -1)
    [[ -z "$vid" ]] && return 1
    # Heuristic: video stream is the larger of the two fNNN files
    local audio
    audio=$(ls -1S "${out%.webm}".f*.webm 2>/dev/null | tail -1)
    [[ "$vid" == "$audio" ]] && return 1
    local video
    video=$(ls -1S "${out%.webm}".f*.webm 2>/dev/null | head -1)
    echo "[merge] $out  ←  $video + $audio (manual ffmpeg)"
    if ffmpeg -hide_banner -loglevel warning \
            -i "$video" -i "$audio" \
            -map 0:v -map 1:a -c copy -y "$out"; then
        rm -f "$video" "$audio" "${out%.webm}".temp.webm
        return 0
    fi
    return 1
}

# Probe a finished file end-to-end. Returns 0 if usable, 1 if corrupt.
# Catches the byte-level resume corruption that ffprobe's metadata read
# misses: we look for the EBML / DTS errors ffmpeg surfaces on stderr.
verify_integrity() {
    local f="$1" expected_sec="$2"
    local actual
    actual=$(ffprobe -v error -show_entries format=duration \
                -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null)
    if [[ -z "$actual" ]]; then
        echo "[verify] $f: ffprobe couldn't read duration" >&2
        return 1
    fi
    if (( $(printf '%.0f' "$actual") < expected_sec * 95 / 100 )); then
        echo "[verify] $f: duration $actual s < expected ${expected_sec}s (likely corrupt)" >&2
        return 1
    fi
    # Cheap deep scan: walk packets without decoding. Flags any container damage.
    local errs
    errs=$(ffmpeg -hide_banner -nostats -v error -i "$f" -map 0 \
                  -c copy -f null - 2>&1 | head -3)
    if [[ -n "$errs" ]]; then
        echo "[verify] $f: container errors detected:" >&2
        echo "$errs" | sed 's/^/  /' >&2
        return 1
    fi
    return 0
}

i=$START
for url in "$@"; do
    out="day${i}.webm"
    if [[ -f "$out" ]]; then
        echo "[skip] $out already exists" >&2
    else
        # Retry the whole download up to 5 times. yt-dlp resumes from .part
        # on each restart, so a transient network drop / SIGURG that kills the
        # process doesn't lose the bytes we already have.
        attempt=1
        max_attempts=5
        # Default to best available (4K when present). Override with FORMAT
        # env var to cap (e.g. FORMAT='bv*[height<=1080]+ba/b[height<=1080]').
        : "${FORMAT:=bv*[ext=webm]+ba[ext=webm]/bv*+ba/b}"
        # Look up expected duration so we can verify integrity afterwards.
        expected_sec=$(yt-dlp --print "%(duration)s" --no-warnings "$url" 2>/dev/null || echo 0)
        while true; do
            yt-dlp \
                -f "$FORMAT" \
                --merge-output-format webm \
                --concurrent-fragments 8 \
                --retries 30 \
                --fragment-retries 30 \
                --retry-sleep linear=1::10 \
                --socket-timeout 30 \
                --no-progress --newline \
                -o "$out" \
                "$url"
            rc=$?
            # If the .webm wasn't produced but the separate streams are, try
            # our own ffmpeg merge before re-downloading.
            if [[ ! -f "$out" ]] && manual_merge "$out"; then
                echo "[recovered] $out via manual merge after yt-dlp rc=$rc" >&2
            fi
            if [[ -f "$out" ]] && \
               { (( expected_sec == 0 )) || verify_integrity "$out" "$expected_sec"; }; then
                break  # Success
            fi
            # Failure path: corrupt or missing output. Wipe everything and retry.
            echo "[bad ] $out: discarding (yt-dlp rc=$rc)" >&2
            rm -f "$out" "${out%.webm}".f*.webm "${out%.webm}".f*.webm.part \
                  "${out%.webm}".temp.webm "${out%.webm}".webm.part
            if (( attempt >= max_attempts )); then
                echo "[fail] $out: gave up after $max_attempts attempts" >&2
                exit 1
            fi
            attempt=$((attempt + 1))
            echo "[retry] $out: attempt $attempt (clean restart in 5s) ..." >&2
            sleep 5
        done
        echo "[get ] $out  ←  $url  (verified)"
    fi
    i=$((i + 1))
done

echo
echo "Downloaded files:"
ls -lh day*.webm 2>/dev/null || true
echo
echo "Next: ../scripts/find_w2w_races.py scan day1.webm   (then snip)"
