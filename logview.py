"""查使用痕跡：python logview.py [events|errors|user <id>|call <id>|calls] [-n 30]

- events：最近的事件（指令、被忽略的訊息、逾時…）
- errors：最近的錯誤（含完整 traceback）
- user <id>：某位使用者最近的事件
- calls：最近的 Gemini 呼叫（成功/失敗、錯誤訊息）
- call <id>：某次 Gemini 呼叫的提示詞與回應原文
檔案紀錄在 logs/bot.log。
"""
import os
import sqlite3
import sys

DB = os.getenv("DB_PATH", "ielts_learning.db")


def main(argv):
    n = 30
    if "-n" in argv:
        i = argv.index("-n")
        n = int(argv[i + 1])
        del argv[i:i + 2]
    mode = argv[0] if argv else "events"
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if mode in ("events", "errors", "user"):
        where, args = "", []
        if mode == "errors":
            where = "WHERE kind = 'error'"
        elif mode == "user":
            where, args = "WHERE user_id = ?", [argv[1]]
        for r in reversed(conn.execute(f"SELECT * FROM app_events {where} ORDER BY id DESC LIMIT ?", (*args, n)).fetchall()):
            print(f"{r['ts']} {r['bot']}/{r['kind']} {r['name']} user={r['user_id']} ch={r['channel_id']}")
            if r["detail"]:
                print("    " + r["detail"][:3000 if mode == "errors" else 300].replace("\\n", "\n    "))
    elif mode == "calls":
        for r in reversed(conn.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (n,)).fetchall()):
            print(f"#{r['id']} {r['ts']} {r['feature']} user={r['user_id']} ok={r['ok']} {r['latency_ms']}ms "
                  f"in={r['input_tokens']} out={r['output_tokens']} {r['error_kind'] or ''} {(r['error_text'] or '')[:150]}")
    elif mode == "call":
        r = conn.execute("SELECT * FROM api_payloads WHERE call_id = ?", (argv[1],)).fetchone()
        if not r:
            print("找不到（可能超過保存天數，或當時沒有保存）")
            return
        print(f"=== 提示詞 ===\n{r['prompt']}\n\n=== 回應 ===\n{r['response']}")
        if r["error"]:
            print(f"\n=== 錯誤 ===\n{r['error']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
