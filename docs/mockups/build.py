"""產生 README 用的示意圖（HTML 原稿 + 評級曲線 PNG）。

內容全是虛構的範例資料，不含任何真實使用者、Discord ID 或對話；但**排版用的是專案真正的函式**
（gossip.format_turn、listening_test.format_sheet、german_logic.format_sentence_*），所以畫面和 bot 實際輸出一致。

    python docs/mockups/build.py          產生 docs/mockups/*.html 與 docs/images/level_curve_*.png

HTML 轉成 PNG（Windows，用內建的 Edge；其他系統用任何瀏覽器截圖即可）：
    msedge --headless=new --hide-scrollbars --force-device-scale-factor=2 ^
           --window-size=870,1300 --screenshot=docs\\images\\de_sentence.png file:///.../docs/mockups/de_sentence.html
"""
import datetime
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
IMAGES = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))

# 畫評級曲線要用資料庫：一律用暫存的，絕不碰正式資料庫
_tmp = tempfile.mkdtemp()
os.environ["DB_FILE"] = os.path.join(_tmp, "demo.db")
os.environ["LOG_DIR"] = os.path.join(_tmp, "logs")

import german_logic as gl  # noqa: E402
import gossip  # noqa: E402
import listening_test as lt  # noqa: E402


# ---------------- 迷你 Markdown → HTML ----------------
def md(text, open_spoilers=()):
    """支援 **粗體**、*斜體*、`程式碼`、||劇透||、## 標題、- 清單；其餘換行轉 <br>。"""
    out, in_list, spoiler_i = [], False, 0

    def spoil(m):
        nonlocal spoiler_i
        spoiler_i += 1
        cls = "spoiler open" if spoiler_i in open_spoilers else "spoiler"
        return f'<span class="{cls}">{m.group(1)}</span>'

    for line in text.split("\n"):
        s = html.escape(line, quote=False)
        s = re.sub(r"\|\|(.+?)\|\|", spoil, s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
        if s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{s[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<h2>{s[3:]}</h2>" if s.startswith("## ") else (s + "<br>" if s else "<br>"))
    if in_list:
        out.append("</ul>")
    return "".join(out)


def audio(name, length):
    return (f'<div class="audio"><div class="fn">{name}</div><div class="bar"><div class="play">▶</div>'
            f'<span>0:00 / {length}</span><div class="track"></div><span>🔊</span></div></div>')


def voice_msg(length, seed=7):
    import random
    rnd = random.Random(seed)
    bars = "".join(f'<i style="height:{rnd.randint(5, 22)}px"></i>' for _ in range(34))
    return f'<div class="voice"><div class="play">▶</div><div class="wave">{bars}</div><span>{length}</span></div>'


def message(name, color, body, *, bot=True, time="今天 20:15", extra=""):
    tag = '<span class="tag">BOT</span>' if bot else ""
    return (f'<div class="msg"><div class="avatar" style="background:{color}">{name[0]}</div><div class="body">'
            f'<div class="head"><span class="name">{name}</span>{tag}<span class="time">{time}</span></div>'
            f'<div class="text">{body}</div>{extra}</div></div>')


def command(user, cmd, options=""):
    opt = f' <span class="opt">{options}</span>' if options else ""
    return f'<div class="cmd"><div class="arrow"></div><b>{user}</b> 使用了 <b>/{cmd}</b>{opt}</div>'


def page(title, inner, note="示意畫面：範例資料，非實際使用者紀錄"):
    css = (OUT / "discord.css").read_text(encoding="utf-8")
    (OUT / f"{title}.html").write_text(
        f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>{title}</title>'
        f"<style>{css}</style></head><body>{inner}<div class=\"note\">{note}</div></body></html>",
        encoding="utf-8")


BOT_DE, BOT_EN, USER = ("Deutsch_baby", "#3ba55d"), ("ILETS baby", "#e67e22"), ("Learner", "#5865f2")


# ---------------- 1. /de_sentence ----------------
def de_sentence():
    s = {
        "level": "A2", "german": "Ich habe gestern in der Stadt einen schönen Pullover gekauft.",
        "chinese": "我昨天在城裡買了一件漂亮的毛衣。", "situation": "在朋友聚會或購物聊天時，向別人分享自己昨天買了什麼東西。",
        "words": [{"de": "der Pullover", "zh": "毛衣", "note": "陽性名詞，複數同形"},
                  {"de": "kaufen", "zh": "買", "note": "規則動詞，Perfekt 用 haben"},
                  {"de": "gestern", "zh": "昨天", "note": ""}, {"de": "die Stadt", "zh": "城市", "note": "陰性名詞"}],
        "grammar": [
            {"name": "現在完成式（Perfekt mit haben）", "structure": "haben（第二位）+ … + Partizip II（句尾）",
             "explanation": "德語談論過去的事，口語最常用 Perfekt：助動詞 haben 變位放第二位，過去分詞放句尾。",
             "examples": ["Ich habe ein Buch gelesen.（我讀了一本書。）"]},
            {"name": "第四格受格（Akkusativ）", "structure": "動詞 + einen（陽性）/ eine / ein + 名詞",
             "explanation": "Pullover 是陽性名詞，當直接受詞時不定冠詞 ein 變成 einen，形容詞詞尾也跟著變成 -en。",
             "examples": ["Ich suche einen Job.（我在找工作。）"]}],
        "tip": "講到昨天或上週做的事，德國人口語幾乎都用 Perfekt 而不是 Präteritum。",
    }
    head = gl.format_sentence_head(s, "德語例句・現在完成式") + "\n🎙️ 朗讀：Ramona（女聲）"
    btns = '<div class="btns">' + "".join(f'<div class="btn">🔊 {w}</div>' for w in ("der Pullover", "kaufen", "gestern", "die Stadt")) + "</div>"
    inner = (command("Learner", "de_sentence", "tense: 現在完成式")
             + message(*BOT_DE, md(head), extra=audio("satz.wav", "0:04") + audio("satz_0.5x.wav", "0:08") + btns)
             + message(*BOT_DE, md(gl.format_sentence_body(s)), time="今天 20:15"))
    page("de_sentence", inner)


# ---------------- 2. /de_gossip ----------------
def de_gossip():
    opening = ("Hallo! Stell dir vor, was heute im Büro passiert ist: Tom hat Kaffeepulver auf das weiße Hemd vom Chef "
               "verschüttet! Hast du auch schon mal so ein peinliches Erlebnis bei der Arbeit gehabt?")
    opening_en = ("Hello! Imagine what happened in the office today: Tom spilled coffee powder on the boss's white shirt! "
                  "Have you ever had such an embarrassing experience at work?")
    turn = gossip.parse_turn(json.dumps({
        "transcript": "Ja, gestern ich habe Kaffee auf meinen Computer verschüttet.",
        "transcript_en": "Yes, yesterday I spilled coffee on my computer.", "status": "errors", "verdict": "內容很生動，只差一個語序！",
        "errors": [{"said": "gestern ich habe", "fix": "gestern habe ich",
                    "why": "動詞要放第二位（V2）：句首放了時間詞 gestern，主詞 ich 就要移到動詞後面"}],
        "better": "Ja, gestern habe ich Kaffee auf meinen Computer verschüttet.", "pronunciation_note": "",
        "reply": "Oh nein, der arme Computer! Funktioniert er noch? Tom hat letzte Woche sein Handy in den Kaffee fallen lassen.",
        "reply_en": "Oh no, the poor computer! Does it still work? Last week Tom dropped his phone into his coffee."}))
    feedback = gossip.format_turn(turn, 1, 6)
    teacher = gossip.format_teacher(turn["reply"], turn["reply_en"])
    inner = (command("Learner", "de_gossip", "rounds: 6")
             + message(*BOT_DE, md("☕ **德語閒聊 gossip：Büro-Geflüster**（A2，6 回合，Lena：Ramona（女聲））\n"
                                  "Lena 會用語音跟你聊八卦，請用 **Discord 語音訊息**回答。每回合我會**即時點評**你的文法，"
                                  "再由 Lena 接話；聊完後給你評分、建議，以及全部的逐字稿與英文翻譯。\n\n"
                                  + gossip.format_teacher(opening, opening_en)), extra=audio("lena.wav", "0:12"))
             + message(*USER, "", bot=False, time="今天 20:16", extra=voice_msg("0:07"))
             + message(*BOT_DE, md(feedback + "\n\n" + teacher, open_spoilers=(1, 2, 3)), time="今天 20:16", extra=audio("lena.wav", "0:07")))
    page("de_gossip", inner, "示意畫面：範例資料。黑色區塊是遮擋文字（Discord 裡點一下才會展開）；上面開場白保持遮擋，下面這一回合已展開")


# ---------------- 3. /listen（雅思模擬）----------------
def ielts_listen():
    data = {
        "context": "A customer phones a gym to ask about joining.",
        "speakers": {"A": "Emma, a receptionist", "B": "Mr Brown, a customer"},
        "questions": [
            {"type": "blank", "question": "Customer's surname: ____"},
            {"type": "blank", "question": "Phone number: ____"},
            {"type": "blank", "question": "Wants to start on 12 ____"},
            {"type": "blank", "question": "Standard membership: £ ____ per month"},
            {"type": "mcq", "question": "Why does Mr Brown want to join?",
             "options": ["to lose weight", "to meet new people", "to prepare for a race"]},
            {"type": "mcq", "question": "What is included for free in the first month?",
             "options": ["a fitness test", "a towel service", "two guest passes"]},
        ],
    }
    sheet = lt.format_sheet(1, data)
    help_text = lt.ANSWER_HELP + "\n⏳ 30 秒後開始播放錄音，**只會播一次**，先看一下題目。"
    inner = (command("Learner", "listen", "mode: 雅思模擬　section: Section 1")
             + message(*BOT_EN, md(sheet))
             + message(*BOT_EN, md(help_text), time="今天 20:21")
             + message(*BOT_EN, md("🎧 **Section 1**"), time="今天 20:21", extra=audio("section1.ogg", "2:41"))
             + message(*USER, md("1 Brown\n2 07412 558 913\n3 March\n4 32\n5B 6A"), bot=False, time="今天 20:24",
                       extra='<div class="reaction">✅ <small>1</small></div>'))
    page("ielts_listen", inner, "示意畫面：範例資料（題目是示範用的，不是實際考題）")


# ---------------- 4. 評級曲線（用真正的繪圖函式 + 虛構的紀錄）----------------
def charts():
    import random

    import bot as core
    import german
    import skills
    import usage

    core.init_db()
    rnd = random.Random(3)
    start = datetime.datetime.now() - datetime.timedelta(days=28)

    def seed(user, skill, base, gain, rd0, n=14):
        with core.db_connect() as conn:
            for i in range(n):
                ts = (start + datetime.timedelta(days=i * 2 + rnd.random())).strftime("%Y-%m-%d %H:%M:%S")
                rating = base + gain * i / (n - 1) + rnd.uniform(-0.12, 0.12)
                rd = max(0.25, rd0 * (1 - i / (n * 1.6)))
                conn.execute("INSERT INTO skill_history (user_id, skill, ts, rating, rd, source, detail) VALUES (?, ?, ?, ?, ?, 'demo', '')",
                             (user, skill, ts, rating, rd))

    seed("1", "de_grammar", 2.6, 1.0, 0.8)
    seed("1", "de_speaking", 2.3, 0.9, 0.9)
    buf = german.render_chart(1)
    (IMAGES / "level_curve_de.png").write_bytes(buf.getvalue())

    usage.current_user.set("2")
    for skill, base, gain in zip(skills.SKILLS, (5.2, 5.0, 4.8, 5.5), (0.9, 0.8, 1.1, 0.6)):
        seed("2", skill, base, gain, 0.9)
    (IMAGES / "level_curve_ielts.png").write_bytes(core.render_progress_chart().getvalue())


if __name__ == "__main__":
    IMAGES.mkdir(parents=True, exist_ok=True)
    de_sentence()
    de_gossip()
    ielts_listen()
    charts()
    print("完成：", ", ".join(sorted(p.name for p in OUT.glob("*.html"))), "｜", ", ".join(sorted(p.name for p in IMAGES.glob("level_curve_*.png"))))
