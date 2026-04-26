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

i=1
for url in "$@"; do
    out="day${i}.webm"
    if [[ -f "$out" ]]; then
        echo "[skip] $out already exists" >&2
    else
        echo "[get ] $out  ←  $url"
        # -f bv*+ba/b: best video+audio merged, prefer webm/AV1 if available
        # --merge-output-format webm: keep AV1+Opus container
        # --concurrent-fragments 8: faster on fast networks
        yt-dlp \
            -f 'bv*[ext=webm]+ba[ext=webm]/bv*+ba/b' \
            --merge-output-format webm \
            --concurrent-fragments 8 \
            --no-progress --newline \
            -o "$out" \
            "$url"
    fi
    i=$((i + 1))
done

echo
echo "Downloaded files:"
ls -lh day*.webm 2>/dev/null || true
echo
echo "Next: ../scripts/find_rush_races.py scan day1.webm   (then snip)"
