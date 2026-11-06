"""主模型 503 時改用備援模型（假的 Gemini client，不連網）。"""
import asyncio
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_FILE", os.path.join(_tmp, "t.db"))
os.environ.setdefault("LOG_DIR", os.path.join(_tmp, "logs"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


class Res:
    text = "ok"
    usage_metadata = None


class FakeModels:
    def __init__(self, down):
        self.down, self.calls = down, []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.down:
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return Res()


class FakeClient:
    def __init__(self, down):
        self.aio = type("A", (), {"models": FakeModels(down)})()


class Fallback(unittest.TestCase):
    def setUp(self):
        bot.init_db()
        self.old = (bot.ai_client, bot.GEMINI_FALLBACK_MODEL, bot.TRANSIENT_RETRY_WAITS)
        bot.GEMINI_FALLBACK_MODEL = "fallback-model"
        bot.TRANSIENT_RETRY_WAITS = (0.0, 0.0)

    def tearDown(self):
        bot.ai_client, bot.GEMINI_FALLBACK_MODEL, bot.TRANSIENT_RETRY_WAITS = self.old

    def ask(self, down, tag):
        bot.ai_client = FakeClient(down)
        bot.guard.open_until = 0.0
        bot.guard.error_streak = 0
        bot.guard.recent.clear()
        text = asyncio.run(bot.ask_gemini(f"hello {tag}", feature="test"))
        return text, bot.ai_client.aio.models.calls

    def test_primary_ok_no_fallback(self):
        text, calls = self.ask(set(), "a")
        self.assertEqual(calls, [bot.GEMINI_MODEL])

    def test_primary_down_uses_fallback(self):
        text, calls = self.ask({bot.GEMINI_MODEL}, "b")
        self.assertEqual(text, "ok")
        self.assertEqual(calls, [bot.GEMINI_MODEL, "fallback-model"])

    def test_disabled_fallback_raises(self):
        bot.GEMINI_FALLBACK_MODEL = ""
        with self.assertRaises(Exception):
            self.ask({bot.GEMINI_MODEL}, "c")


if __name__ == "__main__":
    unittest.main()
