"""真實 Gemini + 真實本地語音辨識的對話追問測試（用暫存資料庫）。
執行：DB_FILE=/tmp/x.db LOG_DIR=/tmp/xl python tests/live_dialogue.py
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asr  # noqa: E402
import bot as core  # noqa: E402
import dialogue  # noqa: E402
import tts  # noqa: E402
import usage  # noqa: E402

usage.current_user.set(4242)


def say(text, name):
    path = f"/tmp/{name}.wav"
    tts.synthesize([{"speaker": "A", "text": text}], path, 1.0, "en-us")
    return path


async def main():
    core.init_db()
    t = time.time()
    text = await core.ask_gemini(dialogue.build_tree_prompt(5.5, "", []), json_mode=True, temperature=1.0,
                                 feature="speaking_dialogue_gen")
    tree = dialogue.parse_tree(text)
    print(f"樹產生 {time.time() - t:.1f}s｜主題 {tree['topic']}｜開場：{tree['opening']}")
    for b in tree["branches"]:
        print(f"  分支 [{b['label']}] 關鍵字={b['keywords'][:6]} -> {b['question']}")
        for leaf in b["branches"]:
            print(f"      └ [{leaf['label']}] -> {leaf['question']}")
    print("  通用:", tree["generic"], "｜", tree["generic_next"])

    d = dialogue.Dialogue(tree, max_live=1)
    session = core.SpeakingSession(tree["topic"], [{"part": 3, "question": tree["opening"], "prefix": "", "card": None}],
                                   "", db_id=1, level=5.5, speed=1.0, prefix="livetest")
    session.dialogue = d
    msg = SimpleNamespace(author=SimpleNamespace(id=4242), channel=SimpleNamespace(id=9))

    # 第 1 輪：用第一個分支的關鍵字造一句話來答；第 2 輪：離題的長回答（測即時接話）
    first = tree["branches"][0]
    answers = [
        f"Well, to be honest, I would say {first['keywords'][0]} and also {first['keywords'][1]} are very important for me, "
        "and I think it is something I do quite often in my free time.",
        "Actually yesterday my neighbour bought a brand new motorbike and it makes a terrible noise every morning at six.",
    ]
    for i, ans in enumerate(answers, 1):
        path = say(ans, f"live_ans{i}")
        session.answers.append({"seq": i, "step": session.steps[-1], "path": path, "mime": "audio/wav", "seconds": 8,
                                "skipped": False})
        session.index += 1
        t = time.time()
        await core.advance_dialogue(msg, session)
        print(f"\n第 {i} 輪答：{ans[:70]}…\n  用時 {time.time() - t:.1f}s\n  {session.notice.replace(chr(10), ' ｜ ')}"
              f"\n  下一題：{session.steps[-1]['question']}｜音檔 {os.path.exists(session.steps[-1]['audio'])}")
    print("\n對話結束了嗎:", d.finished, "｜即時接話次數:", d.live_used)
    import sqlite3
    with sqlite3.connect(core.DB_FILE) as c:
        n = c.execute("SELECT COUNT(*) FROM api_calls WHERE ts >= datetime('now','localtime','-10 minutes')").fetchone()[0]
    print("這段對話 Gemini 呼叫次數（含出題）:", n)


asyncio.run(main())
