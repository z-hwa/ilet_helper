"""德語閒聊的流程測試：假 Discord ＋罐頭 Gemini 回應＋真的 Piper 配音。用暫存資料庫，不碰正式資料。

執行：PYTHONPATH=. python tests/flow_gossip.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

import soundfile as sf
from discord import app_commands

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import german as g  # noqa: E402
import usage

core = g.core
tmp = tempfile.mkdtemp()
os.chdir(tmp)
os.symlink(str(ROOT / "models"), "models")
(Path(tmp) / "audio_cache").mkdir()
core.DB_FILE = os.path.join(tmp, "t.db")
core.OWNER_ID = "100"
core.init_db()

OPEN = json.dumps({"topic": "Büro-Klatsch", "german": "Hallo! Hast du gehört? Mein Kollege hat sich ein Tattoo stechen lassen.",
                   "english": "Hi! Have you heard? My colleague got a tattoo."})
FINAL = json.dumps({"level_estimate": "A2+", "summary": "整體不錯", "criteria": {"pronunciation": 3, "vocabulary": 3, "grammar": 2.5, "fluency": 3},
                    "criteria_notes": {"grammar": "完成式助動詞"}, "priorities": ["haben/sein"],
                    "suggestions": [{"seq": 1, "original": "habe gegangen", "better": "bin gegangen", "why": "sein"}]})


def turn(status, n, **kw):
    d = {"transcript": f"Ich habe gestern gegangen {n}.", "transcript_en": f"I went yesterday {n}.", "status": status,
         "verdict": "ok", "errors": [{"said": "habe gestern gegangen", "fix": "bin gestern gegangen", "why": "sein"}] if status == "errors" else [],
         "better": "Ich bin gestern gegangen.", "pronunciation_note": "", "reply": f"Echt? Erzähl mehr {n}!", "reply_en": f"Really? Tell me more {n}!"}
    d.update(kw)
    return json.dumps(d)


SCRIPT = {"queue": []}
CALLS = []


async def fake_generate(model, contents, config):
    system = getattr(config, "system_instruction", None) or ""
    prompt = contents if isinstance(contents, str) else contents[0]
    CALLS.append((system[:20], prompt[:30]))
    if "聊八卦" in system and "回合" in system:
        text = SCRIPT["queue"].pop(0)
    elif "嚴謹的德語口說老師" in system:
        text = FINAL
    else:
        text = OPEN
    return NS(text=text, usage_metadata=NS(prompt_token_count=1, candidates_token_count=1, thoughts_token_count=0))


core.ai_client = NS(aio=NS(models=NS(generate_content=fake_generate)))
out, files = [], []


class R:
    async def send_message(self, text, ephemeral=False, **k): out.append(text)
    async def defer(self, **k): pass
    def is_done(self): return True


class F:
    async def send(self, text=None, **k):
        out.append(text)
        if "file" in k: files.append(k["file"].filename)


class Typing:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Ch:
    id = 5
    async def send(self, text, **k):
        out.append(text)
        if "file" in k: files.append(k["file"].filename)
        files.extend(f.filename for f in k.get("files", []))
    def typing(self): return Typing()


class Att:
    def __init__(self, path):
        self.filename, self.content_type, self.path = "voice-message.ogg", "audio/ogg", path
        self.size, self.duration = os.path.getsize(path), sf.info(path).duration
    async def save(self, p): Path(p).write_bytes(Path(self.path).read_bytes())


class Msg:
    def __init__(self, uid, content="", clip=None):
        self.author, self.channel = NS(id=uid, bot=False), Ch()
        self.content, self.attachments = content, ([clip] if clip else [])
    async def reply(self, text, **k):
        out.append(text)
        if "file" in k: files.append(k["file"].filename)
        files.extend(f.filename for f in k.get("files", []))


def inter(uid):
    return NS(user=NS(id=uid, display_name="U"), guild_id=None, channel_id=5, id=int(time.time() * 1000) % 10**9,
              command=None, response=R(), followup=F())


def ogg():
    wav = Path(tmp) / "v.wav"
    g.synth(["Ich gehe gern schwimmen."], wav)
    data, rate = sf.read(str(wav))
    p = Path(tmp) / "v.ogg"
    sf.write(str(p), data, rate, format="OGG", subtype="VORBIS")
    return str(p)


def check(label, ok):
    print(f"  {'✅' if ok else '❌'} {label}")
    return ok


async def start(uid, rounds):
    core.guard = usage.Guard(usage.Limits(user_rpm=99, rpm=99))
    g.bot.sessions.clear()
    out.clear(), files.clear(), CALLS.clear()
    await g.de_gossip.callback(inter(uid), rounds, "")


async def main():
    uid, client, ok = 888, g.bot, True
    usage.current_user.set(uid)
    await g.de_register.callback(inter(uid), app_commands.Choice(name="A2", value="A2"))
    voice = ogg()

    print("== A. 3 回合：錯誤 → 沒聽清楚（不算）→ 重說 → 完全正確 → 自動結算 ==")
    SCRIPT["queue"] = [turn("errors", 1), turn("unclear", 0, transcript=""), turn("ok", 2), turn("perfect", 3)]
    await start(uid, 3)
    ok &= check("開場白附語音、翻譯被遮擋", any("||Hallo!" in t and "🌐 ||Hi! Have you heard" in t for t in out) and "lena.wav" in files)
    s = client.sessions[(5, uid)]
    out.clear(), files.clear()
    await client.on_message(Msg(uid, clip=Att(voice)))
    ok &= check("第 1 回合：即時點評指出錯誤", any("第 1/3 回合" in t and "→「**bin gestern gegangen**」" in t and "更自然的說法" in t for t in out))
    ok &= check("第 1 回合：Lena 的回應是遮擋文字＋語音", any("🎧 **Lena 說**" in t and "||Echt? Erzähl mehr 1!||" in t for t in out) and "lena.wav" in files)
    out.clear()
    await client.on_message(Msg(uid, clip=Att(voice)))
    ok &= check("沒聽清楚：不算回合、請他再說", len(s.turns) == 1 and any("沒聽清楚" in t for t in out))
    out.clear()
    await client.on_message(Msg(uid, "hallo ich tippe das"))
    ok &= check("打字不算，只提醒一次", len(s.turns) == 1 and any("只收語音" in t for t in out))
    out.clear()
    await client.on_message(Msg(uid, "noch einmal tippen"))
    ok &= check("第二次打字不再回應", out == [])
    await client.on_message(Msg(uid, clip=Att(voice)))
    ok &= check("重說後算第 2 回合", len(s.turns) == 2 and s.turns[1]["seq"] == 2)
    out.clear(), files.clear()
    await client.on_message(Msg(uid, clip=Att(voice)))
    ok &= check("最後一回合後自動結算（session 結束）", (5, uid) not in client.sessions)
    joined = "\n".join(t or "" for t in out)
    ok &= check("總評與四項評分", "德語閒聊結束" in joined and "A2+" in joined and "文法｜2.5" in joined)
    ok &= check("建議例句", "建議：「bin gegangen」" in joined)
    ok &= check("逐字稿＋英文翻譯（遮擋）", "你：||Ich habe gestern gegangen 1.||" in joined and "🌐 ||I went yesterday 1.||" in joined
                and "Lena：||Hallo!" in joined)
    ok &= check("逐字稿附你的錄音", sum(f.startswith("antwort_") for f in files) == 3)
    ok &= check("評級有更新", "德語口說" in joined)
    ok &= check("暫存錄音都清掉了", not list((Path(tmp) / "audio_cache").glob("de_gsp_*")))
    print("  Gemini 呼叫：", len(CALLS), "次（開場 1＋回合 4＋評分 1 = 6）")
    ok &= check("呼叫次數", len(CALLS) == 6)
    with core.db_connect() as c:
        row = c.execute("SELECT status, answered, estimate FROM de_gossip_sessions ORDER BY id DESC LIMIT 1").fetchone()
        n = c.execute("SELECT COUNT(*) FROM de_gossip_turns WHERE session_id = (SELECT MAX(id) FROM de_gossip_sessions)").fetchone()[0]
    ok &= check(f"資料庫 {row} / {n} 回合", row[0] == "completed" and row[1] == 3 and n == 3)

    print("\n== B. 只聊 1 回合就 stop：不評分 ==")
    SCRIPT["queue"] = [turn("ok", 1)]
    await start(uid, 5)
    await client.on_message(Msg(uid, clip=Att(voice)))
    out.clear()
    await client.on_message(Msg(uid, "stop"))
    ok &= check("不足 2 回合不評分", (5, uid) not in client.sessions and any("不足 2 回合" in (t or "") for t in out))

    print("\n== C. 聊 2 回合 stop：評分 ==")
    SCRIPT["queue"] = [turn("ok", 1), turn("errors", 2)]
    await start(uid, 5)
    await client.on_message(Msg(uid, clip=Att(voice)))
    await client.on_message(Msg(uid, clip=Att(voice)))
    out.clear()
    await client.on_message(Msg(uid, "Stop"))
    ok &= check("提前結束也會評分", any("程度估計" in (t or "") for t in out))

    print("\n== D. 逾時自動結束 ==")
    SCRIPT["queue"] = [turn("ok", 1), turn("ok", 2)]
    await start(uid, 5)
    await client.on_message(Msg(uid, clip=Att(voice)))
    await client.on_message(Msg(uid, clip=Att(voice)))
    out.clear()
    s = client.sessions[(5, uid)]
    client.get_channel = lambda cid: Ch()
    await client.expire((5, uid), s)
    ok &= check("逾時：通知＋評分", any("自動結束" in (t or "") for t in out) and any("程度估計" in (t or "") for t in out) and (5, uid) not in client.sessions)

    print("\n結果：", "全部通過" if ok else "有失敗")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
