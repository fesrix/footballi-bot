import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_utils import inspect_audio_file


class ShortAudioProbeTests(unittest.TestCase):
    @patch("media_utils.subprocess.run")
    def test_missing_audio_stops_before_decode(self, run):
        run.return_value = SimpleNamespace(stdout='{"streams":[]}')
        self.assertEqual(inspect_audio_file("clip.mp4"), ("missing_audio", None))
        run.assert_called_once()

    @patch("media_utils.subprocess.run")
    def test_silent_track_and_undecodable_track_do_not_pass(self, run):
        for metadata, expected in (("max_volume: -91.0 dB", "silent_audio"), ("", "audio_probe_failed")):
            run.side_effect = [SimpleNamespace(stdout='{"streams":[{"codec_name":"aac"}]}'),
                               SimpleNamespace(stderr=metadata)]
            self.assertEqual(inspect_audio_file("clip.mp4")[0], expected)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Needs FFmpeg")
class ShortAudioTests(unittest.TestCase):
    def test_real_mp4_with_missing_silent_and_audible_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, source, expected in (
                ("picture_only", None, "missing_audio"),
                ("silent", "anullsrc=r=44100:cl=mono", "silent_audio"),
                ("audible", "sine=f=440:r=44100", None),
            ):
                with self.subTest(name=name):
                    output = str(Path(directory) / f"{name}.mp4")
                    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                               "color=c=black:s=64x96:r=10"]
                    if source:
                        command += ["-f", "lavfi", "-i", source, "-c:a", "aac"]
                    subprocess.run(command + ["-t", "1", "-c:v", "mpeg4", output],
                                   check=True, capture_output=True, timeout=15)
                    issue, peak = inspect_audio_file(output)
                    self.assertEqual(issue, expected)
                    if expected is None:
                        self.assertGreater(peak, -65)
