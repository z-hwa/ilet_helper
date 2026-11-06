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

SET = json.dumps({"topic": "H", "questions": [{"question": f"Frage {i}?", "hint_zh": "z"} for i in range(5)]})
SCORE = json.dumps({"level_estimate": "A2", "summary": "s", "criteria": {"pronunciation": 3, "vocabulary": 3, "grammar": 3, "fluency": 3},
                    "criteria_notes": {}, "priorities": ["a"], "suggestions": [], "transcripts": ["x"] * 5})


async def fake_generate(model, contents, config):
    system = getattr(config, "system_instruction", None) or ""
    text = SCORE if "德語口說老師" in system else SET
    return NS(text=text, usage_metadata=NS(prompt_token_count=1, candidates_token_count=1, thoughts_token_count=0))


core.ai_client = NS(aio=NS(models=NS(generate_content=fake_generate)))
out = []
SAVE_DELAY = {"s": 0.0}


class R:
    async def send_message(self, text, ephemeral=False, **k): out.append(text)
    async def defer(self, **k): pass
    def is_done(self): return True


class F:
    async def send(self, text=None, **k): out.append(text)


class Ch:
    id = 5
    async def send(self, text, **k): out.append(text)


class Att:
    def __init__(self, path):
        self.filename, self.content_type, self.path = "voice-message.ogg", "audio/ogg", path
        self.size, self.duration = os.path.getsize(path), sf.info(path).duration
    async def save(self, p):
        await asyncio.sleep(SAVE_DELAY["s"])  # 模擬下載錄音需要時間
        Path(p).write_bytes(Path(self.path).read_bytes())


class Msg:
    def __init__(self, uid, content="", clip=None):
        self.author, self.channel = NS(id=uid, bot=False), Ch()
        self.content, self.attachments = content, ([clip] if clip else [])
    async def reply(self, text, **k): out.append(text)


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


async def start(uid):
    core.guard = usage.Guard(usage.Limits(user_rpm=99, rpm=99))
    g.bot.sessions.clear()
    out.clear()
    await g.de_speak.callback(inter(uid), "")


async def main():
    uid, client = 777, g.bot
    usage.current_user.set(uid)
    await g.de_register.callback(inter(uid), app_commands.Choice(name="A2", value="A2"))
    voice = ogg()

    print("== A. 各種打法的 stop（在口說測驗裡）==")
    for typed in ["stop", "Stop", "  stop  ", "STOP", "stop.", "stop!", "Stopp", "stopp", "/stop", "停止", "結束", "ende", "Ende", "aufhören"]:
        await start(uid)
        await client.on_message(Msg(uid, typed))
        print(f"  {typed!r:12} → {'✅ 結束了' if (5, uid) not in client.sessions else '❌ 沒有結束（而且完全沒有回應）'}")

    print("\n== B. 時間差：錄音剛傳出去（Bot 還在下載），馬上接著打 stop ==")
    await start(uid)
    SAVE_DELAY["s"] = 0.4
    out.clear()
    await asyncio.gather(client.on_message(Msg(uid, clip=Att(voice))), client.on_message(Msg(uid, "stop")))
    SAVE_DELAY["s"] = 0.0
    print("  測驗還在進行中嗎？", (5, uid) in client.sessions, "（stop 之後應該是 False）")
    print("  Bot 送出的訊息:", [t[:30].replace("\n", " ") for t in out])
    zombie = any("第 2/5 題" in t for t in out)
    print("  stop 之後 Bot 還在出下一題（殭屍狀態）:", zombie)
    if zombie and (5, uid) not in client.sessions:
        out.clear()
        await client.on_message(Msg(uid, clip=Att(voice)))
        await client.on_message(Msg(uid, "stop"))
        print("  之後你再傳錄音、再打 stop：Bot 的回應 =", out, "→ 完全沒反應，你就再也停不了")

asyncio.run(main())
