"""真實 Gemini + 真實 Kokoro 的聽力模擬測驗測試（用暫存資料庫）。
執行：cp ielts_learning.db /tmp/x.db; DB_FILE=/tmp/x.db LOG_DIR=/tmp/xl python tests/live_listen_test.py [sections，例如 1234]
"""
import asyncio
import os
import sys
import time

import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as core  # noqa: E402
import listening_test as lt  # noqa: E402
import usage  # noqa: E402

usage.current_user.set(4242)


async def main():
    core.init_db()
    core.AUDIO_DIR.mkdir(exist_ok=True)
    wanted = [int(c) for c in (sys.argv[1] if len(sys.argv) > 1 else "1234")]
    session = core.ListenTestSession(wanted, "", 5.5)
    for i, section in enumerate(wanted):
        session.current = i
        t = time.time()
        try:
            data, path = await core.prepare_listen_section(session, f"live{section}")
        except Exception as e:
            print(f"Section {section} 失敗：{type(e).__name__}: {e}")
            continue
        info = sf.info(str(path))
        words = sum(len(s["text"].split()) for s in data["segments"])
        print(f"\n=== Section {section}｜{data['title']}｜{time.time() - t:.0f}s（含 Gemini 與配音）")
        print(f"腳本 {words} 字｜音檔 {info.duration:.0f} 秒、{os.path.getsize(path) / 1e6:.2f} MB｜"
              f"有效題 {len(data['questions'])}/{data['raw_count']}")
        print(lt.format_sheet(section, data)[:1500])
        for q in data["questions"][:3]:
            print(f"  答案={lt.answer_text(q)}｜依據={q['evidence'][:80]!r}")
        path.unlink(missing_ok=True)
        # 模擬作答：偶數題答對、奇數題答錯，確認整條評分流程可跑
        session.data, session.answers, session.graded = data, {}, False
        session.db_id = core.create_exercise_row("listen_test", data["title"], "", data["script"], len(data["questions"]))
        for n, q in enumerate(data["questions"], 1):
            session.answers[n] = lt.answer_text(q).split(". ")[0] if n % 2 == 0 else "zzz"
        core.grade_listen_section(session)
        await asyncio.sleep(8)
    key = (1, 4242)
    core.bot.quizzes[key] = session
    out = core.finish_listen_test(core.bot, key, session)
    print("\n".join(out)[:1200])
    print("總訊息數", len(out), "｜最長", max(len(m) for m in out))


asyncio.run(main())
