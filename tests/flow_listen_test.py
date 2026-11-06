"""聽力模擬測驗的流程測試：假 Discord 物件＋罐頭題目，不連網、不配音。
執行：DB_FILE=/tmp/x.db LOG_DIR=/tmp/xl python tests/flow_listen_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
assert os.getenv("DB_FILE", "").startswith("/tmp"), "請用暫存資料庫，不要寫進正式資料庫"
import bot as core  # noqa: E402
import listening_test as lt  # noqa: E402
import usage  # noqa: E402

usage.current_user.set(4242)
core.LISTEN_PREVIEW_SECONDS = 0


def canned(section):
    filler = " ".join(["well"] * 320)
    qs = [{"type": "blank", "question": f"Item {i}: ____", "answer": f"word{i}", "alternatives": [], "evidence": "", "explanation": "e"}
          for i in range(1, 11)]
    script = " ".join(f"The item is word{i}." for i in range(1, 11)) + " " + filler
    return lt.parse_section(json.dumps({"title": f"T{section}", "context": "ctx", "segments": [{"speaker": "A", "text": script}],
                                        "choices": [], "questions": qs}), section)


async def fake_prepare(session, tag):
    path = Path(tempfile.gettempdir()) / f"fake_{tag}_{session.section}.ogg"
    path.write_bytes(b"x")
    return canned(session.section), path


core.prepare_listen_section = fake_prepare


class Chan:
    def __init__(self):
        self.sent = []
        self.id = 9

    async def send(self, text=None, file=None):
        self.sent.append((text, bool(file)))


class Msg:
    def __init__(self, chan, text, mid=1):
        self.content, self.channel, self.id = text, chan, mid
        self.author = SimpleNamespace(id=4242)
        self.reactions = []

    async def add_reaction(self, e):
        self.reactions.append(e)

    async def reply(self, text, **kw):
        self.channel.sent.append((text, False))


async def main():
    core.init_db()
    chan = Chan()
    key = (chan.id, 4242)

    # 1) 完整測驗：Section 1 答對 8 題、done -> 進入 Section 2 -> stop 結束
    session = core.ListenTestSession([1, 2, 3, 4], "", 5.5)
    core.bot.quizzes[key] = session
    assert await core.run_listen_section(session, chan.send, 1)
    assert any(f for _, f in chan.sent), "應該有送出音檔"
    assert not session.preparing and session.data
    m = Msg(chan, "1 word1\n2 word2\n3 word3\n4 word4\n5 word5\n6 word6\n7 word7\n8 word8\n9 wrong")
    await core.handle_listen_answer(core.bot, m, session)
    assert m.reactions == ["✅"] and len(session.answers) == 9, session.answers
    await core.handle_listen_answer(core.bot, Msg(chan, "hello there"), session)  # 看不懂 -> 提示，不計
    assert len(session.answers) == 9
    chan.sent.clear()
    await core.handle_listen_answer(core.bot, Msg(chan, "done"), session)
    assert session.current == 1 and session.results[0]["correct"] == 8, (session.current, session.results)
    assert key in core.bot.quizzes
    await core.handle_listen_answer(core.bot, Msg(chan, "stop"), session)  # Section 2 一題都沒答
    assert key not in core.bot.quizzes
    out = "\n".join(t for t, _ in chan.sent if t)
    assert "8/10" in out and "Section 2" in out and "錄音原文" in out, out[:800]
    print("完整測驗 done/stop 流程 OK")

    # 2) 單段：答完 done 直接結束；結束後再收到訊息不應出錯
    chan.sent.clear()
    session = core.ListenTestSession([4], "", 5.5)
    core.bot.quizzes[key] = session
    await core.run_listen_section(session, chan.send, 2)
    await core.handle_listen_answer(core.bot, Msg(chan, "lots of words with no number"), session)  # 不是合法格式
    assert len(session.answers) == 0
    await core.handle_listen_answer(core.bot, Msg(chan, "1 word1\n2 word2\n3 word3"), session)
    await core.handle_listen_answer(core.bot, Msg(chan, "done"), session)
    assert key not in core.bot.quizzes and session.results[0]["correct"] == 3
    print("單段流程 OK")

    # 3) 逾時：沒作答的段落也要能收尾、不重複評分
    session = core.ListenTestSession([1], "", 5.5)
    core.bot.quizzes[key] = session
    await core.run_listen_section(session, chan.send, 3)
    await core.handle_listen_answer(core.bot, Msg(chan, "1 word1"), session)
    out = core.finish_listen_test(core.bot, key, session, reason="timeout")
    assert key not in core.bot.quizzes and len(session.results) == 1 and out
    again = core.finish_listen_test(core.bot, key, session)
    assert len(session.results) == 1
    print("逾時收尾 OK；DB 紀錄：")
    import sqlite3
    with sqlite3.connect(core.DB_FILE) as c:
        for row in c.execute("SELECT kind, status, answered, correct FROM exercise_sessions WHERE kind='listen_test'"):
            print("  ", row)
    del again


asyncio.run(main())
