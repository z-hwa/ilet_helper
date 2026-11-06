"""德語寶寶：第二隻 Discord bot（自己的 token 與 /de_ 指令）。

與雅斯寶寶共用資料庫、Gemini 額度與用量防護、封鎖名單；啟動方式見 run_all.py。
- 每日例句＋文法解析（/de_today、/de_sentence）
- 針對給過的文法出練習，優先今天的例句（/de_practice）
- 口說練習：只收語音訊息，打字一律忽略（/de_speak）
- 閒聊 gossip：跟 Lena 用語音聊指定回合，每回合即時點評文法，結束評分（/de_gossip，邏輯在 gossip.py）
"""
import asyncio
import datetime
import io
import json
import logging
import os
import random
import re
import time
import unicodedata
import wave
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from google.genai import types

import bot as core  # 共用基礎：資料庫、Gemini 呼叫與用量防護、設定
import german_logic as gl
import gossip
import skills
import usage

log = logging.getLogger("german_bot")

DAILY_CHANNEL_ID = os.getenv("GERMAN_DAILY_CHANNEL_ID", "").strip()
DAILY_TIME = os.getenv("GERMAN_DAILY_TIME", "08:00").strip()
DAILY_LEVEL = os.getenv("GERMAN_DAILY_LEVEL", "A2").strip().upper()
if DAILY_LEVEL not in gl.LEVELS:
    DAILY_LEVEL = "A2"
VOICE_MODEL = Path(os.getenv("GERMAN_VOICE_MODEL", "models/de/de_DE-thorsten-medium.onnx"))
SPEAKING_QUESTIONS = 5
MIN_ANSWERED_TO_SCORE = 2
STOP_WORDS = {"stop", "stopp", "quit", "exit", "end", "ende", "beenden", "aufhören", "結束", "停止", "停", "結束練習"}
SKIP_WORDS = {"skip", "next", "weiter", "überspringen", "ueberspringen", "跳過", "下一題", "略過"}


def command_word(text):
    """把使用者打的字正規化成 stop / skip / None：容許大小寫、前後空白與標點、開頭的 /、全形字。"""
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    t = t.strip(" \t\r\n/!！.。,，:：;；~～*_`\"'「」()（）[]")
    if t in STOP_WORDS:
        return "stop"
    if t in SKIP_WORDS:
        return "skip"
    return None


# ---------------- 德語語音（Piper，本地）----------------
_voices = {}  # 模型路徑 -> 已載入的 PiperVoice（每個約佔 60MB 記憶體，用到才載入）
MULTI_VOICE = os.getenv("GERMAN_MULTI_VOICE", "1").strip() != "0"  # 設 0 就只用 GERMAN_VOICE_MODEL 一個聲音
VOICE_GENDER = {"thorsten": "男", "karlsson": "男", "pavoque": "男", "ramona": "女", "kerstin": "女", "eva_k": "女"}


def tts_available():
    return VOICE_MODEL.exists() and Path(str(VOICE_MODEL) + ".json").exists()


def voice_files():
    """可用的德語聲音：預設聲音排第一，再加上同資料夾裡其他有 .json 設定檔的 Piper 模型（依檔名排序）。"""
    if not MULTI_VOICE:
        return [VOICE_MODEL]
    others = sorted(p for p in VOICE_MODEL.parent.glob("*.onnx")
                    if p != VOICE_MODEL and Path(str(p) + ".json").exists())
    return [VOICE_MODEL, *others]


def voice_label(path):
    """de_DE-ramona-low.onnx -> 'Ramona（女聲）'。"""
    parts = Path(path).name.split("-")
    name = parts[1] if len(parts) > 2 else Path(path).stem
    gender = VOICE_GENDER.get(name)
    return f"{name.split('_')[0].capitalize()}（{gender}聲）" if gender else name.capitalize()


def voice_for(seed=None):
    """依 seed（例如例句 id）固定挑一個聲音，所以同一句的原速、0.5 倍速與單字按鈕永遠是同一個人；沒給 seed 就隨機。"""
    files = voice_files()
    return random.choice(files) if seed is None else files[int(seed) % len(files)]


def synth(texts, out_path, slow=False, scale=None, voice=None):
    """把幾段德語念成一個 wav（段與段之間停 0.4 秒）。voice：模型路徑，不給就用預設聲音。CPU 運算，請用 to_thread 呼叫。"""
    from piper import PiperVoice, SynthesisConfig

    model = str(voice or VOICE_MODEL)
    if model not in _voices:
        _voices[model] = PiperVoice.load(model)
    piper_voice = _voices[model]
    cfg = SynthesisConfig(length_scale=scale or (1.2 if slow else 1.0))  # scale 2.0 = 0.5 倍速
    first = True
    with wave.open(str(out_path), "wb") as wav:
        for text in texts:
            for chunk in piper_voice.synthesize(text, syn_config=cfg):
                if first:
                    wav.setframerate(chunk.sample_rate)
                    wav.setsampwidth(chunk.sample_width)
                    wav.setnchannels(chunk.sample_channels)
                    rate, first = chunk.sample_rate, False
                wav.writeframes(chunk.audio_int16_bytes)
            if not first:
                wav.writeframes(b"\x00\x00" * int(rate * 0.4))
    return Path(out_path)


# ---------------- 資料庫 ----------------
def db():
    return core.db_connect()


def today():
    return datetime.date.today().isoformat()


def get_profile(user):
    with db() as c:
        row = c.execute("SELECT level FROM de_profile WHERE user_id = ?", (str(user),)).fetchone()
    return row[0] if row else None


def set_profile(user, level):
    with db() as c:
        c.execute(
            "INSERT INTO de_profile (user_id, level, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET level = excluded.level",
            (str(user), level, core.now_str()),
        )


SENTENCE_COLS = ("id", "kind", "user_id", "day", "level", "topic", "german", "chinese", "situation",
                 "words", "grammar", "tip", "posted")


def _sentence(row):
    s = dict(zip(SENTENCE_COLS, row))
    s["words"] = json.loads(s["words"] or "[]")
    s["grammar"] = json.loads(s["grammar"] or "[]")
    return s


def save_sentence(kind, user, level, topic, s):
    with db() as c:
        cur = c.execute(
            "INSERT INTO de_sentences (kind, user_id, day, level, topic, german, chinese, situation, words, grammar, "
            "tip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, str(user) if user else None, today(), level, topic, s["german"], s["chinese"], s["situation"],
             json.dumps(s["words"], ensure_ascii=False), json.dumps(s["grammar"], ensure_ascii=False), s["tip"],
             core.now_str()),
        )
        row = c.execute(f"SELECT {', '.join(SENTENCE_COLS)} FROM de_sentences WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _sentence(row)


def get_daily(day=None):
    with db() as c:
        row = c.execute(
            f"SELECT {', '.join(SENTENCE_COLS)} FROM de_sentences WHERE kind = 'daily' AND day = ? ORDER BY id LIMIT 1",
            (day or today(),),
        ).fetchone()
    return _sentence(row) if row else None


def mark_posted(sentence_id):
    with db() as c:
        c.execute("UPDATE de_sentences SET posted = 1 WHERE id = ?", (sentence_id,))


def recent_sentences(user, days=30, limit=60):
    """這位使用者「給過」的例句：最近幾天的每日例句，加上他自己要過的。新的在前面。"""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with db() as c:
        rows = c.execute(
            f"SELECT {', '.join(SENTENCE_COLS)} FROM de_sentences WHERE day >= ? AND "
            "(kind = 'daily' OR (kind = 'user' AND user_id = ?)) ORDER BY id DESC LIMIT ?",
            (since, str(user), limit),
        ).fetchall()
    return [_sentence(r) for r in rows]


def recent_german(limit=40):
    """最近出過的句子（每日、/de_sentence 全部算，所有人合在一起），用來避免重複。"""
    with db() as c:
        return [r[0] for r in c.execute("SELECT german FROM de_sentences ORDER BY id DESC LIMIT ?", (limit,))]


def save_question(user, question, answer, sentence_id):
    with db() as c:
        c.execute(
            "INSERT INTO de_questions (user_id, question, answer, sentence_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(user), question, answer, sentence_id, core.now_str()),
        )


def recent_questions(user, hours=2, limit=3):
    """這位使用者最近問過的幾題（舊的在前），讓追問「那這個呢？」有上下文。"""
    since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        rows = c.execute(
            "SELECT question, answer FROM de_questions WHERE user_id = ? AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (str(user), since, limit),
        ).fetchall()
    return rows[::-1]


def grammar_points(sentences):
    seen, points = set(), []
    for s in sentences:
        for g in s["grammar"]:
            if g["key"] not in seen:
                seen.add(g["key"])
                points.append(g)
    return points


def grammar_stats(user):
    with db() as c:
        return {k: (cor, tot) for k, cor, tot in c.execute(
            "SELECT key, correct, total FROM de_grammar_stats WHERE user_id = ?", (str(user),))}


def bump_grammar(user, key, name, correct):
    with db() as c:
        c.execute(
            "INSERT INTO de_grammar_stats (user_id, key, name, correct, total, last_seen) VALUES (?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET correct = correct + excluded.correct, total = total + 1, "
            "name = excluded.name, last_seen = excluded.last_seen",
            (str(user), key, name, int(correct), core.now_str()),
        )


# ---------------- 程度評級（演算法與雅斯寶寶相同，見 skills.py）----------------
DE_SKILLS = {"de_grammar": "文法", "de_speaking": "口說"}
DE_EMOJI = {"de_grammar": "📚", "de_speaking": "🗣️"}


def load_de_rating(skill, user):
    """目前評級；沒練過就從註冊時選的 CEFR 程度開始。太久沒練習，不確定度會自動回升。"""
    with db() as c:
        row = c.execute(
            "SELECT rating, rd, updated_at FROM skill_ratings WHERE user_id = ? AND skill = ?", (str(user), skill)
        ).fetchone()
    if not row:
        return skills.Rating(gl.CEFR_CENTER[get_profile(user) or "A2"])
    rating, rd, updated = row
    idle = datetime.datetime.now() - datetime.datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
    return skills.Rating(rating, skills.inflate(rd, idle.total_seconds() / 86400))


def update_de_items(skill, items, user, source, detail=""):
    """答對/答錯類的題目：items = [(得分 0~1, 題目難度)]。"""
    before = load_de_rating(skill, user)
    after = skills.update_from_items(before, items)
    core.save_rating(skill, after, source, detail, before, str(user))
    return before, after


def update_de_band(skill, value, sigma, user, source, detail=""):
    """口說：Gemini 估的 CEFR 程度（已轉成數值）。"""
    before = load_de_rating(skill, user)
    after = skills.update_from_band(before, value, sigma)
    core.save_rating(skill, after, source, detail, before, str(user))
    return before, after


def de_change_line(skill, before, after):
    delta = after.rating - before.rating
    arrow = "📈" if delta > 0.04 else "📉" if delta < -0.04 else "➖"
    return (f"{arrow} 德語{DE_SKILLS[skill]} {gl.cefr_score(before.rating)} → {gl.cefr_score(after.rating)}"
            f"（±{after.rd:.1f}）")


def level_for(skill, user):
    """出題用的 CEFR 等級：依目前評級，不是註冊時填的那個。"""
    return gl.content_level(load_de_rating(skill, user).rating)


def render_chart(user):
    """文法與口說的評級曲線（陰影是不確定度）。沒有足夠紀錄時回傳 None。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    plotted = False
    for skill, name in (("de_grammar", "Grammar"), ("de_speaking", "Speaking")):
        rows = core.skill_history_rows(skill, user=str(user))
        if len(rows) < 2:
            continue
        xs = [datetime.datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        ys, rds = [r[1] for r in rows], [r[2] for r in rows]
        ax.plot(xs, ys, marker="o", markersize=3, label=name)
        ax.fill_between(xs, [y - d for y, d in zip(ys, rds)], [y + d for y, d in zip(ys, rds)], alpha=0.12)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.set_yticks([i + 0.5 for i in range(1, 7)])
    ax.set_yticklabels(gl.LEVELS6)
    for boundary in range(1, 8):
        ax.axhline(boundary, color="gray", linewidth=0.4, alpha=0.5)
    ax.set_ylim(1, 7)
    ax.set_ylabel("CEFR level (shaded = uncertainty)")
    ax.set_title("German level curve")
    ax.legend()
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------- 問 Gemini 並解析（格式有問題就重問一次）----------------
RETRY_HINT = "\n（上一次回傳的 JSON 格式有誤。請嚴格輸出合法的 JSON：不要多餘的逗號、不要註解，字串裡的雙引號要跳脫。）"


def _with_hint(contents):
    """重問時在提示詞後面加一句話：順便避開用量防護對「完全相同請求」的攔截。"""
    if isinstance(contents, str):
        return contents + RETRY_HINT
    contents = list(contents)
    contents[0] = str(contents[0]) + RETRY_HINT
    return contents


async def ask_parsed(contents, parse, *, feature, system=None, temperature=0.7, attempts=2):
    """問 Gemini 並解析。回傳格式有問題（壞掉的 JSON、有效題目不足…）時換個說法再問，最多 attempts 次。"""
    last = None
    for attempt in range(attempts):
        if attempt:
            contents = _with_hint(contents)
        text = await core.ask_gemini(contents, system, json_mode=True, temperature=temperature, feature=feature)
        try:
            return text, parse(str(text))
        except ValueError as e:  # json.JSONDecodeError 也是 ValueError
            last = e
            log.warning("%s 的回傳格式有問題（%s）：%s", feature, "重問一次" if attempt + 1 < attempts else "放棄", e)
    raise last


# ---------------- 例句 ----------------
_daily_lock = asyncio.Lock()


async def make_sentence(level, topic, kind, user=None, tense=""):
    """請 Gemini 生一個例句並存起來。每日例句不屬於任何人（user=None）。
    tense：gl.TENSES 的 key、"random"（隨機抽一個時態）或空字串（不指定）。"""
    previous = recent_german(200)
    tense = gl.pick_tense(tense)
    for attempt in range(3):  # 跟以前的句子一樣或太像、或沒用到指定時態，就重出（最多 3 次）
        text, data = await ask_parsed(
            gl.build_sentence_prompt(level, topic, previous[:40], tense), gl.parse_sentence,
            feature="de_sentence", temperature=1.0,
        )
        core.mark_call(text.call_id, 1, 1)
        if not gl.sentence_too_similar(data["german"], previous) and gl.tense_ok(data["german"], tense):
            break
        core.log_event("german", "duplicate_sentence" if gl.sentence_too_similar(data["german"], previous) else "wrong_tense",
                       kind, user, None, {"german": data["german"], "tense": tense, "attempt": attempt + 1})
    sentence = save_sentence(kind, user, level, topic, data)
    sentence["tense"] = tense
    return sentence


def spoken_word(entry):
    """單字按鈕要念的字：去掉括號補充。"""
    return re.sub(r"\(.*?\)", "", entry).strip()


class PronounceView(discord.ui.View):
    """例句下面的按鈕：🔊 點單字聽發音（整句的原速與 0.5 倍速已經附在訊息裡）。回覆只有按的人看得到（ephemeral），不洗頻。"""

    def __init__(self, sentence):
        super().__init__(timeout=6 * 3600)
        self.german = sentence["german"]
        self.voice = voice_for(sentence.get("id"))  # 和整句同一個聲音
        seen = set()
        for w in sentence.get("words", [])[:6]:
            word = spoken_word(w["de"])
            if not word or word.lower() in seen:
                continue
            seen.add(word.lower())
            button = discord.ui.Button(label=f"🔊 {word}"[:80], style=discord.ButtonStyle.secondary)
            button.callback = self.make_callback(word, 1.3, "wort.wav")  # 單字念慢一點才聽得清楚
            self.add_item(button)

    def make_callback(self, text, scale, filename):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            path = core.AUDIO_DIR / f"de_btn_{interaction.id}.wav"
            try:
                await asyncio.to_thread(synth, [text], path, False, scale, self.voice)
                await interaction.followup.send(f"🔊 {text}", file=discord.File(path, filename=filename), ephemeral=True)
                core.log_event("german", "button_audio", filename, interaction.user.id, interaction.channel_id,
                               {"text": text, "voice": Path(self.voice).name})
            except Exception as e:
                log.exception("按鈕配音失敗")
                core.log_error("german", "button_audio", e, interaction.user.id, interaction.channel_id, text=text)
                await interaction.followup.send("⚠️ 配音失敗了，請再試一次。", ephemeral=True)
            finally:
                path.unlink(missing_ok=True)
        return callback


async def send_sentence(send, sentence, title):
    """送出例句：第一則是句子＋翻譯，**發音緊接在它下面**（同一則訊息），之後才是單字與文法解析。

    send：channel.send 或 interaction.followup.send。
    """
    paths = []  # [(路徑, 檔名)]：原本速度、0.5 倍速
    voice = voice_for(sentence["id"])
    if tts_available():
        stamp = f"{sentence['id']}_{int(time.time() * 1000)}"
        for suffix, filename, kwargs in (("", "satz.wav", {"slow": sentence["level"] in ("A1", "A2")}),
                                         ("_slow", "satz_0.5x.wav", {"scale": 2.0})):
            candidate = core.AUDIO_DIR / f"de_sentence_{stamp}{suffix}.wav"
            try:
                await asyncio.to_thread(synth, [sentence["german"]], candidate, voice=voice, **kwargs)
                paths.append((candidate, filename))
            except Exception as e:
                log.exception("例句配音失敗")
                core.log_error("german", "sentence_audio", e, voice=Path(voice).name)
                candidate.unlink(missing_ok=True)
    head, body = gl.format_sentence_head(sentence, title), gl.format_sentence_body(sentence)
    if paths and len(voice_files()) > 1:
        head += f"\n🎙️ 朗讀：{voice_label(voice)}"
    # Discord 的附件一定顯示在訊息文字的最下面，所以：第一則 = 句子＋翻譯＋情境＋發音（發音緊接在句子下面），
    # 第二則 = 單字與文法解析
    chunks = [head, *core.chunk_text(body)]
    try:
        for i, chunk in enumerate(chunks):
            if i == 0 and paths:
                await send(chunk, files=[discord.File(p, filename=n) for p, n in paths], view=PronounceView(sentence))
            else:
                await send(chunk)
    finally:
        for p, _ in paths:
            p.unlink(missing_ok=True)


# ---------------- 練習 ----------------
@dataclass
class PracticeSession:
    items: list
    db_id: int
    call_id: int = None
    names: dict = field(default_factory=dict)  # 文法 key -> 名稱
    index: int = 0
    correct: int = 0
    missed: list = field(default_factory=list)
    scores: list = field(default_factory=list)  # [(得分 0~1, 題目難度)]，結束時用來更新評級
    start_rating: float = 2.5  # 題目沒標難度時，用開始時的評級代替
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 同一個人的訊息一次處理一則
    last_active: float = field(default_factory=time.monotonic)


def create_practice_row(user, total, focus):
    with db() as c:
        return c.execute(
            "INSERT INTO de_practice_sessions (user_id, started_at, total, focus) VALUES (?, ?, ?, ?)",
            (str(user), core.now_str(), total, focus),
        ).lastrowid


def log_practice_answer(session_id, seq, ex, user_answer, result):
    with db() as c:
        c.execute(
            "INSERT INTO de_practice_answers (session_id, seq, grammar_key, qtype, prompt, expected, user_answer, "
            "result, explanation, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, seq, ex["grammar_key"], ex["type"], ex["prompt"], gl.answer_display(ex), user_answer,
             result, ex["explanation"], core.now_str()),
        )


def close_practice_row(session_id, status):
    with db() as c:
        answered, correct = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(result = 'correct'), 0) FROM de_practice_answers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        c.execute(
            "UPDATE de_practice_sessions SET ended_at = ?, answered = ?, correct = ?, status = ? WHERE id = ?",
            (core.now_str(), answered, correct, status, session_id),
        )


def finish_practice(client, key, session, reason="stop"):
    client.sessions.pop(key, None)
    user = key[1]
    answered = session.index
    status = "completed" if answered >= len(session.items) else ("timeout" if reason == "timeout" else "stopped")
    close_practice_row(session.db_id, status)
    core.mark_consumed(session.call_id, answered)
    text = f"🏁 **德語練習結束**：{session.correct}/{answered} 題答對"
    if session.missed:
        names = sorted({session.names.get(k, k) for k in session.missed})
        text += "\n需要再加強的文法：" + "、".join(names)
    if answered >= core.MIN_ANSWERED_TO_RATE:
        before, after = update_de_items("de_grammar", session.scores, user, "de_practice", f"practice #{session.db_id}")
        text += "\n" + de_change_line("de_grammar", before, after)
    else:
        text += f"\n（答滿 {core.MIN_ANSWERED_TO_RATE} 題才會更新程度評級）"
    return text + "\n想再練一輪可以用 `/de_practice`，`/de_level` 看程度，`/de_stats` 看各文法點的答對率。"


async def handle_practice(client, message, session):
    key = (message.channel.id, message.author.id)
    text = message.content.strip()
    word = command_word(text)
    if word == "stop":
        await core.reply_long(message, finish_practice(client, key, session))
        return
    ex = session.items[session.index]
    skipped = word == "skip"
    result, detail = ("skip", "") if skipped else gl.check_answer(ex, text)
    if result is None:
        await core.reply_long(message, "請輸入 A / B / C / D（或 `skip`、`stop`）。")
        return

    correct = result == "correct"
    session.correct += correct
    user = message.author.id
    bump_grammar(user, ex["grammar_key"], session.names.get(ex["grammar_key"], ex["grammar_key"]), correct)
    if not correct:
        session.missed.append(ex["grammar_key"])
    log_practice_answer(session.db_id, session.index + 1, ex, "" if skipped else text, result)
    session.scores.append((1.0 if correct else 0.0, ex.get("difficulty") or session.start_rating))

    if correct:
        reply = "✅ 答對了！"
    elif result == "spelling":
        reply = f"❌ 差一點（{detail}）。你打的是 `{text}`，正確答案是 **{gl.answer_display(ex)}**"
    else:
        reply = f"{'⏭️ 跳過' if skipped else '❌ 不對'}，答案是 **{gl.answer_display(ex)}**"
    if ex["explanation"]:
        reply += "\n" + ex["explanation"]

    session.index += 1
    if session.index >= len(session.items):
        reply += "\n\n" + finish_practice(client, key, session)
    else:
        reply += "\n\n" + gl.format_exercise(session.items[session.index], session.index, len(session.items))
    await core.reply_long(message, reply)


# ---------------- 口說（只收語音訊息）----------------
@dataclass
class SpeakSession:
    topic: str
    level: str
    steps: list  # {"question", "hint_zh", "prefix", "long", "audio"}
    closing_audio: str
    db_id: int
    call_id: int = None
    index: int = 0
    answers: list = field(default_factory=list)  # {"seq", "step", "path", "mime", "seconds", "skipped"}
    last_active: float = field(default_factory=time.monotonic)
    hinted: bool = False  # 打字被忽略時只提醒一次
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 語音與 stop 同時到時不會搶著改狀態

    @property
    def current(self):
        return self.steps[self.index]


def create_speaking_row(user, topic, level, total):
    with db() as c:
        return c.execute(
            "INSERT INTO de_speaking_sessions (user_id, started_at, topic, level, total) VALUES (?, ?, ?, ?, ?)",
            (str(user), core.now_str(), topic, level, total),
        ).lastrowid


def log_speaking_answer(session_id, seq, question, seconds, skipped):
    with db() as c:
        c.execute(
            "INSERT INTO de_speaking_answers (session_id, seq, question, seconds, skipped, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, seq, question, seconds, int(skipped), core.now_str()),
        )


def close_speaking_row(session_id, status, estimate=None, criteria=None, report=None, transcripts=None):
    with db() as c:
        answered = c.execute(
            "SELECT COUNT(*) FROM de_speaking_answers WHERE session_id = ? AND skipped = 0", (session_id,)
        ).fetchone()[0]
        c.execute(
            "UPDATE de_speaking_sessions SET ended_at = ?, answered = ?, estimate = ?, criteria = ?, report = ?, "
            "status = ? WHERE id = ?",
            (core.now_str(), answered, estimate, json.dumps(criteria or {}), report, status, session_id),
        )
        for seq, text in enumerate(transcripts or [], start=1):
            c.execute(
                "UPDATE de_speaking_answers SET transcript = ? WHERE session_id = ? AND seq = ?",
                (str(text or ""), session_id, seq),
            )


def cleanup_speaking(session):
    for step in session.steps:
        Path(step["audio"]).unlink(missing_ok=True) if step.get("audio") else None
    if session.closing_audio:
        Path(session.closing_audio).unlink(missing_ok=True)
    for a in session.answers:
        if a.get("path"):
            Path(a["path"]).unlink(missing_ok=True)


def speak_step_text(session):
    step = session.current
    hint = f"（中文大意：||{step['hint_zh']}||）" if step["hint_zh"] else ""
    return (
        f"**第 {session.index + 1}/{len(session.steps)} 題**\n"
        f"🎧 聽問題，用**語音訊息**回答。想核對文字：||{step['question']}||{hint}\n"
        "（`skip` 跳過、`stop` 結束）"
    )


async def send_with_audio(message, text, path, filename="frage.wav"):
    try:
        await message.reply(text, file=discord.File(path, filename=filename))
    except discord.Forbidden:
        await message.channel.send(text, file=discord.File(path, filename=filename))


@dataclass
class SpeakReport:
    messages: list  # 總評與建議例句
    session: SpeakSession
    items: list = field(default_factory=list)  # [{"texts": [...], "files": [(路徑, 檔名)]}]：每題的逐字稿與語音檔

    def cleanup(self):
        cleanup_speaking(self.session)


async def finish_speaking(client, key, session, reason="stop"):
    """評分並存檔，回傳 SpeakReport。錄音要等報告送出去之後才刪（逐字稿旁邊會附上語音檔），請用 deliver_report 送。"""
    client.sessions.pop(key, None)
    answered = [a for a in session.answers if not a["skipped"] and a["path"]]
    status = "completed" if session.index >= len(session.steps) else ("timeout" if reason == "timeout" else "stopped")
    core.mark_consumed(session.call_id, session.index)
    try:
        if len(answered) < MIN_ANSWERED_TO_SCORE:
            close_speaking_row(session.db_id, status)
            return SpeakReport(
                [f"🏁 **德語口說結束**：只回答了 {len(answered)} 題，不足 {MIN_ANSWERED_TO_SCORE} 題，這次不評分。"], session
            )

        contents = [f"這是一場德語口說練習（CEFR {session.level}），主題：{session.topic}。以下依序是每一題與學生的語音回答。"]
        for a in session.answers:
            contents.append(f"第 {a['seq']} 題：{a['step']['question']}")
            if a["skipped"]:
                contents.append("（學生跳過這題，沒有錄音）")
            else:
                contents.append(types.Part.from_bytes(data=Path(a["path"]).read_bytes(), mime_type=a["mime"]))
        try:
            text, data = await ask_parsed(
                contents, gl.parse_scoring, system=gl.SCORING_SYSTEM, feature="de_speak_score", temperature=0.3
            )
        except usage.UsageLimitError as e:
            close_speaking_row(session.db_id, "unscored")
            return SpeakReport([f"🚦 {e}\n這次的錄音無法評分，已丟棄；等額度恢復後再練一次吧。"], session)
        except Exception as e:
            log.warning("德語口說評分失敗：%s", e)
            close_speaking_row(session.db_id, "unscored")
            return SpeakReport(["⚠️ 評分失敗（可能是額度、網路或回傳格式的問題），這次無法評分，請稍後再練一次。"], session)

        transcripts = data.get("transcripts") if isinstance(data.get("transcripts"), list) else []
        core.mark_call(text.call_id, len(answered), sum(1 for t in transcripts if str(t).strip()))
        qa = [{"seq": a["seq"], "question": a["step"]["question"], "skipped": a["skipped"]} for a in session.answers]
        built = gl.build_speaking_report(data, qa)
        close_speaking_row(session.db_id, status, estimate=gl.clean(data.get("level_estimate")),
                           criteria=data.get("criteria"), report=built["markdown"], transcripts=transcripts)

        messages, tail = built["messages"], ""
        estimate = gl.cefr_to_number(data.get("level_estimate"))
        if estimate is not None:
            before, after = update_de_band("de_speaking", estimate, 0.6, key[1], "de_speak", f"speaking #{session.db_id}")
            tail = "\n\n" + de_change_line("de_speaking", before, after)
        messages[0] = "🏁 **德語口說結束**\n\n" + messages[0] + tail

        # 每題的逐字稿旁邊附上「老師的問題」與「你的錄音」，可以直接重聽
        by_seq = {a["seq"]: a for a in session.answers}
        items = []
        for t in built["transcripts"]:
            a = by_seq[t["seq"]]
            files = [(a["step"]["audio"], f"frage_{a['seq']}.wav")]
            if a["path"]:
                files.append((a["path"], f"antwort_{a['seq']}{Path(a['path']).suffix or '.ogg'}"))
            items.append({"texts": t["texts"], "files": files})
        return SpeakReport(messages, session, items)
    except Exception:
        cleanup_speaking(session)
        raise


async def deliver_report(report, *, message=None, channel=None):
    """依序送出口說報告。message 有給的話，第一則用回覆；之後都直接發在頻道。送完才清掉暫存的語音檔。"""
    channel = channel or message.channel
    reply_pending = message is not None

    async def post(text, files=()):
        nonlocal reply_pending
        make = lambda: {"files": [discord.File(p, filename=n) for p, n in files]} if files else {}
        if reply_pending:
            reply_pending = False
            try:
                await message.reply(text, **make())
                return
            except discord.Forbidden:
                pass  # 缺『讀取訊息歷史記錄』權限：改成直接發
        await channel.send(text, **make())

    try:
        for text in report.messages:
            for chunk in core.chunk_text(text):
                await post(chunk)
        for item in report.items:
            for i, text in enumerate(item["texts"]):
                await post(text, item["files"] if i == 0 else ())
    finally:
        report.cleanup()


async def handle_speaking(client, message, session):
    """只收語音訊息：打字一律忽略（使用者可能在打講稿），只有 skip / stop 這兩個指令例外。"""
    key = (message.channel.id, message.author.id)
    word = command_word(message.content)
    if word == "stop":
        core.log_event("german", "speak_stop", "stop", message.author.id, message.channel.id, {"text": message.content[:50]})
        await deliver_report(await finish_speaking(client, key, session), message=message)
        return

    clip = core.audio_attachment(message)
    step = session.current
    skipped, path, mime, seconds = False, None, None, None
    if clip:
        if clip.size > core.MAX_AUDIO_BYTES:
            await core.reply_long(message, "這個音檔太大了，請控制在 15MB 以內。")
            return
        ext = Path(clip.filename).suffix.lower() or ".ogg"
        path = core.AUDIO_DIR / f"de_spk_{session.db_id}_{session.index + 1}{ext}"
        await clip.save(path)
        mime = core.AUDIO_MIME.get(ext) or (clip.content_type or "audio/ogg").split(";")[0]
        seconds = getattr(clip, "duration", None)
    elif word == "skip":
        skipped = True
    else:
        # 打字（可能是講稿）：不當成答案；只在第一次提醒，之後不回應，免得洗頻
        core.log_event("german", "ignored_text", "speaking", message.author.id, message.channel.id,
                       {"text": message.content[:80], "attachments": len(message.attachments)})
        if not session.hinted and message.content.strip():
            session.hinted = True
            await core.reply_long(message, "🎙️ 口說模式只收語音訊息，打字會被忽略。要跳過這題打 `skip`，要結束打 `stop`。")
        return

    session.answers.append({"seq": session.index + 1, "step": step, "path": str(path) if path else None,
                            "mime": mime, "seconds": seconds, "skipped": skipped})
    log_speaking_answer(session.db_id, session.index + 1, step["question"], seconds, skipped)
    session.index += 1
    if session.index >= len(session.steps):
        await send_with_audio(message, "🎙️ 收到了，老師正在評分，大約要半分鐘…", session.closing_audio)
        await deliver_report(await finish_speaking(client, key, session), message=message)
    else:
        await send_with_audio(message, speak_step_text(session), session.current["audio"])


# ---------------- gossip 閒聊（只收語音訊息；邏輯在 gossip.py）----------------
@dataclass
class GossipSession:
    topic: str
    level: str
    rounds: int
    voice: str
    opening: dict  # {"topic", "german", "english"}：Lena 的開場白
    db_id: int
    turns: list = field(default_factory=list)  # 每個完成的回合：parse_turn 的結果＋seq、teacher_de/en、path、mime、seconds
    attempts: int = 0  # 送過幾次 Gemini（沒聽清楚而重傳時，讓請求內容不一樣，避免被重複請求防護擋掉）
    last_active: float = field(default_factory=time.monotonic)
    hinted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def create_gossip_row(user, topic, level, rounds):
    with db() as c:
        return c.execute(
            "INSERT INTO de_gossip_sessions (user_id, started_at, topic, level, rounds) VALUES (?, ?, ?, ?, ?)",
            (str(user), core.now_str(), topic, level, rounds),
        ).lastrowid


def log_gossip_turn(session_id, turn):
    with db() as c:
        c.execute(
            "INSERT INTO de_gossip_turns (session_id, seq, transcript, transcript_en, status, errors, verdict, better, "
            "pronunciation_note, teacher_de, teacher_en, seconds, call_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn["seq"], turn["transcript"], turn["transcript_en"], turn["status"],
             json.dumps(turn["errors"], ensure_ascii=False), turn["verdict"], turn["better"], turn["pronunciation_note"],
             turn["teacher_de"], turn["teacher_en"], turn.get("seconds"), turn.get("call_id"), core.now_str()),
        )


def close_gossip_row(session_id, status, answered, estimate=None, criteria=None, report=None):
    with db() as c:
        c.execute(
            "UPDATE de_gossip_sessions SET ended_at = ?, answered = ?, estimate = ?, criteria = ?, report = ?, "
            "status = ? WHERE id = ?",
            (core.now_str(), answered, estimate, json.dumps(criteria or {}), report, status, session_id),
        )


@dataclass
class GossipReport:
    messages: list  # 總評與建議例句
    session: GossipSession
    items: list = field(default_factory=list)  # [{"texts": [...], "files": [(路徑, 檔名)]}]：每回合的逐字稿與錄音

    def cleanup(self):
        for t in self.session.turns:
            if t.get("path"):
                Path(t["path"]).unlink(missing_ok=True)


async def gossip_audio(session, text, name):
    """Lena 的話配成語音（本地 Piper）；失敗回傳 None，訊息照樣送文字。"""
    path = core.AUDIO_DIR / f"de_gsp_{session.db_id}_{name}.wav"
    try:
        await asyncio.to_thread(synth, [text], path, session.level in ("A1", "A2"), None, session.voice)
        return path
    except Exception as e:
        log.exception("閒聊配音失敗")
        core.log_error("german", "gossip_audio", e, text=text[:80])
        path.unlink(missing_ok=True)
        return None


async def send_gossip(message, text, path):
    """送出一則訊息；語音附在最後一段（Discord 的附件一定顯示在文字下面）。送完刪掉語音檔。"""
    chunks = core.chunk_text(text) or ["（沒有內容）"]
    try:
        for chunk in chunks[:-1]:
            await message.channel.send(chunk)
        if path:
            await send_with_audio(message, chunks[-1], path, "lena.wav")
        else:
            await core.reply_long(message, chunks[-1])
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def finish_gossip(client, key, session, reason="stop"):
    """評分並存檔，回傳 GossipReport（送完報告才會刪掉錄音，用 deliver_report 送）。"""
    client.sessions.pop(key, None)
    turns = session.turns
    status = "completed" if len(turns) >= session.rounds else ("timeout" if reason == "timeout" else "stopped")
    try:
        if len(turns) < gossip.MIN_ANSWERED_TO_SCORE:
            close_gossip_row(session.db_id, status, len(turns))
            return GossipReport([f"🏁 **德語閒聊結束**：只聊了 {len(turns)} 回合，不足 {gossip.MIN_ANSWERED_TO_SCORE} 回合，這次不評分。"], session)
        try:
            text, data = await ask_parsed(
                gossip.build_final_text(session.level, session.topic, turns), gl.parse_scoring,
                system=gossip.FINAL_SYSTEM, feature="de_gossip_score", temperature=0.3,
            )
        except usage.UsageLimitError as e:
            close_gossip_row(session.db_id, "unscored", len(turns))
            return GossipReport([f"🚦 {e}\n這次的對話無法評分；等額度恢復後再聊一次吧。"], session)
        except Exception as e:
            log.warning("德語閒聊評分失敗：%s", e)
            core.log_error("german", "gossip_score", e)
            close_gossip_row(session.db_id, "unscored", len(turns))
            return GossipReport(["⚠️ 評分失敗（可能是額度、網路或回傳格式的問題），這次無法評分，請稍後再聊一次。"], session)

        core.mark_call(text.call_id, 1, 1)
        opening = {"seq": 0, "teacher_de": session.opening["german"], "teacher_en": session.opening["english"],
                   "transcript": "", "transcript_en": ""}
        built = gossip.build_report(data, [opening, *turns])
        close_gossip_row(session.db_id, status, len(turns), estimate=gl.clean(data.get("level_estimate")),
                         criteria=data.get("criteria"), report=built["markdown"])
        messages, tail = built["messages"], ""
        estimate = gl.cefr_to_number(data.get("level_estimate"))
        if estimate is not None:
            before, after = update_de_band("de_speaking", estimate, 0.7, key[1], "de_gossip", f"gossip #{session.db_id}")
            tail = "\n\n" + de_change_line("de_speaking", before, after)
        messages[0] = "🏁 **德語閒聊結束**\n\n" + messages[0] + tail

        by_seq = {t["seq"]: t for t in turns}
        items = []
        for item in built["items"]:
            t = by_seq.get(item["seq"])
            files = [(t["path"], f"antwort_{t['seq']}{Path(t['path']).suffix or '.ogg'}")] if t and t.get("path") else []
            items.append({"texts": item["texts"], "files": files})
        return GossipReport(messages, session, items)
    except Exception:
        GossipReport([], session).cleanup()
        raise


async def handle_gossip(client, message, session):
    """只收語音訊息：每回合即時點評文法並由 Lena 接話；指定回合數到了自動結算，stop 可提前結束。"""
    key = (message.channel.id, message.author.id)
    if command_word(message.content) == "stop":
        core.log_event("german", "gossip_stop", "stop", message.author.id, message.channel.id, {"turns": len(session.turns)})
        await deliver_report(await finish_gossip(client, key, session), message=message)
        return

    clip = core.audio_attachment(message)
    if not clip:
        core.log_event("german", "ignored_text", "gossip", message.author.id, message.channel.id,
                       {"text": message.content[:80], "attachments": len(message.attachments)})
        if not session.hinted and message.content.strip():
            session.hinted = True
            await core.reply_long(message, "🎙️ 閒聊模式只收語音訊息，打字會被忽略。要提前結束打 `stop`。")
        return
    if clip.size > core.MAX_AUDIO_BYTES:
        await core.reply_long(message, "這個音檔太大了，請控制在 15MB 以內。")
        return

    round_no = len(session.turns) + 1
    session.attempts += 1
    ext = Path(clip.filename).suffix.lower() or ".ogg"
    path = core.AUDIO_DIR / f"de_gsp_{session.db_id}_u{round_no}_{session.attempts}{ext}"
    await clip.save(path)
    mime = core.AUDIO_MIME.get(ext) or (clip.content_type or "audio/ogg").split(";")[0]
    seconds = getattr(clip, "duration", None)
    contents = [gossip.build_turn_text(session.opening, session.turns, session.attempts),
                types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)]
    try:
        async with message.channel.typing():
            text, turn = await ask_parsed(
                contents, gossip.parse_turn, system=gossip.turn_system(session.level, session.topic, round_no, session.rounds),
                feature="de_gossip_turn", temperature=0.7,
            )
    except usage.UsageLimitError as e:
        path.unlink(missing_ok=True)
        await core.reply_long(message, f"🚦 {e}\n這則語音沒有算進去，稍後再傳一次。")
        return
    except Exception as e:
        path.unlink(missing_ok=True)
        log.exception("德語閒聊回合處理失敗")
        core.log_error("german", "gossip_turn", e, message.author.id, message.channel.id, round=round_no)
        await core.reply_long(message, "⚠️ 這個回合處理失敗了（可能是網路或 AI 暫時忙碌），這則語音沒有算進去，請再傳一次。")
        return

    core.mark_call(text.call_id, 1, 0 if turn["status"] == "unclear" else 1)
    reply_de, reply_en = turn.pop("reply"), turn.pop("reply_en")
    if turn["status"] == "unclear":  # 沒聽清楚：這回合不算，Lena 請他再說一次
        path.unlink(missing_ok=True)
        core.log_event("german", "gossip_unclear", "", message.author.id, message.channel.id, {"round": round_no})
        audio = await gossip_audio(session, reply_de, f"r{round_no}_{session.attempts}")
        await send_gossip(message, "🔁 這一段沒聽清楚，這回合不算。\n\n" + gossip.format_teacher(reply_de, reply_en), audio)
        return

    turn.update(seq=round_no, teacher_de=reply_de, teacher_en=reply_en, path=str(path), mime=mime,
                seconds=seconds, call_id=text.call_id)
    session.turns.append(turn)
    log_gossip_turn(session.db_id, turn)
    core.log_event("german", "gossip_turn", turn["status"], message.author.id, message.channel.id,
                   {"round": round_no, "errors": len(turn["errors"]), "chars": len(turn["transcript"])})
    done = round_no >= session.rounds
    body = gossip.format_turn(turn, round_no, session.rounds) + "\n\n" + gossip.format_teacher(reply_de, reply_en)
    if done:
        body += "\n\n🏁 回合到了！Lena 正在整理評分，大約要半分鐘…"
    audio = await gossip_audio(session, reply_de, f"r{round_no}")
    await send_gossip(message, body, audio)
    if done:
        await deliver_report(await finish_gossip(client, key, session), message=message)


# ---------------- Discord ----------------
class GermanTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        usage.current_user.set(interaction.user.id)
        if core.ALLOWED_GUILD_IDS and interaction.guild_id and str(interaction.guild_id) not in core.ALLOWED_GUILD_IDS:
            core.log_command("german", interaction, False, "未授權的伺服器")
            await interaction.response.send_message("這個伺服器沒有被授權使用這個 Bot。", ephemeral=True)
            return False
        name = interaction.command.name if interaction.command else ""
        if name == "de_register":
            core.log_command("german", interaction)
            return True
        blocked = core.accounts.get_user(core.db_connect, interaction.user.id)
        if blocked and blocked["blocked"]:  # 雅思寶寶那邊的封鎖名單這裡也適用
            core.log_command("german", interaction, False, "帳號被停用")
            await interaction.response.send_message("⛔ 你的帳號已被管理員停用。", ephemeral=True)
            return False
        if get_profile(interaction.user.id) is None:
            core.log_command("german", interaction, False, "尚未設定德語程度")
            await interaction.response.send_message(
                "👋 你還沒設定德語程度。先用 `/de_register` 選一個 CEFR 程度（A1~C1）就能開始了！", ephemeral=True
            )
            return False
        core.log_command("german", interaction)
        return True


class GermanBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # 要讀使用者在頻道打的答案與語音訊息
        super().__init__(intents=intents)
        self.tree = GermanTree(self)
        self.sessions = {}  # (channel_id, user_id) -> PracticeSession | SpeakSession

    async def setup_hook(self):
        if core.GUILD_ID:
            guild = discord.Object(id=int(core.GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
        self._tasks = [asyncio.create_task(self.watchdog()), asyncio.create_task(self.daily_loop())]

    async def on_ready(self):
        log.info("已登入：%s", self.user)
        if not core.ALLOWED_GUILD_IDS:
            log.warning("⚠️ 德語寶寶也沒有設定伺服器白名單（DISCORD_GUILD_ID / ALLOWED_GUILD_IDS）。")
        for guild in list(self.guilds):
            await self.leave_if_unauthorized(guild)
        if not core.GUILD_ID:
            for guild in list(self.guilds):
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
        if not DAILY_CHANNEL_ID:
            log.info("沒有設定 GERMAN_DAILY_CHANNEL_ID：不會自動發每日例句（使用者可用 /de_today 取得）。")

    async def on_guild_join(self, guild):
        await self.leave_if_unauthorized(guild)

    async def leave_if_unauthorized(self, guild):
        if core.ALLOWED_GUILD_IDS and str(guild.id) not in core.ALLOWED_GUILD_IDS:
            log.warning("離開未授權的伺服器：%s (%s)", guild.name, guild.id)
            await guild.leave()

    # ---- 訊息：依「頻道 + 留言者」找出屬於誰的練習 ----
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        session = self.sessions.get((message.channel.id, message.author.id))
        if not session:
            return
        session.last_active = time.monotonic()
        usage.current_user.set(message.author.id)
        handler = (handle_speaking if isinstance(session, SpeakSession)
                   else handle_gossip if isinstance(session, GossipSession) else handle_practice)
        key = (message.channel.id, message.author.id)
        async with session.lock:
            if self.sessions.get(key) is not session:  # 等鎖的時候這輪已經被 stop 結束了
                core.log_event("german", "late_message", type(session).__name__, message.author.id, message.channel.id,
                               {"text": message.content[:50], "attachments": len(message.attachments)})
                if message.attachments:
                    await message.channel.send("這輪已經結束了，這則語音沒有被收進去。")
                return
            try:
                await handler(self, message, session)
            except Exception as e:
                log.exception("處理德語練習作答失敗")
                core.log_error("german", "on_message", e, message.author.id, message.channel.id,
                               session=type(session).__name__)
                await message.channel.send("⚠️ 處理答案時出錯了，請看終端機的錯誤訊息。")
            finally:
                session.last_active = time.monotonic()

    # ---- 逾時自動結束 ----
    async def watchdog(self):
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for key, session in list(self.sessions.items()):
                if now - session.last_active < core.SESSION_IDLE_SECONDS or self.sessions.get(key) is not session:
                    continue
                try:
                    async with session.lock:
                        if self.sessions.get(key) is session:
                            core.log_event("german", "timeout", type(session).__name__, key[1], key[0])
                            await self.expire(key, session)
                except Exception as e:
                    log.exception("自動結束逾時的德語練習失敗")
                    core.log_error("german", "expire", e, key[1], key[0])
                    self.sessions.pop(key, None)

    async def expire(self, key, session):
        usage.current_user.set(key[1])
        channel = self.get_channel(key[0]) or await self.fetch_channel(key[0])
        name = ("德語口說" if isinstance(session, SpeakSession)
                else "德語閒聊" if isinstance(session, GossipSession) else "德語練習")
        notice = f"⏰ <@{key[1]}> 超過 {core.SESSION_IDLE_SECONDS / 60:g} 分鐘沒有回應，{name}已自動結束。"
        if isinstance(session, (SpeakSession, GossipSession)):
            finish = finish_speaking if isinstance(session, SpeakSession) else finish_gossip
            report = await finish(self, key, session, reason="timeout")
            await channel.send(notice)
            await deliver_report(report, channel=channel)
        else:
            for chunk in core.chunk_text(notice + "\n\n" + finish_practice(self, key, session, reason="timeout")):
                await channel.send(chunk)

    # ---- 每日例句 ----
    async def daily_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if DAILY_CHANNEL_ID and datetime.datetime.now().time() >= parse_daily_time():
                    existing = get_daily()
                    if existing is None or not existing["posted"]:
                        await self.post_daily()
            except usage.UsageLimitError as e:
                log.info("每日例句暫時無法產生：%s", e)
                await asyncio.sleep(600)
            except Exception:
                log.exception("發送每日例句失敗，10 分鐘後重試")
                await asyncio.sleep(600)
            await asyncio.sleep(30)

    async def post_daily(self):
        async with _daily_lock:
            sentence = get_daily() or await make_sentence(DAILY_LEVEL, "", "daily")
            if sentence["posted"]:
                return
            channel = self.get_channel(int(DAILY_CHANNEL_ID)) or await self.fetch_channel(int(DAILY_CHANNEL_ID))
            await send_sentence(channel.send, sentence, "今日德語例句")
            await channel.send("想練習這句的文法？用 `/de_practice`；想要自己程度的句子用 `/de_sentence`。")
            mark_posted(sentence["id"])


def parse_daily_time():
    try:
        hour, minute = DAILY_TIME.split(":")
        return datetime.time(int(hour), int(minute))
    except (ValueError, TypeError):
        return datetime.time(8, 0)


bot = GermanBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    original = getattr(error, "original", error)
    command = interaction.command.qualified_name if getattr(interaction, "command", None) else "?"
    if isinstance(original, usage.UsageLimitError):
        msg = f"🚦 {original}"
        core.log_event("german", "usage_limit", command, interaction.user.id, interaction.channel_id, {"reason": str(original)})
    elif isinstance(original, ValueError):  # AI 回傳的格式有問題，已經自動重問過還是不行
        log.warning("德語指令的 AI 回傳格式錯誤：%s", original)
        core.log_error("german", command, original, interaction.user.id, interaction.channel_id, kind="ai_format")
        msg = "⚠️ 這次 AI 回傳的內容格式有問題，已經自動重試過還是失敗，請再試一次。"
    else:
        log.exception("德語指令錯誤", exc_info=error)
        core.log_error("german", command, original, interaction.user.id, interaction.channel_id)
        msg = f"⚠️ 發生錯誤：{original}"
    if interaction.response.is_done():
        await interaction.followup.send(msg)
    else:
        await interaction.response.send_message(msg, ephemeral=isinstance(original, usage.UsageLimitError))


LEVEL_CHOICES = [app_commands.Choice(name=f"{lv}｜{gl.LEVEL_HINT[lv]}", value=lv) for lv in gl.LEVELS]


@bot.tree.command(name="de_register", description="第一次使用：設定你的德語程度（CEFR）")
@app_commands.describe(level="你目前的德語程度")
@app_commands.choices(level=LEVEL_CHOICES)
async def de_register(interaction: discord.Interaction, level: app_commands.Choice[str]):
    user = interaction.user.id
    first = get_profile(user) is None
    set_profile(user, level.value)
    if not first:  # 重新選程度 = 重設評級的起點（曲線會留下這個轉折）
        for skill in DE_SKILLS:
            core.save_rating(skill, skills.Rating(gl.CEFR_CENTER[level.value], 0.6), "manual",
                             f"重新登記 {level.value}", load_de_rating(skill, user), str(user))
    await interaction.response.send_message(
        f"{'✅ 註冊完成' if first else '✅ 已更新，評級起點已重設'}！你的德語程度：**{level.value}**\n"
        "之後會依你的練習表現自動調整（`/de_level` 查看、`/de_progress` 看曲線）。\n"
        "`/de_today` 今日例句｜`/de_sentence` 依你的程度生例句｜`/de_ask` 提問｜`/de_practice` 文法練習｜"
        "`/de_speak` 口說（只收語音訊息）｜`/de_gossip` 閒聊（語音聊八卦、即時點評）｜`/de_stats` 統計",
        ephemeral=True,
    )


@bot.tree.command(name="de_today", description="今日德語例句＋文法解析（含德語發音）")
async def de_today(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    async with _daily_lock:
        sentence = get_daily()
        if sentence is None:
            sentence = await make_sentence(DAILY_LEVEL, "", "daily")
    await send_sentence(interaction.followup.send, sentence, "今日德語例句")


TENSE_CHOICES = [app_commands.Choice(name="🎲 隨機時態（預設）", value="random")] + [
    app_commands.Choice(name=name, value=key) for key, (name, _) in gl.TENSES.items()
] + [app_commands.Choice(name="不限時態", value="any")]


@bot.tree.command(name="de_sentence", description="依你的程度現在生一個實用例句＋文法解析（可選時態）")
@app_commands.describe(topic="情境或主題（選填，例如：餐廳點餐、看醫生）", level="想用的程度（預設是你登記的程度）",
                       tense="時態：現在式／過去式／現在完成式／未來式，預設隨機")
@app_commands.choices(level=LEVEL_CHOICES, tense=TENSE_CHOICES)
async def de_sentence(interaction: discord.Interaction, topic: str = "", level: app_commands.Choice[str] = None,
                      tense: app_commands.Choice[str] = None):
    await interaction.response.defer(thinking=True)
    lv = level.value if level else level_for("de_grammar", interaction.user.id)
    mode = tense.value if tense else "random"
    sentence = await make_sentence(lv, topic, "user", interaction.user.id, "" if mode == "any" else mode)
    core.log_event("german", "sentence_tense", mode, interaction.user.id, interaction.channel_id,
                   {"tense": sentence["tense"], "id": sentence["id"]})
    title = "德語例句" + (f"・{gl.TENSES[sentence['tense']][0]}" if sentence["tense"] else "")
    await send_sentence(interaction.followup.send, sentence, title)


FOCUS_CHOICES = [
    app_commands.Choice(name="今日例句優先（預設）", value="today"),
    app_commands.Choice(name="我最弱的文法", value="weak"),
]


@bot.tree.command(name="de_practice", description="針對給過的文法出練習，今日例句優先")
@app_commands.describe(count="題數（3~10，預設 5）", focus="練習重點")
@app_commands.choices(focus=FOCUS_CHOICES)
async def de_practice(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 3, 10] = 5,
    focus: app_commands.Choice[str] = None,
):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.sessions:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    user = interaction.user.id
    recents = recent_sentences(user)
    if not recents:
        await interaction.response.send_message("還沒有給過例句，先用 `/de_today` 或 `/de_sentence` 學一句吧！", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    mode = focus.value if focus else "today"
    todays = [s for s in recents if s["day"] == today()]
    points = gl.pick_focus(grammar_points(todays), grammar_points(recents), grammar_stats(user), 3, mode)
    level = level_for("de_grammar", user)
    text, items = await ask_parsed(
        gl.build_practice_prompt(level, points, todays or recents[:2], count + 2), gl.parse_exercises,
        feature="de_practice", temperature=0.7,
    )
    items = items[:count]
    core.mark_call(text.call_id, count, len(items))

    names = {p["key"]: p["name"] for p in points}
    session = PracticeSession(items, create_practice_row(user, len(items), mode), text.call_id, names,
                              start_rating=load_de_rating("de_grammar", user).rating)
    bot.sessions[key] = session
    await interaction.followup.send(
        f"🧩 **德語文法練習**（{len(items)} 題）\n重點：{'、'.join(names.values())}\n"
        "直接在頻道輸入答案。ä ö ü ß 打不出來可以用 ae oe ue ss；名詞與句首要大寫。`skip` 跳過、`stop` 結束。\n\n"
        + gl.format_exercise(items[0], 0, len(items))
    )


@bot.tree.command(name="de_speak", description="德語口說練習：老師用語音問，你用語音訊息回答（打字不會被當成答案）")
@app_commands.describe(topic="主題（選填）")
async def de_speak(interaction: discord.Interaction, topic: str = ""):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.sessions:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not tts_available():
        await interaction.response.send_message(f"找不到德語語音模型（{VOICE_MODEL}），口說練習暫時無法使用。", ephemeral=True)
        return
    if core.today_call_count() + 2 > core.guard.limits.rpd:
        await interaction.response.send_message("今天剩下的 Gemini 額度不夠做一場口說練習（需要 2 次），明天再來吧。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    level = level_for("de_speaking", interaction.user.id)
    text, data = await ask_parsed(
        gl.build_speaking_prompt(level, topic, SPEAKING_QUESTIONS), gl.parse_speaking_set,
        feature="de_speak_gen", temperature=0.8,
    )
    core.mark_call(text.call_id, SPEAKING_QUESTIONS, len(data["steps"]))

    prefix, slow = f"de_{interaction.id}", level in ("A1", "A2")
    closing = core.AUDIO_DIR / f"{prefix}_end.wav"
    voice = voice_for()  # 這一場的老師：整場同一個聲音
    try:
        for i, step in enumerate(data["steps"]):
            path = core.AUDIO_DIR / f"{prefix}_{i}.wav"
            await asyncio.to_thread(synth, gl.segments_for(step), path, slow, None, voice)
            step["audio"] = str(path)
        await asyncio.to_thread(synth, [gl.CLOSING], closing, slow, None, voice)
    except Exception:
        log.exception("德語配音失敗")
        for f in core.AUDIO_DIR.glob(f"{prefix}_*"):
            f.unlink(missing_ok=True)
        await interaction.followup.send("⚠️ 德語配音失敗，請看終端機錯誤訊息。")
        return

    session = SpeakSession(data["topic"], level, data["steps"], str(closing),
                           create_speaking_row(interaction.user.id, data["topic"], level, len(data["steps"])), text.call_id)
    bot.sessions[key] = session
    intro = (
        f"🎙️ **德語口說練習：{data['topic']}**（{level}，共 {len(data['steps'])} 題"
        + (f"，老師：{voice_label(voice)}" if len(voice_files()) > 1 else "") + "）\n"
        "老師會用語音問你問題，請用 **Discord 語音訊息**回答（輸入框右邊的麥克風），也可以傳音檔。\n"
        "🗒️ **打字不會被當成答案**：你可以先在頻道打講稿，只有語音訊息才算作答。\n"
        f"⏰ 超過 {core.SESSION_IDLE_SECONDS / 60:g} 分鐘沒有回應會自動結束；全部答完才會評分，答滿 {MIN_ANSWERED_TO_SCORE} 題後打 `stop` 可以提前評分。\n\n"
    )
    try:
        await interaction.followup.send(
            intro + speak_step_text(session), file=discord.File(session.current["audio"], filename="frage.wav")
        )
    except Exception:
        bot.sessions.pop(key, None)
        close_speaking_row(session.db_id, "abandoned")
        cleanup_speaking(session)
        raise


@bot.tree.command(name="de_gossip", description="德語閒聊（gossip）：跟 Lena 用語音聊指定回合，每句即時點評文法，結束後評分")
@app_commands.describe(rounds=f"聊幾個回合（{gossip.MIN_ROUNDS}~{gossip.MAX_ROUNDS}，預設 {gossip.DEFAULT_ROUNDS}）",
                       topic="想聊的八卦主題（選填，例如：辦公室、鄰居、旅行糗事）")
async def de_gossip(interaction: discord.Interaction,
                    rounds: app_commands.Range[int, gossip.MIN_ROUNDS, gossip.MAX_ROUNDS] = gossip.DEFAULT_ROUNDS,
                    topic: str = ""):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.sessions:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not tts_available():
        await interaction.response.send_message(f"找不到德語語音模型（{VOICE_MODEL}），閒聊暫時無法使用。", ephemeral=True)
        return
    need = rounds + 2  # 開場 1 次＋每回合 1 次＋評分 1 次
    if core.today_call_count() + need > core.guard.limits.rpd:
        await interaction.response.send_message(f"今天剩下的 Gemini 額度不夠聊 {rounds} 個回合（需要 {need} 次），明天再來吧。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    level = level_for("de_speaking", interaction.user.id)
    text, opening = await ask_parsed(
        gossip.build_opening_prompt(level, topic, rounds), gossip.parse_opening, feature="de_gossip_open", temperature=1.0,
    )
    core.mark_call(text.call_id, 1, 1)
    voice = voice_for()  # 這一場的 Lena：整場同一個聲音
    session = GossipSession(opening["topic"], level, rounds, str(voice), opening,
                            create_gossip_row(interaction.user.id, opening["topic"], level, rounds))
    audio = await gossip_audio(session, opening["german"], "open")
    bot.sessions[key] = session
    core.log_event("german", "gossip_start", opening["topic"], interaction.user.id, interaction.channel_id,
                   {"rounds": rounds, "level": level, "voice": Path(voice).name, "requested_topic": topic})
    intro = (
        f"☕ **德語閒聊 gossip：{opening['topic']}**（{level}，{rounds} 回合"
        + (f"，Lena：{voice_label(voice)}" if len(voice_files()) > 1 else "") + "）\n"
        "Lena 會用語音跟你聊八卦，請用 **Discord 語音訊息**回答（輸入框右邊的麥克風）。\n"
        "每回合我會**即時點評**你的文法（有錯就指出並給更自然的說法），再由 Lena 接話；"
        f"聊完 {rounds} 回合後給你評分、建議，以及全部的逐字稿與英文翻譯（黑色區塊，點開才看得到）。\n"
        f"🗒️ 打字不會被當成回答。想提前結束打 `stop`（至少聊 {gossip.MIN_ANSWERED_TO_SCORE} 回合才評分）；"
        f"{core.SESSION_IDLE_SECONDS / 60:g} 分鐘沒有回應會自動結束。\n\n"
    )
    first = intro + gossip.format_teacher(opening["german"], opening["english"], "🎧 **Lena 說**")
    try:
        files = {"file": discord.File(audio, filename="lena.wav")} if audio else {}
        await interaction.followup.send(first, **files)
    except Exception:
        bot.sessions.pop(key, None)
        close_gossip_row(session.db_id, "abandoned", 0)
        raise
    finally:
        if audio:
            Path(audio).unlink(missing_ok=True)


def practice_context(user, channel_id):
    """學生剛剛在做的練習題：目前顯示、尚未作答的那題（不含答案），以及最近答過的幾題（含他的答案與正確答案）。"""
    current, session_id = None, None
    session = bot.sessions.get((channel_id, user))
    if isinstance(session, PracticeSession):
        session_id = session.db_id
        if session.index < len(session.items):
            ex = session.items[session.index]
            options = "｜選項：" + " / ".join(f"{l}. {o}" for l, o in zip("ABCD", ex["options"])) if ex["options"] else ""
            current = f"{gl.EX_NAME[ex['type']]}｜{ex['prompt']}｜提示：{ex['hint_zh'] or '無'}{options}"
    with db() as c:
        if session_id is None:  # 沒有進行中的練習：看他最近 2 小時內做的那一場
            since = (datetime.datetime.now() - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            row = c.execute(
                "SELECT id FROM de_practice_sessions WHERE user_id = ? AND started_at >= ? ORDER BY id DESC LIMIT 1",
                (str(user), since),
            ).fetchone()
            session_id = row[0] if row else None
        rows = c.execute(
            "SELECT seq, qtype, prompt, expected, user_answer, result, explanation FROM de_practice_answers "
            "WHERE session_id = ? ORDER BY seq DESC LIMIT 3", (session_id,),
        ).fetchall() if session_id else []
    answered = [
        f"第 {seq} 題｜{gl.EX_NAME.get(qtype, qtype)}｜題目：{prompt}｜學生的答案：{mine or '（跳過）'}｜"
        f"正確答案：{expected}｜結果：{result}｜解說：{explanation or '無'}"
        for seq, qtype, prompt, expected, mine, result, explanation in reversed(rows)
    ]
    return {"current": current, "answered": answered}


@bot.tree.command(name="de_ask", description="對例句或德語學習內容有疑問？直接問老師")
@app_commands.describe(question="你的問題（例如：這句為什麼用 hätte？ / dass 和 weil 差在哪？）")
async def de_ask(interaction: discord.Interaction, question: app_commands.Range[str, 2, 500]):
    await interaction.response.defer(thinking=True)
    user = interaction.user.id
    sentences = recent_sentences(user, days=7, limit=2)  # 「這句」指的是最近給你的那句
    text = await core.ask_gemini(
        gl.build_ask_prompt(question, level_for("de_grammar", user), sentences, recent_questions(user),
                            practice_context(user, interaction.channel_id)),
        temperature=0.4, feature="de_ask",
    )
    answer = str(text).strip()
    core.mark_call(text.call_id, 1, 1 if answer else 0)
    if not answer:
        await interaction.followup.send("⚠️ 這次沒有取得回答，請換個方式問或稍後再試。")
        return
    save_question(user, question, answer, sentences[0]["id"] if sentences else None)
    for chunk in core.chunk_text(f"❓ **{question}**\n\n{answer}"):
        await interaction.followup.send(chunk)


@bot.tree.command(name="de_level", description="你的德語程度評級（文法、口說）")
async def de_level(interaction: discord.Interaction):
    user = interaction.user.id
    lines = [f"📊 **德語程度評級**（登記程度 {get_profile(user)}）"]
    for skill, name in DE_SKILLS.items():
        rt = load_de_rating(skill, user)
        history = [r[1] for r in core.skill_history_rows(skill, limit=12, user=str(user))]
        change = core.week_change(skill, user=str(user))
        trend = f"，近 7 天 {change:+.1f}" if change is not None else "，尚無練習紀錄"
        lines.append(
            f"{DE_EMOJI[skill]} {name} **{gl.cefr_score(rt.rating)}** ±{rt.rd:.1f} "
            f"{skills.sparkline(history)}{trend}"
        )
    lines.append("\n±代表不確定度：練習越多越穩定，太久沒練習會回升。`/de_progress` 看曲線圖。")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="de_progress", description="畫出你的德語程度曲線")
async def de_progress(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    buf = await asyncio.to_thread(render_chart, interaction.user.id)
    if buf is None:
        await interaction.followup.send("還沒有足夠的練習紀錄可以畫曲線，先做幾場文法練習或口說吧！", ephemeral=True)
        return
    await interaction.followup.send(file=discord.File(buf, filename="de_progress.png"), ephemeral=True)


@bot.tree.command(name="de_stats", description="你的德語學習統計與各文法點答對率")
async def de_stats(interaction: discord.Interaction):
    user = str(interaction.user.id)
    with db() as c:
        level = get_profile(user)
        sentences = c.execute("SELECT COUNT(*) FROM de_sentences WHERE kind = 'user' AND user_id = ?", (user,)).fetchone()[0]
        practice = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(answered), 0), COALESCE(SUM(correct), 0) FROM de_practice_sessions "
            "WHERE user_id = ?", (user,)).fetchone()
        speaking = c.execute("SELECT COUNT(*) FROM de_speaking_sessions WHERE user_id = ? AND status != 'active'", (user,)).fetchone()[0]
        grammar = c.execute(
            "SELECT name, correct, total FROM de_grammar_stats WHERE user_id = ? ORDER BY "
            "(correct + 1.0) / (total + 2.0) ASC LIMIT 8", (user,)).fetchall()
    lines = [
        f"🇩🇪 **德語統計**（程度 {level}）",
        f"- 自己要的例句：{sentences} 句｜文法練習：{practice[0]} 場，{practice[2]}/{practice[1]} 題答對｜口說：{speaking} 場",
    ]
    if grammar:
        lines.append("\n**文法點答對率（由弱到強）**")
        lines += [f"- {name}：{cor}/{tot}（{cor / tot:.0%}）" for name, cor, tot in grammar]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
