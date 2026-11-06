"""德語多聲音的選擇邏輯（不合成語音）。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_FILE", os.path.join(_tmp, "t.db"))
os.environ.setdefault("LOG_DIR", os.path.join(_tmp, "logs"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import german  # noqa: E402


class Voices(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in ("de_DE-thorsten-medium", "de_DE-ramona-low", "de_DE-karlsson-low", "de_DE-nojson-low"):
            (self.dir / f"{name}.onnx").write_bytes(b"x")
            if "nojson" not in name:
                (self.dir / f"{name}.onnx.json").write_text("{}")
        self.old = german.VOICE_MODEL
        german.VOICE_MODEL = self.dir / "de_DE-thorsten-medium.onnx"

    def tearDown(self):
        german.VOICE_MODEL = self.old

    def test_default_first_and_models_without_config_skipped(self):
        names = [p.name for p in german.voice_files()]
        self.assertEqual(names[0], "de_DE-thorsten-medium.onnx")
        self.assertEqual(sorted(names[1:]), ["de_DE-karlsson-low.onnx", "de_DE-ramona-low.onnx"])

    def test_same_seed_same_voice_and_all_voices_used(self):
        self.assertEqual(german.voice_for(7), german.voice_for(7))
        self.assertEqual({german.voice_for(i) for i in range(1, 7)}, set(german.voice_files()))

    def test_single_voice_when_disabled(self):
        german.MULTI_VOICE = False
        try:
            self.assertEqual(german.voice_files(), [german.VOICE_MODEL])
        finally:
            german.MULTI_VOICE = True

    def test_label(self):
        self.assertEqual(german.voice_label("models/de/de_DE-ramona-low.onnx"), "Ramona（女聲）")
        self.assertEqual(german.voice_label("models/de/de_DE-thorsten-medium.onnx"), "Thorsten（男聲）")


if __name__ == "__main__":
    unittest.main()
