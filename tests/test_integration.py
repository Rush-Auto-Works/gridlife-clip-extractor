"""Integration tests: real ffmpeg/tesseract against tiny synthetic clips.

Skipped automatically when the tools are missing (e.g. Windows CI, which
runs only the unit suite). The scan test asserts the pairing invariant
that matters: every OCR hit lands inside the time window where the test
text was burned into the video, regardless of whether the decoder honors
-skip_frame nokey.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import find_w2w_races as m

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "find_w2w_races.py"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
TESSERACT = shutil.which("tesseract")

DURATION = 30
TEXT_WINDOW = (10, 20)
TEXT = "RUSH RACE 1"
FONT_CANDIDATES = [
    "/usr/share/truetype/dejavu/DejaVuSans.ttf",      # Debian/Ubuntu
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",   # macOS
]


def _has_drawtext():
    out = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return " drawtext " in out


@unittest.skipUnless(FFMPEG, "ffmpeg not on PATH")
class IntegrationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="w2w_tests_"))
        cls.font = None
        if _has_drawtext():
            cls.font = next(
                (f for f in FONT_CANDIDATES if Path(f).exists()), None)
        encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
        cls.has_svt = "libsvtav1" in encoders
        cls.vp9 = cls._encode("libvpx-vp9",
                              ["-deadline", "realtime", "-cpu-used", "8",
                               "-b:v", "500k"])
        cls.av1 = (cls._encode("libsvtav1", ["-preset", "8"])
                   if cls.has_svt else None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def _encode(cls, vcodec, vopts):
        out = cls.dir / f"test_{vcodec.replace('lib', '')}.webm"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i",
               f"testsrc2=duration={DURATION}:size=1280x720:rate=10",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
        if cls.font:
            cmd += ["-vf",
                    f"drawtext=fontfile={cls.font}:text='{TEXT}':fontsize=48:"
                    "fontcolor=white:box=1:boxcolor=black@0.8:boxborderw=12:"
                    "x=20:y=20:"
                    f"enable='between(t,{TEXT_WINDOW[0]},{TEXT_WINDOW[1]})'"]
        cmd += ["-g", "30", "-c:v", vcodec, *vopts,
                "-c:a", "libopus", "-b:a", "64k", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        return out

    def _streams(self, path):
        return subprocess.run(
            [m.FFPROBE, "-v", "error", "-show_entries", "stream=codec_name",
             "-of", "csv", str(path)],
            capture_output=True, text=True).stdout


class VideoProbeTest(IntegrationCase):
    def test_video_duration(self):
        dur = m.video_duration(self.vp9)
        self.assertGreaterEqual(dur, DURATION - 1)
        self.assertLessEqual(dur, DURATION + 1)

    def test_keyframe_times_sorted_in_range(self):
        kfs = m.keyframe_times(self.vp9)
        self.assertGreater(len(kfs), 0)
        self.assertEqual(kfs, sorted(kfs))
        self.assertTrue(all(0 <= t <= DURATION for t in kfs))


class ShowinfoPairingTest(IntegrationCase):
    def _pairing(self, video):
        scratch = m._scratch_dir()
        try:
            proc = m.extract_keyframes(video, scratch)
            _, err = proc.communicate()
            self.assertEqual(proc.returncode, 0)
            frames = sorted(scratch.glob("k_*.png"))
            pts = m._showinfo_pts_times(err)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self.assertGreater(len(frames), 0)
        self.assertEqual(len(frames), len(pts),
                         "every decoded frame must carry its own pts")
        self.assertEqual(pts, sorted(pts))
        self.assertLess(pts[-1], DURATION + 1)
        return frames, pts

    def test_vp9_pairing(self):
        frames, pts = self._pairing(self.vp9)
        kfs = m.keyframe_times(self.vp9)
        if len(frames) == len(kfs):
            # decoder honors -skip_frame nokey: pts must equal keyframe times
            for a, b in zip(pts, kfs):
                self.assertLessEqual(abs(a - b), 0.05)

    def test_av1_pairing(self):
        if not self.av1:
            self.skipTest("libsvtav1 not available in this ffmpeg build")
        self._pairing(self.av1)


class ScanWindowTest(IntegrationCase):
    @unittest.skipUnless(TESSERACT, "tesseract not installed")
    def test_hits_landed_at_true_timestamps(self):
        if not self.font:
            self.skipTest("no drawtext font available to burn overlay text")
        sidecar = Path(m.sidecar_for(self.vp9, "rush"))
        sidecar.unlink(missing_ok=True)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "scan", str(self.vp9)],
            cwd=str(REPO), capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        state = json.loads(sidecar.read_text())
        self.assertTrue(state["completed"])
        hits = state["hits"]
        self.assertGreaterEqual(len(hits), 3)
        lo, hi = TEXT_WINDOW
        bad = [h["t"] for h in hits if not (lo <= h["t"] <= hi)]
        self.assertEqual(bad, [],
                         f"hits outside the burned-in window: {bad[:5]}")
        self.assertEqual(len(state["ranges"]), 1)


class SnipTest(IntegrationCase):
    def setUp(self):
        self.out = self.dir / "out_clips"
        shutil.rmtree(self.out, ignore_errors=True)
        Path(m.sidecar_for(self.vp9, "rush")).write_text(json.dumps({
            "video": str(self.vp9), "series": "rush",
            "duration": float(DURATION), "completed": True,
            "gridlife_times": [],
            "hits": [
                {"t": 6.0, "text": TEXT, "label": "RACE_1"},
                {"t": 20.0, "text": TEXT, "label": "RACE_1"},
            ],
            "ranges": [
                {"start": 5.0, "end": 12.0, "hits": 3, "labels": ["RACE_1"]},
                {"start": 18.0, "end": 26.0, "hits": 3, "labels": ["RACE_1"]},
            ],
        }))

    def _snip(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "snip", str(self.vp9),
             "--out", str(self.out), *extra],
            cwd=str(REPO), capture_output=True, text=True, timeout=300)

    def _duration(self, path):
        out = subprocess.run(
            [m.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True).stdout.strip()
        return float(out)

    def test_default_webm_stream_copy_join(self):
        proc = self._snip()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        clips = list(self.out.glob("*.webm"))
        self.assertEqual(len(clips), 1)
        self.assertIn("session01", clips[0].name)
        self.assertIn("RACE_1", clips[0].name)
        self.assertEqual(list(self.out.glob(".*concat*")), [])
        self.assertEqual(list(self.out.glob("*part*")), [])
        dur = self._duration(clips[0])
        # nominal 15s; stream-copy cuts snap to keyframes
        self.assertGreaterEqual(dur, 13)
        self.assertLessEqual(dur, 22)
        streams = self._streams(clips[0])
        self.assertIn("vp9", streams)
        self.assertIn("opus", streams)

    def test_aac_audio_rejected_for_webm(self):
        proc = self._snip("--aac-audio")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("webm", proc.stderr + proc.stdout)

    def test_reencode_rejected_for_webm(self):
        proc = self._snip("--reencode")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("webm", proc.stderr + proc.stdout)

    def test_aac_audio_with_mp4(self):
        proc = self._snip("--aac-audio", "--container", "mp4")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        clips = list(self.out.glob("*.mp4"))
        self.assertEqual(len(clips), 1)
        streams = self._streams(clips[0])
        self.assertIn("vp9", streams)
        self.assertIn("aac", streams)


if __name__ == "__main__":
    unittest.main()
