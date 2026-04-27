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
# Run this from inside the event's folder, e.g.
#   cd "2026-04 GridLife CMP" && ../scripts/download_gridlife_streams.sh URL1 URL2 URL3
#
# Defaults to 1080p (plenty for OCR; 4K is 4-10x larger). For 4K override:
#   FORMAT='bv*[ext=webm]+ba[ext=webm]/bv*+ba/b' download_gridlife_streams.sh URL...
#
set -euo pipefail

CHANNEL="https://www.youtube.com/@Gridlife/streams"

if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "yt-dlp not found. Install with: brew install yt-dlp" >&2
    exit 1
fi

case "${1:-}" in
    "")
        sed -n '2,16p' "$0" | sed 's/^# *//'
        exit 0
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

i=1
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
        # Cap at 1080p by default — the OCR pipeline downscales to 720p
        # anyway, and 4K streams are 4-10× larger which means 4-10× more
        # exposure to the resume-corruption bug (interrupted partial writes
        # of fragmented streams have produced byte-level corruption).
        # Override with FORMAT=4k or FORMAT='bv*+ba/b' for the original.
        : "${FORMAT:=bv*[height<=1080][ext=webm]+ba[ext=webm]/bv*[height<=1080]+ba/b[height<=1080]}"
        until yt-dlp \
                -f "$FORMAT" \
                --merge-output-format webm \
                --concurrent-fragments 8 \
                --retries 30 \
                --fragment-retries 30 \
                --retry-sleep linear=1::10 \
                --socket-timeout 30 \
                --no-progress --newline \
                -o "$out" \
                "$url"; do
            rc=$?
            # If yt-dlp completed both downloads but its merger failed, the
            # separate fNNN.webm streams are already on disk. Try to merge
            # them ourselves before paying for another download attempt.
            if manual_merge "$out"; then
                echo "[recovered] $out via manual merge after yt-dlp rc=$rc" >&2
                break
            fi
            if (( attempt >= max_attempts )); then
                echo "[fail] $out: gave up after $max_attempts attempts (last rc=$rc)" >&2
                exit 1
            fi
            echo "[retry] $out: attempt $attempt failed (rc=$rc), retrying in 5s ..." >&2
            sleep 5
            attempt=$((attempt + 1))
            echo "[get ] $out  ←  $url  (attempt $attempt)"
        done
        echo "[get ] $out  ←  $url"
    fi
    i=$((i + 1))
done

echo
echo "Downloaded files:"
ls -lh day*.webm 2>/dev/null || true
echo
echo "Next: ../scripts/find_w2w_races.py scan day1.webm   (then snip)"
