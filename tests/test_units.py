"""Unit tests for find_w2w_races.py helpers. No ffmpeg/tesseract needed."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import find_w2w_races as m
from find_w2w_races import Hit, Range


class ClassifyTest(unittest.TestCase):
    def test_race_with_number(self):
        self.assertEqual(m.classify("RUSH SERIES | RACE 2"), "RACE_2")

    def test_race_without_number(self):
        self.assertEqual(m.classify("RACE"), "RACE")

    def test_qualifying_wins_over_race(self):
        self.assertEqual(m.classify("QUAL RACE 1"), "QUALIFYING")

    def test_warmup_and_practice(self):
        self.assertEqual(m.classify("RUSH WARMUP"), "WARMUP")
        self.assertEqual(m.classify("PRACTICE"), "PRACTICE")

    def test_unknown(self):
        self.assertEqual(m.classify("hello world"), "UNKNOWN")


class ParseSeriesTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(m.parse_series("rush"), ["rush"])

    def test_all_preserves_declaration_order(self):
        self.assertEqual(m.parse_series("all"), list(m.SERIES))

    def test_comma_list_dedupes_and_lowercases(self):
        self.assertEqual(m.parse_series("GLTC, rush ,GLTC"), ["gltc", "rush"])

    def test_unknown_raises_systemexit(self):
        with self.assertRaises(SystemExit):
            m.parse_series("nascar")


class FmtTsTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(m.fmt_ts(0), "00:00:00.000")

    def test_hours_minutes_seconds(self):
        self.assertEqual(m.fmt_ts(3661.5), "01:01:01.500")


class SidecarForTest(unittest.TestCase):
    def test_appends_series_and_json_suffix(self):
        self.assertEqual(m.sidecar_for(Path("a.webm"), "rush"),
                         Path("a.webm.rush.json"))


class MergeRangesTest(unittest.TestCase):
    @staticmethod
    def hit(t, label="RACE_1"):
        return Hit(t=t, text=f"RUSH {label}", label=label)

    def test_empty_hits(self):
        self.assertEqual(m.merge_ranges([], 120, 5, 15, 100), [])

    def test_merges_within_gap_and_pads(self):
        hits = [self.hit(10), self.hit(11), self.hit(12)]
        (r,) = m.merge_ranges(hits, gap=120, pad_pre=5, pad_post=15, max_t=100)
        self.assertEqual((r.start, r.end, r.hits), (5.0, 27.0, 3))
        self.assertEqual(r.labels, ["RACE_1"])

    def test_splits_beyond_gap(self):
        hits = [self.hit(10), self.hit(11), self.hit(12),
                self.hit(200, "RACE_2"), self.hit(201, "RACE_2"),
                self.hit(202, "RACE_2")]
        ranges = m.merge_ranges(hits, gap=120, pad_pre=5, pad_post=15, max_t=1000)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0].labels, ["RACE_1"])
        self.assertEqual(ranges[1].labels, ["RACE_2"])

    def test_drops_below_min_hits(self):
        hits = [self.hit(10), self.hit(11)]
        self.assertEqual(m.merge_ranges(hits, 120, 5, 15, 100), [])

    def test_clamps_to_zero_and_max_t(self):
        hits = [self.hit(1), self.hit(2), self.hit(3)]
        (r,) = m.merge_ranges(hits, 120, pad_pre=5, pad_post=15, max_t=10)
        self.assertEqual(r.start, 0.0)
        self.assertEqual(r.end, 10.0)

    def test_dominant_label_beats_unknown(self):
        hits = [self.hit(10, "UNKNOWN"), self.hit(11, "RACE_3"),
                self.hit(12, "UNKNOWN")]
        (r,) = m.merge_ranges(hits, 120, 5, 15, 100)
        self.assertEqual(r.labels, ["RACE_3"])


class BackExtendTest(unittest.TestCase):
    @staticmethod
    def rng(start):
        return Range(start=start, end=start + 10, hits=3, labels=["RACE_1"])

    def test_extends_through_connected_banner_trail(self):
        (r,) = m.back_extend_to_commercial_end([self.rng(100)], [96, 92, 88])
        self.assertEqual(r.start, 88.0)

    def test_stops_at_commercial_gap(self):
        (r,) = m.back_extend_to_commercial_end([self.rng(100)], [50, 90])
        # 100-90=10 (extend), then 90-50=40 > commercial_gap → stop
        self.assertEqual(r.start, 90.0)

    def test_respects_max_lookback(self):
        (r,) = m.back_extend_to_commercial_end([self.rng(100)], [10, 95],
                                               max_lookback=20)
        # banner at 10 is outside the 20s lookback window
        self.assertEqual(r.start, 95.0)

    def test_distant_first_banner_stops_extension(self):
        (r,) = m.back_extend_to_commercial_end([self.rng(100)], [10, 20])
        # 100-20=80 > commercial_gap → no extension at all
        self.assertEqual(r.start, 100.0)

    def test_no_banners_leaves_ranges_untouched(self):
        ranges = [self.rng(100)]
        self.assertEqual(
            m.back_extend_to_commercial_end(ranges, [])[0].start, 100.0)
        self.assertEqual(
            m.back_extend_to_commercial_end([], [10, 20]), [])


class GroupIntoSessionsTest(unittest.TestCase):
    @staticmethod
    def rng(s, e):
        return {"start": s, "end": e, "hits": 3, "labels": ["RACE_1"]}

    def test_splits_on_session_gap(self):
        sessions = m.group_into_sessions(
            [self.rng(0, 10), self.rng(15, 25), self.rng(5000, 5010)], 1800)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(sessions[0]), 2)
        self.assertEqual(len(sessions[1]), 1)

    def test_empty(self):
        self.assertEqual(m.group_into_sessions([], 1800), [])


class ShowinfoPtsTest(unittest.TestCase):
    SAMPLE = (
        "[Parsed_showinfo_0 @ 0x7fa] n:   0 pts:    -42 pts_time:-1.4 pos:   512\n"
        "[Parsed_showinfo_0 @ 0x7fa] n:   1 pts:     36 pts_time:1.5 pos:  1024\n"
        "[Parsed_showinfo_0 @ 0x7fa] n:   2 pts:    300 pts_time:12 pos:  2048\n"
        "[Parsed_showinfo_0 @ 0x7fa] n:   3 pts:    480 pts_time:19.25 pos:  4096\n"
    )

    def test_parses_in_output_order(self):
        self.assertEqual(m._showinfo_pts_times(self.SAMPLE),
                         [-1.4, 1.5, 12.0, 19.25])

    def test_empty_and_garbage(self):
        self.assertEqual(m._showinfo_pts_times(""), [])
        self.assertEqual(m._showinfo_pts_times("no matches here\n"), [])


class ResolveToolTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_uses_path_hit(self):
        self.assertTrue(os.path.isabs(m.FFMPEG))

    def test_falls_back_to_existing_candidate(self):
        tmp = tempfile.mkdtemp()
        try:
            cand = os.path.join(tmp, "definitely-not-installed")
            with open(cand, "w"):
                pass
            self.assertEqual(
                m._resolve_tool("definitely-not-installed",
                                ["/nope/nothing", cand]),
                cand)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_bare_name_when_unresolved(self):
        self.assertEqual(m._resolve_tool("definitely-not-installed", []),
                         "definitely-not-installed")


class ScratchDirTest(unittest.TestCase):
    def test_prefers_sandbox_root_only_when_usable(self):
        root = Path("/private/tmp/claude")
        d = m._scratch_dir()
        try:
            self.assertTrue(d.is_dir())
            if root.is_dir() and os.access(root, os.W_OK):
                self.assertEqual(d.parent, root)
            else:
                self.assertEqual(d.parent, Path(tempfile.gettempdir()))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
