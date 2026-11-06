import asyncio
import datetime
import difflib
import io
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

import accounts
import asr
import dialogue
import jsonutil
import exercises
import listening_test as lt
import german_logic
import reports
import skills
import sources
import speaking
import tts
import usage

# ----------------- 1. 基本設定 -----------------
load_dotenv()
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash").strip()  # 主模型 503 時改用；留空＝不備援
DB_FILE = os.getenv("DB_FILE", "ielts_learning.db")
# 你目前的雅思程度（band），測驗時用來生成適合的新單字
IELTS_LEVEL = os.getenv("IELTS_LEVEL", "5.5")
# 管理員的 Discord 使用者 ID（可核准/封鎖、看全域用量）；沒設時，從既有紀錄推斷擁有者
OWNER_ID = os.getenv("OWNER_ID") or None
# 閱讀練習「真實文章改編」可用的來源（逗號分隔）；維基系需要先設定 READING_CONTACT 才會啟用
READING_SOURCES = [s.strip() for s in os.getenv("READING_SOURCES", "owid,nasa,nist,globalvoices,wikipedia,wikinews,wikivoyage").split(",") if s.strip()]
# 持續性互動（測驗、閱讀、聽力、口說模擬）超過這麼久沒有回應，就自動結束並在頻道提示
SESSION_IDLE_SECONDS = float(os.getenv("SESSION_IDLE_MINUTES", "10")) * 60
# 設定後，slash command 會即時同步到該伺服器（全域同步最久要 1 小時）
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
# Bot 只允許待在這些伺服器（逗號分隔的伺服器 ID）；被加進其他伺服器會自動退出。預設只允許 DISCORD_GUILD_ID
ALLOWED_GUILD_IDS = {g.strip() for g in os.getenv("ALLOWED_GUILD_IDS", GUILD_ID or "").split(",") if g.strip()}

AUDIO_DIR = Path("audio_cache")
AUDIO_DIR.mkdir(exist_ok=True)
RECORDS_ROOT = Path("records")  # 每週一個檔案：records/weekly_2026-W45.md

# 所有紀錄：logs/bot.log（檔案，自動輪替）＋資料庫裡的事件紀錄與 Gemini 提示詞/回應原文，方便事後追查問題
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
PAYLOAD_RETENTION_DAYS = int(os.getenv("PAYLOAD_RETENTION_DAYS", "30"))  # Gemini 提示詞與回應原文保存幾天；0 = 不保存
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "365"))  # 指令使用與錯誤等事件紀錄保存幾天

DELIMITER = "===DATA_RECORD==="
DISCORD_LIMIT = 1900  # Discord 單則上限 2000，留一點餘裕

AUDIO_MIME = {
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}
MAX_AUDIO_BYTES = 15 * 1024 * 1024

def setup_logging():
    """終端機與 logs/bot.log 同時輸出（兩隻 bot 共用）。檔案每 5MB 輪替一次，保留 10 份。"""
    root = logging.getLogger()
    if getattr(root, "_ilets_configured", False):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    for handler in (
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=10,
                                             encoding="utf-8"),
    ):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root._ilets_configured = True


setup_logging()
log = logging.getLogger("ielts_bot")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 用量防護的上限；預設值對應 Flash Lite 免費額度（15 RPM / 500 RPD）並留餘裕。
# 換成額度較小的模型（例如每日 20 次）時，請在 .env 調低。
guard = usage.Guard(
    usage.Limits(
        rpm=int(os.getenv("GEMINI_RPM_LIMIT", "12")),
        rpd=int(os.getenv("GEMINI_RPD_LIMIT", "450")),
        user_rpm=int(os.getenv("USER_RPM_LIMIT", "8")),
    )
)


# ----------------- 2. 資料庫 -----------------
def db_connect():
    return sqlite3.connect(DB_FILE)


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS training_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                category TEXT,
                target_word TEXT,
                user_input TEXT,
                polished_sentence TEXT,
                synonyms TEXT,
                feedback TEXT,
                mastery INTEGER,
                review_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                channel_id TEXT,
                started_at TEXT,
                ended_at TEXT,
                total INTEGER,
                answered INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                skill_changes TEXT,
                status TEXT DEFAULT 'active'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                seq INTEGER,
                word TEXT,
                question TEXT,
                user_answer TEXT,
                result TEXT,
                explanation TEXT,
                synonyms TEXT,
                examples TEXT,
                difficulty REAL,
                answered_at TEXT
            )
            """
        )
        # 閱讀 / 聽力練習
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,
                title TEXT,
                topic TEXT,
                passage TEXT,
                started_at TEXT,
                ended_at TEXT,
                total INTEGER,
                answered INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                skill_change TEXT,
                status TEXT DEFAULT 'active'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                seq INTEGER,
                qtype TEXT,
                question TEXT,
                options TEXT,
                correct_answer TEXT,
                user_answer TEXT,
                result TEXT,
                explanation TEXT,
                difficulty REAL,
                answered_at TEXT
            )
            """
        )
        # 口說模擬測驗
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speaking_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                channel_id TEXT,
                topic TEXT,
                started_at TEXT,
                ended_at TEXT,
                total INTEGER,
                answered INTEGER DEFAULT 0,
                band REAL,
                criteria TEXT,
                report TEXT,
                skill_change TEXT,
                status TEXT DEFAULT 'active'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speaking_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                seq INTEGER,
                part INTEGER,
                question TEXT,
                seconds REAL,
                skipped INTEGER,
                transcript TEXT,
                answered_at TEXT
            )
            """
        )
        # 技能評級的歷史（畫曲線用）；目前值在 skill_ratings，與使用者資料表一起由 accounts.migrate 建立
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill TEXT,
                ts TEXT,
                rating REAL,
                rd REAL,
                source TEXT,
                detail TEXT
            )
            """
        )
        # API 用量與效益：每次呼叫一筆；被防護攔下或斷路器事件記在 api_events
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                feature TEXT,
                model TEXT,
                user_id TEXT,
                prompt_chars INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                thinking_tokens INTEGER,
                latency_ms INTEGER,
                ok INTEGER,
                error_kind TEXT,
                units_requested INTEGER,
                units_useful INTEGER,
                units_consumed INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                kind TEXT,
                reason TEXT,
                user_id TEXT,
                feature TEXT,
                detail TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts)")
        # 使用痕跡：每個指令、錯誤（含完整 traceback）、被忽略的訊息…都記一筆，事後可以用 logview.py 或 SQL 查
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                bot TEXT,
                kind TEXT,
                name TEXT,
                user_id TEXT,
                channel_id TEXT,
                detail TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_events_ts ON app_events (ts)")
        # 每次 Gemini 呼叫的提示詞與回應原文（對應 api_calls.id）；音檔只記大小，不存內容
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_payloads (
                call_id INTEGER PRIMARY KEY,
                ts TEXT,
                prompt TEXT,
                response TEXT,
                error TEXT
            )
            """
        )
        # 舊版資料庫補欄位
        for table, column, decl in (
            ("quiz_sessions", "skill_changes", "TEXT"),
            ("quiz_answers", "difficulty", "REAL"),
            ("quiz_answers", "expected", "TEXT"),  # 這題要填的確切字串（可能是單字的變化形）
            ("training_logs", "source", "TEXT"),  # word / submit / speak / quiz，週報用來區分
            ("exercise_answers", "evidence", "TEXT"),  # 閱讀題的原文依據
            ("api_calls", "error_text", "TEXT"),  # 呼叫失敗時 Gemini 回的錯誤訊息
            ("exercise_sessions", "source_name", "TEXT"),  # 閱讀的真實文章來源（原創短文則為空）
            ("exercise_sessions", "source_url", "TEXT"),
            ("exercise_sessions", "source_credit", "TEXT"),
        ):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        # 單人 → 多人：既有紀錄與評級歸給擁有者（OWNER_ID，沒設就從既有紀錄推斷），並設為管理員
        legacy = conn.execute("SELECT value FROM settings WHERE key = 'level'").fetchone()
        accounts.migrate(conn, OWNER_ID, float(legacy[0]) if legacy else float(IELTS_LEVEL))
        german_logic.create_tables(conn)  # 德語寶寶共用同一個資料庫


# ----------------- 2.5 技能評級（聽說讀寫）-----------------
SKILL_EMOJI = {"listening": "🎧", "speaking": "🗣️", "reading": "📖", "writing": "✍️"}
MIN_ANSWERED_TO_RATE = 3  # 一場練習至少答幾題才計入評級


def uid():
    """目前這個操作的 Discord 使用者 ID（字串）。

    指令由 IeltsTree.interaction_check 設定、訊息由 on_message 設定、背景工作由各自的入口設定；
    沒設定就直接報錯，避免資料被悄悄寫到錯的人身上。
    """
    user = usage.current_user.get()
    if user is None:
        raise RuntimeError("目前沒有使用者身分：呼叫前請先設定 usage.current_user")
    return str(user)


def load_rating(skill, user=None):
    """讀取這位使用者目前的評級；沒練習過就用他註冊時填的基礎分數。太久沒練習，不確定度會自動回升。"""
    user = user or uid()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT rating, rd, updated_at FROM skill_ratings WHERE user_id = ? AND skill = ?", (user, skill)
        ).fetchone()
    if not row:
        return skills.Rating(accounts.base_level(db_connect, user, skill, IELTS_LEVEL))
    rating, rd, updated = row
    idle = (datetime.datetime.now() - datetime.datetime.strptime(updated, "%Y-%m-%d %H:%M:%S"))
    return skills.Rating(rating, skills.inflate(rd, idle.total_seconds() / 86400))


def save_rating(skill, after, source, detail="", before=None, user=None):
    user = user or uid()
    ts = now_str()
    with db_connect() as conn:
        if before is not None and not conn.execute(
            "SELECT 1 FROM skill_history WHERE user_id = ? AND skill = ? LIMIT 1", (user, skill)
        ).fetchone():
            # 第一次更新前，先記下起點，曲線才有起始點
            conn.execute(
                "INSERT INTO skill_history (user_id, skill, ts, rating, rd, source, detail) "
                "VALUES (?, ?, ?, ?, ?, 'initial', '')",
                (user, skill, ts, before.rating, before.rd),
            )
        conn.execute(
            "INSERT INTO skill_ratings (user_id, skill, rating, rd, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, skill) DO UPDATE SET rating = excluded.rating, rd = excluded.rd, "
            "updated_at = excluded.updated_at",
            (user, skill, after.rating, after.rd, ts),
        )
        conn.execute(
            "INSERT INTO skill_history (user_id, skill, ts, rating, rd, source, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, skill, ts, after.rating, after.rd, source, detail),
        )


def update_skill_items(skill, items, weight, source, detail="", user=None):
    before = load_rating(skill, user)
    after = skills.update_from_items(before, items, weight)
    save_rating(skill, after, source, detail, before, user)
    return before, after


def update_skill_band(skill, band, sigma, source, detail="", user=None):
    before = load_rating(skill, user)
    after = skills.update_from_band(before, band, sigma)
    save_rating(skill, after, source, detail, before, user)
    return before, after


def vocab_level(user=None):
    """單字的出題程度 = 這位使用者閱讀與寫作的平均（單字測驗讀寫各算一半）。"""
    return (load_rating("reading", user).rating + load_rating("writing", user).rating) / 2


def fmt_change(skill, before, after):
    delta = after.rating - before.rating
    arrow = "📈" if delta > 0.04 else "📉" if delta < -0.04 else "➖"
    return f"{arrow} {skills.SKILL_NAMES[skill]} {before.rating:.1f} → {after.rating:.1f}"


def skill_history_rows(skill, limit=None, user=None):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT ts, rating, rd FROM skill_history WHERE user_id = ? AND skill = ? ORDER BY id",
            (user or uid(), skill),
        ).fetchall()
    return rows[-limit:] if limit else rows


def week_change(skill, user=None):
    """近 7 天的變化：與 7 天前（或最早一筆）的評級比。沒有歷史則回傳 None。"""
    rows = skill_history_rows(skill, user=user)
    if len(rows) < 2:
        return None
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    older = [r for r in rows if r[0] <= cutoff]
    base = older[-1][1] if older else rows[0][1]
    return rows[-1][1] - base


def save_to_db(category, target_word, user_input, polished, synonyms, feedback, mastery, source="", user=None):
    """source：這筆紀錄怎麼來的（word / submit / quiz）。每筆都綁定使用者。"""
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO training_logs
            (timestamp, category, target_word, user_input, polished_sentence, synonyms, feedback, mastery, source,
             user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                category,
                target_word,
                user_input,
                polished,
                synonyms,
                feedback,
                mastery,
                source,
                user or uid(),
            ),
        )


def pick_review_words(limit=3):
    """掌握度最低的優先，同分隨機；被抽到的單字 review_count +1。只從這位使用者的單字庫抽。"""
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, target_word, feedback FROM training_logs
            WHERE category = 'Vocabulary' AND user_id = ?
            ORDER BY mastery ASC, review_count ASC, RANDOM() LIMIT ?
            """,
            (uid(), limit),
        ).fetchall()
        conn.executemany(
            "UPDATE training_logs SET review_count = review_count + 1 WHERE id = ?",
            [(r[0],) for r in rows],
        )
    return rows


def pick_quiz_words(limit):
    """每個單字只出一次；掌握度低、較少複習的優先。回傳 [(word, feedback, example)]。"""
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT target_word, MAX(feedback), MAX(polished_sentence) FROM training_logs
            WHERE category = 'Vocabulary' AND user_id = ?
            GROUP BY LOWER(target_word)
            ORDER BY MIN(mastery) ASC, SUM(review_count) ASC, RANDOM() LIMIT ?
            """,
            (uid(), limit),
        ).fetchall()


def known_words():
    """這位使用者單字庫裡所有單字（新單字生成時要避開）。"""
    with db_connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT target_word FROM training_logs WHERE category = 'Vocabulary' AND user_id = ?",
            (uid(),),
        )]


def record_quiz_result(word, correct):
    """答對掌握度 +1，答錯 -1（限制在 1~5），同時累計複習次數。"""
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE training_logs
            SET mastery = MAX(1, MIN(5, COALESCE(mastery, 3) + ?)),
                review_count = review_count + 1
            WHERE category = 'Vocabulary' AND user_id = ? AND LOWER(target_word) = LOWER(?)
            """,
            (1 if correct else -1, uid(), word),
        )


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enrich_word(word, synonyms, examples):
    """把測驗產生的同義字與例句補進單字庫（只補空的欄位，不覆蓋原有內容）。"""
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE training_logs SET
                synonyms = CASE WHEN COALESCE(synonyms, '') = '' THEN ? ELSE synonyms END,
                polished_sentence = CASE WHEN COALESCE(polished_sentence, '') = '' THEN ?
                                         ELSE polished_sentence END
            WHERE category = 'Vocabulary' AND user_id = ? AND LOWER(target_word) = LOWER(?)
            """,
            (", ".join(synonyms), examples[0] if examples else "", uid(), word),
        )


def create_quiz_session_row(user_id, channel_id, total):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO quiz_sessions (user_id, channel_id, started_at, total) VALUES (?, ?, ?, ?)",
            (str(user_id), str(channel_id), now_str(), total),
        )
        return cur.lastrowid


def log_quiz_answer(session_id, seq, item, user_answer, result):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO quiz_answers
            (session_id, seq, word, question, user_answer, result, explanation, synonyms, examples,
             difficulty, answered_at, expected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, seq, item["word"],
                item["question"]
                + (f"（描述：{item['desc']}）" if item.get("desc") else "")
                + (f"（說明：{item['note']}）" if item.get("note") else ""),
                user_answer, result, item["explanation"], ", ".join(item["synonyms"]),
                json.dumps(item["examples"], ensure_ascii=False), item.get("band"), now_str(),
                item["answer"],
            ),
        )


def close_quiz_session_row(session_id, status, skill_changes=None):
    with db_connect() as conn:
        answered, correct = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(result = 'correct'), 0) "
            "FROM quiz_answers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.execute(
            "UPDATE quiz_sessions SET ended_at = ?, answered = ?, correct = ?, status = ?, "
            "skill_changes = ? WHERE id = ?",
            (now_str(), answered, correct, status, json.dumps(skill_changes or {}), session_id),
        )


# ---- 閱讀 / 聽力練習的存檔 ----
def create_exercise_row(kind, title, topic, passage, total, source=None):
    """source：sources.Article（真實文章改編時）；原創內容則為 None。"""
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO exercise_sessions (kind, title, topic, passage, started_at, total, user_id, "
            "source_name, source_url, source_credit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, title, topic, passage, now_str(), total, uid(),
             source.source if source else None, source.url if source else None, source.credit if source else None),
        )
        return cur.lastrowid


def used_reading_urls():
    """這位使用者已經做過的真實文章，避免重複出同一篇。"""
    with db_connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT source_url FROM exercise_sessions WHERE user_id = ? AND source_url IS NOT NULL "
            "ORDER BY id DESC LIMIT 300",
            (uid(),),
        )}


def log_exercise_answer(session_id, seq, item, user_answer, result):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO exercise_answers
            (session_id, seq, qtype, question, options, correct_answer, user_answer, result,
             explanation, difficulty, answered_at, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, seq, item["type"], item["question"],
                json.dumps(item["options"], ensure_ascii=False), exercises.answer_text(item),
                user_answer, result, item["explanation"], item["band"], now_str(), item.get("evidence", ""),
            ),
        )


def create_speaking_row(user_id, channel_id, topic, total):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO speaking_sessions (user_id, channel_id, topic, started_at, total) VALUES (?, ?, ?, ?, ?)",
            (str(user_id), str(channel_id), topic, now_str(), total),
        )
        return cur.lastrowid


def log_speaking_answer(session_id, seq, step, seconds, skipped):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO speaking_answers (session_id, seq, part, question, seconds, skipped, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, seq, step["part"], step["question"], seconds, int(skipped), now_str()),
        )


def close_speaking_row(session_id, status, band=None, criteria=None, report=None, transcripts=None,
                       skill_change=None):
    with db_connect() as conn:
        answered = conn.execute(
            "SELECT COUNT(*) FROM speaking_answers WHERE session_id = ? AND skipped = 0", (session_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE speaking_sessions SET ended_at = ?, answered = ?, band = ?, criteria = ?, report = ?, "
            "skill_change = ?, status = ? WHERE id = ?",
            (now_str(), answered, band, json.dumps(criteria or {}), report,
             json.dumps(skill_change or {}), status, session_id),
        )
        for seq, text in enumerate(transcripts or [], start=1):
            conn.execute(
                "UPDATE speaking_answers SET transcript = ? WHERE session_id = ? AND seq = ?",
                (str(text or ""), session_id, seq),
            )


def close_exercise_row(session_id, status, skill_change=None):
    with db_connect() as conn:
        answered, correct = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(result = 'correct'), 0) "
            "FROM exercise_answers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.execute(
            "UPDATE exercise_sessions SET ended_at = ?, answered = ?, correct = ?, status = ?, "
            "skill_change = ? WHERE id = ?",
            (now_str(), answered, correct, status, json.dumps(skill_change or {}), session_id),
        )


# ----------------- 3. Gemini -----------------
SYSTEM_INSTRUCTION = f"""
你是一位嚴謹的雅思（IELTS）單字教練，請用繁體中文講解，英文例句保持英文。
你的回覆分為兩部分，中間以「{DELIMITER}」分隔：

[前端回覆]
以 Markdown 格式詳細輸出：提供英英定義、雅思同義詞替換（依情境分類）、高頻搭配詞、學術例句。

{DELIMITER}
輸出純 JSON（不要 Markdown 標記、不要 ``` 圍欄）：
{{
  "category": "Vocabulary",
  "target_word": "核心單字",
  "polished_sentence": "一個最能代表用法的學術例句",
  "synonyms": "同義字清單（逗號分隔）",
  "feedback": "這個字的學習要點摘要",
  "mastery": 3
}}
"""


# ---- API 用量記錄 ----
def day_start():
    return datetime.date.today().strftime("%Y-%m-%d 00:00:00")


def today_call_count(user_id=None):
    """今天的 Gemini 呼叫次數；全域額度防護用全域數字，user_id 有給就只算這個人。"""
    sql, args = "SELECT COUNT(*) FROM api_calls WHERE ts >= ?", [day_start()]
    if user_id:
        sql += " AND user_id = ?"
        args.append(str(user_id))
    with db_connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


def log_api_call(feature, user_id, prompt_chars, tokens, latency_ms, ok, error_kind, error_text=None, model=None):
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO api_calls (ts, feature, model, user_id, prompt_chars, input_tokens, output_tokens,
                                   thinking_tokens, latency_ms, ok, error_kind, error_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_str(), feature, model or GEMINI_MODEL, str(user_id) if user_id else None, prompt_chars,
             *tokens, latency_ms, int(ok), error_kind, error_text),
        )
        return cur.lastrowid


def log_event(bot_name, kind, name="", user_id=None, channel_id=None, detail=None):
    """記錄一筆使用痕跡（指令、錯誤、被忽略的訊息…）。同時寫進 log 檔與資料庫；記錄本身出錯不能影響主流程。"""
    text = json.dumps(detail, ensure_ascii=False, default=str)[:4000] if detail is not None else ""
    log.info("[事件] %s/%s %s user=%s ch=%s %s", bot_name, kind, name, user_id, channel_id, text[:300])
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO app_events (ts, bot, kind, name, user_id, channel_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_str(), bot_name, kind, name, str(user_id) if user_id else None,
                 str(channel_id) if channel_id else None, text),
            )
    except Exception:
        log.exception("寫入事件紀錄失敗")


def log_command(bot_name, interaction, allowed=True, reason=""):
    """記錄誰用了哪個指令、帶了什麼參數。"""
    try:
        command = interaction.command.qualified_name if getattr(interaction, "command", None) else "?"
        options = {k: str(v)[:200] for k, v in (getattr(interaction, "namespace", None) or {})}
    except Exception:
        command, options = "?", {}
    log_event(bot_name, "command" if allowed else "command_denied", command, interaction.user.id,
              getattr(interaction, "channel_id", None), {"options": options, "reason": reason} if reason or options else None)


def log_error(bot_name, name, error, user_id=None, channel_id=None, **extra):
    """記錄錯誤，附完整 traceback。"""
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log_event(bot_name, "error", name, user_id, channel_id, {"error": f"{type(error).__name__}: {error}", "traceback": tb, **extra})


def describe_contents(contents):
    """把送給 Gemini 的內容轉成可以存的文字；音檔與 PDF 只記大小。"""
    if isinstance(contents, str):
        return contents
    parts = []
    for c in contents:
        if isinstance(c, str):
            parts.append(c)
        else:
            inline = getattr(c, "inline_data", None)
            parts.append(f"[附件 {getattr(inline, 'mime_type', '?')} {len(getattr(inline, 'data', b'') or b'')} bytes]")
    return "\n".join(parts)


def save_payload(call_id, system_instruction, contents, response, error=None):
    """保存這次呼叫的提示詞與回應原文，事後才查得到 Gemini 當時到底回了什麼。"""
    if PAYLOAD_RETENTION_DAYS <= 0 or not call_id:
        return
    try:
        prompt = (f"[system]\n{system_instruction}\n\n" if system_instruction else "") + "[prompt]\n" + describe_contents(contents)
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_payloads (call_id, ts, prompt, response, error) VALUES (?, ?, ?, ?, ?)",
                (call_id, now_str(), prompt[:60000], (response or "")[:60000], error),
            )
    except Exception:
        log.exception("保存 Gemini 提示詞/回應失敗")


def prune_logs():
    """清掉過期的事件與 Gemini 提示詞/回應原文（保存天數見 .env）。"""
    now = datetime.datetime.now()
    with db_connect() as conn:
        conn.execute("DELETE FROM api_payloads WHERE ts < ?",
                     ((now - datetime.timedelta(days=max(PAYLOAD_RETENTION_DAYS, 1))).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("DELETE FROM app_events WHERE ts < ?",
                     ((now - datetime.timedelta(days=max(EVENT_RETENTION_DAYS, 1))).strftime("%Y-%m-%d %H:%M:%S"),))


def log_api_event(kind, reason, user_id=None, feature="", detail=""):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO api_events (ts, kind, reason, user_id, feature, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now_str(), kind, reason, str(user_id) if user_id else None, feature, detail),
        )


def mark_call(call_id, requested, useful):
    """回報這次呼叫『要了多少、可用多少』（例如要 10 題、解析後有 9 題可用）。"""
    if call_id:
        with db_connect() as conn:
            conn.execute(
                "UPDATE api_calls SET units_requested = ?, units_useful = ? WHERE id = ?",
                (requested, useful, call_id),
            )


def mark_consumed(call_id, consumed):
    """回報這次呼叫產生的內容最後『實際被用到多少』（例如 10 題只作答了 3 題）。"""
    if call_id:
        with db_connect() as conn:
            conn.execute("UPDATE api_calls SET units_consumed = ? WHERE id = ?", (consumed, call_id))


_last_anomaly_check = 0.0


def check_daily_anomaly():
    """最多每小時檢查一次：今天用量是否遠高於過去平均。"""
    global _last_anomaly_check
    if time.monotonic() - _last_anomaly_check < 3600:
        return
    _last_anomaly_check = time.monotonic()
    with db_connect() as conn:
        today = conn.execute("SELECT COUNT(*) FROM api_calls WHERE ts >= ?", (day_start(),)).fetchone()[0]
        past = [r[0] for r in conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE ts < ? GROUP BY substr(ts, 1, 10) "
            "ORDER BY substr(ts, 1, 10) DESC LIMIT 7", (day_start(),),
        )]
    note = usage.detect_anomaly(today, past)
    if note:
        log.warning("⚠️ 用量異常：%s", note)
        log_api_event("anomaly", "daily_spike", detail=note)


class GeminiText(str):
    """Gemini 的文字回覆；.call_id 是這次呼叫在 api_calls 的編號，供事後回報效益。"""
    call_id = None


def usage_metadata(res):
    meta = getattr(res, "usage_metadata", None)
    get = lambda name: int(getattr(meta, name, 0) or 0)
    return get("prompt_token_count"), get("candidates_token_count"), get("thoughts_token_count")


TRANSIENT_RETRY_WAITS = (4.0, 10.0)  # 遇到 503 等伺服器暫時錯誤時，每次重試前等幾秒（最多重試 2 次）


async def ask_gemini(contents, system_instruction=None, temperature=0.3, json_mode=False, feature="other"):
    user_id = usage.current_user.get()
    prompt_text = contents if isinstance(contents, str) else " ".join(c for c in contents if isinstance(c, str))
    prompt_chars = len(prompt_text) + len(system_instruction or "")
    prompt_key = hash((system_instruction, prompt_text, json_mode))

    try:
        await guard.acquire(user_id, prompt_key, prompt_chars, today_call_count())
    except usage.UsageLimitError as e:
        log_api_event("blocked", e.reason, user_id, feature, str(e))
        raise

    started = time.monotonic()
    attempt = 0
    model = GEMINI_MODEL
    while True:
        try:
            res = await ai_client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json" if json_mode else None,
                ),
            )
            break
        except Exception as e:
            kind = usage.classify_error(e)
            failed_id = log_api_call(feature, user_id, prompt_chars, (0, 0, 0),
                                     int((time.monotonic() - started) * 1000), False, kind, str(e)[:500], model=model)
            save_payload(failed_id, system_instruction, contents, "", f"{type(e).__name__}: {e}"[:2000])
            opened = guard.record_result(False, kind)
            if opened:
                log.warning("斷路器開啟：%s", opened)
                log_api_event("breaker", kind, user_id, feature, opened)
            # 主模型過載：立刻換備援模型再試（不用等；備援也失敗才走下面的等待重試）
            if (kind == "server" and GEMINI_FALLBACK_MODEL and model == GEMINI_MODEL
                    and GEMINI_FALLBACK_MODEL != GEMINI_MODEL and guard.breaker_remaining() == 0):
                model = GEMINI_FALLBACK_MODEL
                log.warning("%s 暫時無法使用，改用備援模型 %s", GEMINI_MODEL, model)
                log_api_event("fallback", kind, user_id, feature, f"{GEMINI_MODEL} -> {model}")
                started = time.monotonic()
                continue
            # 伺服器暫時忙碌（503 等）：等一下再試，免得使用者錄了十分鐘的音就這樣作廢
            if kind == "server" and attempt < len(TRANSIENT_RETRY_WAITS) and guard.breaker_remaining() == 0:
                wait = TRANSIENT_RETRY_WAITS[attempt]
                attempt += 1
                log.warning("Gemini 暫時無法使用，%.0f 秒後重試（第 %d 次）：%s", wait, attempt, str(e)[:80])
                await asyncio.sleep(wait)
                started = time.monotonic()
                continue
            raise

    guard.record_result(True)
    call_id = log_api_call(
        feature, user_id, prompt_chars, usage_metadata(res),
        int((time.monotonic() - started) * 1000), True, None, model=model,
    )
    check_daily_anomaly()
    text = GeminiText(res.text or "")
    text.call_id = call_id
    save_payload(call_id, system_instruction, contents, str(text))
    return text


def parse_reply(text):
    """拆成 (給使用者看的文字, 結構化資料 dict 或 None)。"""
    reply, _, raw = text.partition(DELIMITER)
    reply = reply.strip()
    raw = raw.strip()
    if not raw:
        return reply, None
    # 容忍模型包上 ```json 圍欄
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = jsonutil.loads_lenient(raw)
    except json.JSONDecodeError:
        log.warning("無法解析 DATA_RECORD：%r", raw[:200])
        return reply, None
    if not isinstance(data, dict):
        return reply, None
    try:
        data["mastery"] = max(1, min(5, int(data.get("mastery", 3))))
    except (TypeError, ValueError):
        data["mastery"] = 3
    return reply, data


def chunk_text(text, limit=DISCORD_LIMIT):
    """依段落/換行切成不超過 limit 的片段。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def send_long(interaction, text):
    for chunk in chunk_text(text) or ["（沒有內容）"]:
        await interaction.followup.send(chunk)


def save_record(data, *, category, target_word, user_input, default_mastery=3):
    save_to_db(
        category=data.get("category", category),
        target_word=data.get("target_word") or target_word,
        user_input=user_input,
        polished=data.get("polished_sentence", ""),
        synonyms=data.get("synonyms", ""),
        feedback=data.get("feedback", ""),
        mastery=data.get("mastery", default_mastery),
        source={"Vocabulary": "word"}.get(category, ""),
    )
    refresh_weekly()


# ----------------- 4. Discord Bot -----------------
class IeltsTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 記下是誰發的指令：資料隔離、每人頻率限制與用量統計都靠它
        usage.current_user.set(interaction.user.id)

        # 只服務白名單伺服器（私訊的指令只在全域註冊時才會出現，這裡不擋）
        if ALLOWED_GUILD_IDS and interaction.guild_id and str(interaction.guild_id) not in ALLOWED_GUILD_IDS:
            log_command("ielts", interaction, False, "未授權的伺服器")
            await interaction.response.send_message("這個伺服器沒有被授權使用這個 Bot。", ephemeral=True)
            return False

        name = interaction.command.name if interaction.command else ""
        if name == "register":
            log_command("ielts", interaction)
            return True
        user = accounts.get_user(db_connect, interaction.user.id)
        if user is None:
            log_command("ielts", interaction, False, "尚未註冊")
            await interaction.response.send_message(
                "👋 你還沒註冊。先用 `/register` 設定你的聽說讀寫基礎分數，之後就能開始練習了！", ephemeral=True
            )
            return False
        if user["blocked"]:
            log_command("ielts", interaction, False, "帳號被停用")
            await interaction.response.send_message("⛔ 你的帳號已被管理員停用。", ephemeral=True)
            return False
        accounts.touch_user(db_connect, interaction.user.id, interaction.user.display_name)
        log_command("ielts", interaction)
        return True


class IeltsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # 測驗模式要讀取使用者在頻道打的答案
        super().__init__(intents=intents)
        self.tree = IeltsTree(self)
        self.quizzes = {}  # (channel_id, user_id) -> QuizSession
        self.locks = {}  # (channel_id, user_id) -> asyncio.Lock：同一個人的訊息一次處理一則（語音與 stop 同時到時不搶狀態）

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # 同一個頻道可以有很多人各自進行測驗：以「頻道 + 留言者」找出屬於誰的測驗，
        # 沒有進行中測驗的人，訊息直接略過（不記錄、不儲存）
        key = (message.channel.id, message.author.id)
        session = self.quizzes.get(key)
        if not session:
            return
        # 只記字數與附件數，不把使用者的答案內容寫進日誌
        log.info("測驗作答 user=%s 字數=%d 附件=%d", message.author.id, len(message.content), len(message.attachments))
        if isinstance(session, ExerciseSession):
            handler = handle_exercise_answer
        elif isinstance(session, ListenTestSession):
            handler = handle_listen_answer
        elif isinstance(session, SpeakingSession):
            handler = handle_speaking_answer
        else:
            handler = handle_quiz_answer
        session.last_active = time.monotonic()  # 有回應就重新計時
        usage.current_user.set(message.author.id)  # 之後讀寫的資料都屬於這位使用者
        async with self.locks.setdefault(key, asyncio.Lock()):
            if self.quizzes.get(key) is not session:  # 等鎖的時候這輪已經結束了（例如剛打了 stop）
                log_event("ielts", "late_message", type(session).__name__, message.author.id, message.channel.id,
                          {"attachments": len(message.attachments)})
                if message.attachments:
                    await message.channel.send("這輪已經結束了，這則訊息沒有被收進去。")
                return
            try:
                await handler(self, message, session)
            except Exception as e:
                log.exception("處理測驗答案失敗")
                log_error("ielts", "on_message", e, message.author.id, message.channel.id,
                          session=type(session).__name__)
                await message.channel.send("⚠️ 處理答案時出錯了，請看終端機的錯誤訊息。")
            finally:
                session.last_active = time.monotonic()  # 處理（如評分）花的時間不算使用者閒置

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # 清掉以前可能註冊過的全域指令，避免選單裡同時出現兩份
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
        self._watchdog = asyncio.create_task(session_watchdog())  # 保留參考，避免被回收

    async def on_ready(self):
        log.info("已登入：%s", self.user)
        if not ALLOWED_GUILD_IDS:
            log.warning(
                "⚠️ 沒有設定伺服器白名單（ALLOWED_GUILD_IDS / DISCORD_GUILD_ID）：任何邀請 Bot 的伺服器都能使用它。"
                "目前所在的伺服器：%s。請把要保留的伺服器 ID 填進 .env 的 DISCORD_GUILD_ID。",
                "、".join(f"{g.name}（{g.id}）" for g in self.guilds) or "無",
            )
        for guild in list(self.guilds):
            await self.leave_if_unauthorized(guild)
        if not GUILD_ID:
            # 全域模式：清掉以前註冊在各伺服器的舊指令（例如已移除的 /submit），避免殘留
            for guild in list(self.guilds):
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)

    async def on_guild_join(self, guild: discord.Guild):
        await self.leave_if_unauthorized(guild)

    async def leave_if_unauthorized(self, guild: discord.Guild):
        """Bot 只待在白名單伺服器；被加進其他伺服器就自動退出。沒設白名單時不處理。"""
        if ALLOWED_GUILD_IDS and str(guild.id) not in ALLOWED_GUILD_IDS:
            log.warning("離開未授權的伺服器：%s (%s)", guild.name, guild.id)
            await guild.leave()


bot = IeltsBot()


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # interaction_check 已經回覆過使用者（未註冊、被停用、未授權的伺服器）
    original = getattr(error, "original", error)
    command = interaction.command.qualified_name if getattr(interaction, "command", None) else "?"
    if isinstance(original, usage.UsageLimitError):
        msg = f"🚦 {original}"  # 被用量防護攔下，屬於預期行為，不需要印錯誤堆疊
        log_event("ielts", "usage_limit", command, interaction.user.id, interaction.channel_id, {"reason": str(original)})
    else:
        log.exception("指令錯誤", exc_info=error)
        log_error("ielts", command, original, interaction.user.id, interaction.channel_id)
        msg = f"⚠️ 發生錯誤：{original}"
    if interaction.response.is_done():
        await interaction.followup.send(msg)
    else:
        await interaction.response.send_message(msg, ephemeral=isinstance(original, usage.UsageLimitError))


# ----------------- 4.5 帳號：註冊與管理 -----------------
class RegisterModal(discord.ui.Modal, title="設定你的雅思基礎分數"):
    """第一次使用時彈出的表單：填四項目前的程度（band），之後依練習表現自動調整。"""

    listening = discord.ui.TextInput(label="聽力 Listening band（1.0~9.0）", default=IELTS_LEVEL, max_length=4)
    speaking = discord.ui.TextInput(label="口說 Speaking band（1.0~9.0）", default=IELTS_LEVEL, max_length=4)
    reading = discord.ui.TextInput(label="閱讀 Reading band（1.0~9.0）", default=IELTS_LEVEL, max_length=4)
    writing = discord.ui.TextInput(label="寫作 Writing band（1.0~9.0）", default=IELTS_LEVEL, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        values, errors = {}, []
        for skill in skills.SKILLS:
            field = getattr(self, skill)
            try:
                values[skill] = accounts.parse_band(field.value)
            except ValueError:
                errors.append(f"{skills.SKILL_NAMES[skill]}：「{field.value}」不是 1.0~9.0 的數字")
        if errors:
            await interaction.response.send_message(
                "⚠️ 有欄位格式不對，請再用 `/register` 重新填一次：\n" + "\n".join(f"- {e}" for e in errors),
                ephemeral=True,
            )
            return
        try:
            user = accounts.register_user(db_connect, interaction.user.id, interaction.user.display_name, values)
        except sqlite3.IntegrityError:
            await interaction.response.send_message("你已經註冊過了。", ephemeral=True)
            return
        levels = "｜".join(f"{SKILL_EMOJI[s]} {skills.SKILL_NAMES[s]} {values[s]:.1f}" for s in skills.SKILLS)
        admin_note = "\n🛠️ 你是這個 Bot 的管理員（可用 `/users`、`/block`、`/unblock`、`/usage`）。" if user["is_admin"] else ""
        await interaction.response.send_message(
            f"✅ 註冊完成！你的起始評級：\n{levels}\n"
            "之後會依你的練習表現自動調整；想手動修改可以用 `/level 技能 band`。\n\n"
            "**開始練習**：`/search` 查單字｜`/word_quiz` 單字測驗｜`/reading` 閱讀｜`/listen` 聽力｜"
            "`/speaking` 口說模擬測驗｜`/weekly` 週報"
            + admin_note,
            ephemeral=True,
        )


@bot.tree.command(name="register", description="第一次使用：設定你的聽說讀寫基礎分數")
async def register_cmd(interaction: discord.Interaction):
    if accounts.get_user(db_connect, interaction.user.id):
        await interaction.response.send_message(
            "你已經註冊過了。`/level` 可以查看評級，`/level 技能 band` 可以手動調整。", ephemeral=True
        )
        return
    await interaction.response.send_modal(RegisterModal())


def is_admin(user_id):
    user = accounts.get_user(db_connect, user_id)
    return bool(user and user["is_admin"])


async def require_admin(interaction):
    if is_admin(interaction.user.id):
        return True
    await interaction.response.send_message("只有管理員可以使用這個指令。", ephemeral=True)
    return False


@bot.tree.command(name="users", description="（管理員）列出所有使用者與今日用量")
async def users_cmd(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    rows = accounts.list_users(db_connect)
    with db_connect() as conn:
        calls = dict(conn.execute(
            "SELECT user_id, COUNT(*) FROM api_calls WHERE ts >= ? GROUP BY user_id", (day_start(),)
        ).fetchall())
    lines = [f"👥 **使用者 {len(rows)} 人**（全域今日 {today_call_count()}/{guard.limits.rpd} 次）"]
    for user_id, name, created, last_seen, admin, blocked in rows:
        flags = ("🛠️" if admin else "") + ("⛔" if blocked else "")
        lines.append(
            f"- {flags}**{name or '?'}**（<@{user_id}>）今日 {calls.get(user_id, 0)} 次｜"
            f"註冊 {str(created)[:10]}｜最近 {str(last_seen)[5:16]}"
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    for chunk in chunk_text("\n".join(lines)):
        await interaction.followup.send(chunk, ephemeral=True)


async def set_block(interaction, member, blocked):
    if not await require_admin(interaction):
        return
    target = accounts.get_user(db_connect, member.id)
    if target is None:
        await interaction.response.send_message("對方還沒註冊，無法設定。", ephemeral=True)
    elif target["is_admin"]:
        await interaction.response.send_message("不能封鎖管理員。", ephemeral=True)
    else:
        accounts.set_blocked(db_connect, member.id, blocked)
        log_api_event("admin", "block" if blocked else "unblock", interaction.user.id, detail=f"target={member.id}")
        await interaction.response.send_message(
            f"{'⛔ 已停用' if blocked else '✅ 已恢復'} {member.mention}"
            + ("（對方進行中的練習會在閒置逾時後結束）" if blocked else ""),
            ephemeral=True,
        )


@bot.tree.command(name="block", description="（管理員）停用某位使用者")
@app_commands.describe(member="要停用的使用者")
async def block_cmd(interaction: discord.Interaction, member: discord.User):
    await set_block(interaction, member, True)


@bot.tree.command(name="unblock", description="（管理員）恢復某位使用者")
@app_commands.describe(member="要恢復的使用者")
async def unblock_cmd(interaction: discord.Interaction, member: discord.User):
    await set_block(interaction, member, False)


def apply_band(data, skill, sigma, detail, source=None):
    """把 Gemini 給的 band 估分更新到該技能的評級。回傳 (band, 舊評級, 新評級)；沒有有效分數則回傳 None。"""
    try:
        band = float((data or {}).get("band"))
    except (TypeError, ValueError):
        return None
    if not 1.0 <= band <= 9.0:
        return None
    before, after = update_skill_band(skill, band, sigma, source or skill, detail[:80])
    return band, before, after


def band_line(skill, result):
    band, before, after = result
    return f"📊 本次估計 band {band:.1f}｜{fmt_change(skill, before, after)}（±{after.rd:.1f}）"


@bot.tree.command(name="search", description="查詢雅思單字：英文釋義、同義替換、搭配詞與例句")
@app_commands.describe(word="要查詢的單字或片語")
async def word_cmd(interaction: discord.Interaction, word: str):
    await interaction.response.defer(thinking=True)
    prompt = f"請解析雅思字彙：{word}，提供詳細英文釋義、同義替換、高頻搭配詞與學術例句。"
    text = await ask_gemini(prompt, SYSTEM_INSTRUCTION, feature="word")
    reply, data = parse_reply(text)
    mark_call(text.call_id, 1, 1 if data else 0)  # 沒解析出結構化資料 = 這次沒存進單字庫
    await send_long(interaction, reply)
    if data:
        save_record(data, category="Vocabulary", target_word=word, user_input=word)


@bot.tree.command(name="word_review", description="從你的單字庫挑選掌握度較低的單字出題複習")
async def review_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    rows = pick_review_words()
    if not rows:
        await interaction.followup.send("目前資料庫中還沒有單字記錄，先用 `/search` 查幾個吧！")
        return

    words_info = "\n".join(f"- {r[1]} (過去備註: {r[2]})" for r in rows)
    prompt = (
        f"學生需要複習以下這幾個單字：\n{words_info}\n"
        "請設計 2 題雅思情境克漏字或同義改寫題進行測驗，答案放在最後並以 ||spoiler|| 包住。"
    )
    text = await ask_gemini(prompt, feature="review")
    mark_call(text.call_id, 1, 1 if text.strip() else 0)
    await send_long(interaction, f"🎯 **弱點抽考複習**\n\n{text}")


# ----------------- 5. 互動測驗 -----------------
@dataclass
class QuizSession:
    items: list
    index: int = 0
    correct: int = 0
    missed: list = field(default_factory=list)
    db_id: int = 0
    scores: list = field(default_factory=list)  # [(得分 0~1, 題目難度 band)]，結束時用來更新評級
    last_active: float = field(default_factory=time.monotonic)  # 最後一次互動，逾時自動結束用

    @property
    def current(self):
        return self.items[self.index]


def blank_shape(answer):
    """答案的長相：每個字露出首字母，其餘一個字母一條底線，字與字之間空三格。
    count on -> `c _ _ _ _   o _`
    """
    def word_shape(w):
        seen_alpha = False
        out = []
        for ch in w:
            if ch.isalpha():
                out.append(ch if not seen_alpha else "_")
                seen_alpha = True
            else:
                out.append(ch)
        return " ".join(out)

    return "   ".join(word_shape(w) for w in answer.split())


def shape_caption(answer):
    words = answer.split()
    letters = sum(ch.isalpha() for ch in answer)
    return f"{len(words)} 個字，共 {letters} 個字母" if len(words) > 1 else f"{letters} 個字母"


CHUNK = 10  # 每次呼叫 Gemini 最多處理的題數，避免一次輸出太長被截斷

ITEM_SCHEMA = (
    '輸出 JSON 陣列，每個元素：{"word": 單字或片語的原形（小寫，專有名詞除外）, '
    '"answer": 學生要填入空格的確切字串：必須是句子文法唯一確定的詞形，可以是 word 的變化形'
    '（複數、第三人稱單數、過去式、-ing 等）；一律小寫（專有名詞與縮寫除外），即使空格在句首也一樣；片語要填完整, '
    '"also_accepted": 同樣正確的其他拼法（字串陣列，例如英美拼法差異；沒有就給 []）, '
    '"question": 英文題目句, '
    '"desc": 這個答案的意思描述，用一句英文定義，或繁體中文意思（也可兩者並列，用「／」分開）；'
    '絕對不能出現答案本身或這個單字的任何變化形, '
    '"note": 繁體中文簡短說明（詞性與需要的詞形，例如「動詞，過去式」「名詞，複數」）, '
    '"explanation": 一句繁體中文解釋該字意思與用法（作答後才會顯示）, '
    '"synonyms": 3~5 個英文同義字或近義詞（字串陣列，依雅思常用度排序）, '
    '"examples": 2~3 個使用該單字的英文例句（字串陣列，雅思寫作或口說情境）, '
    '"band": 這個單字的雅思難度（4.0~9.0 的數字，越難越高）}'
)
QUESTION_RULE = (
    "請讓學生『打出空格該填的確切詞形』作答，必須拼對大小寫、單複數與時態。題目一律用英文句子，不要用中文當題幹。"
    "題型：(a) 含空格 ____ 的英文情境句（優先）：空格應填入的詞形由句子文法唯一確定"
    "（例如 'Last year the company ____ its emissions.' 的答案是 reduced，不是 reduce），"
    "並在 note 標明詞性與詞形；(b) 一句英文釋義或換句話說（例如 A word that means ...），"
    "此時 answer 就是 word 原形。片語（如 count on）整個要填，詞形同樣要由文法確定。"
    "題目文字中絕對不能出現答案本身或這個單字的其他變化形。\n"
)


def as_list(value, limit):
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()][:limit]


def canonical(text):
    """整理成小寫原樣：首字母大寫的字（如 /search Mitigate 存下來的）還原，避免要求使用者打大寫。"""
    text = " ".join(text.split())
    return text.lower() if text.istitle() else text


def word_stem(text):
    # 去掉結尾的 e/y，才抓得到 mitigating、studies 這類詞幹會變的形態
    return re.sub(r"[ey]$", "", text) if len(text) > 4 else text


def mask_pattern(*forms):
    stems = sorted({re.escape(word_stem(f)) for f in forms if f}, key=len, reverse=True)
    return re.compile(r"\b(?:%s)\w{0,5}\b" % "|".join(stems), re.IGNORECASE)


def make_item(word, d, fallback_question="", call_id=None, fallback_answer=None):
    word = canonical(word)
    answer = canonical(str(d.get("answer") or fallback_answer or word))
    also = [canonical(a) for a in as_list(d.get("also_accepted"), 4)]
    mask = mask_pattern(word, answer)

    # 把答案（含各種變化形）遮成底線，空格長相跟答案一致；片語的多個空格合併成一個
    question = mask.sub("\0", d.get("question") or fallback_question)
    question = re.sub(r"_{2,}", "\0", question)
    question = re.sub(r"\0(?:[ \-]+\0)+", "\0", question)
    # Discord 會把 __ 當成底線格式而吃掉空格，所以放進程式碼格式裡
    question = question.replace("\0", f"`{blank_shape(answer)}`")

    try:
        band = max(4.0, min(9.0, float(d.get("band"))))
    except (TypeError, ValueError):
        band = None  # 沒給難度時，作答時用目前評級代替
    return {
        "word": word,  # 單字原形（單字庫、掌握度都以它為準）
        "answer": answer,  # 這題要填的確切字串
        "also": [a for a in also if a != answer],
        "question": question,
        "hint": f"`{blank_shape(answer)}`（{shape_caption(answer)}，已露出首字母）",
        "desc": mask.sub("____", str(d.get("desc", "")).strip()),  # 意思描述；萬一提到答案也遮掉
        "note": mask.sub("____", str(d.get("note", "")).strip()),
        "explanation": d.get("explanation", ""),
        "synonyms": as_list(d.get("synonyms"), 5),
        "examples": as_list(d.get("examples"), 3),
        "band": band,
        "call_id": call_id,  # 產生這題的 API 呼叫，結束時回報『實際用到幾題』
    }


def fallback_question_for(word, example):
    """Gemini 不可用時的備援：用學過的英文例句遮掉單字。回傳 (題目, 例句中實際出現的詞形或 None)。"""
    if example:
        m = re.search(r"\b%s\w{0,5}\b" % re.escape(word_stem(word)), example, re.IGNORECASE)
        if m:
            form = m.group(0)
            return example, form.lower() if word == word.lower() else form
    return "Type the word you have learned. See the note below.", None


async def build_quiz_items(rows):
    """為已學過的單字出題（分批呼叫 Gemini）；失敗時退回用英文例句與備註出題。"""
    items = []
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        words = [r[0] for r in chunk]
        prompt = (
            "你是雅思單字教練。請為下列每個單字各出一題。" + QUESTION_RULE
            + f"單字：{json.dumps(words, ensure_ascii=False)}\n" + ITEM_SCHEMA
        )
        by_word, call_id = {}, None
        try:
            raw = await ask_gemini(prompt, json_mode=True, feature="quiz_build")
            call_id = raw.call_id
            data = jsonutil.loads_lenient(raw)
            by_word = {str(d.get("word", "")).lower(): d for d in data
                       if isinstance(d, dict) and d.get("question")}
        except Exception as e:
            # 包含被用量防護攔下的情況：這些單字本來就有例句與備註，可以不靠 Gemini 出題
            log.warning("產生測驗題目失敗，改用例句出題：%s", e)
        mark_call(call_id, len(chunk), sum(1 for w in words if w.lower() in by_word))
        for word, feedback, example in chunk:
            d = by_word.get(word.lower())
            if d:
                items.append(make_item(word, d, call_id=call_id))
                continue
            question, form = fallback_question_for(canonical(word), example)
            # 備援時沒有新的描述，用單字庫裡存的中文解釋當描述
            items.append(make_item(word, {"desc": feedback or ""}, question, call_id, fallback_answer=form))
    return items


async def generate_new_items(count, exclude):
    """依目前程度讓 Gemini 分批生成新單字與題目，並存進單字庫。"""
    known = {w.lower() for w in exclude}
    items = []
    attempts = 0
    while len(items) < count and attempts < count // CHUNK + 3:
        attempts += 1
        need = min(CHUNK, count - len(items))
        level = vocab_level()
        prompt = (
            f"學習者目前的雅思程度約 band {level:.1f}。請挑選 {need} 個適合他往上一級（約 band "
            f"{level:.1f}~{level + 1.0:.1f}）的雅思高頻單字或片語，涵蓋不同主題，"
            "不要太簡單也不要太冷僻，且不能是下列已出現過的字：\n"
            f"{json.dumps(sorted(known), ensure_ascii=False)}\n"
            "每個單字各出一題。" + QUESTION_RULE + ITEM_SCHEMA
        )
        try:
            raw = await ask_gemini(prompt, json_mode=True, temperature=0.8, feature="quiz_new")
            data = jsonutil.loads_lenient(raw)
        except usage.UsageLimitError:
            if items:
                break  # 已有一部分題目，先用現有的
            raise
        except Exception as e:
            log.warning("生成新單字失敗（第 %d 次）：%s", attempts, e)
            break
        accepted = 0
        for d in data:
            word = canonical(str(d.get("word", ""))) if isinstance(d, dict) else ""
            if not word or word.lower() in known or not d.get("question"):
                continue
            known.add(word.lower())
            item = make_item(word, d, call_id=raw.call_id)
            save_to_db("Vocabulary", word, word, item["examples"][0] if item["examples"] else "",
                       ", ".join(item["synonyms"]), item["explanation"], 3, source="quiz")
            items.append(item)
            accepted += 1
        mark_call(raw.call_id, need, accepted)
    return items[:count]


def format_extras(item):
    parts = []
    if item["answer"] != item["word"]:
        parts.append(f"📌 原形：**{item['word']}**")
    if item["explanation"]:
        parts.append(item["explanation"])
    if item["synonyms"]:
        parts.append("🔁 **同義字**：" + "、".join(item["synonyms"]))
    if item["examples"]:
        parts.append("✏️ **例句**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(item["examples"], 1)))
    return "\n".join(parts)


def format_question(session):
    item = session.current
    desc = f"\n📖 描述：{item['desc']}" if item.get("desc") else ""
    note = f"\n💬 {item['note']}" if item.get("note") else ""
    return (
        f"**第 {session.index + 1}/{len(session.items)} 題**\n"
        f"{item['question']}{desc}{note}\n"
        f"答案：{item['hint']}"
    )


SUFFIXES = ("s", "es", "d", "ed", "ing", "ly")


def diagnose(answer, target):
    """嚴格比對：大小寫、單複數、字尾、標點都必須完全一致（只忽略頭尾與重複的空白）。

    回傳 (結果, 說明)。結果：correct / spelling（很接近但不算對）/ wrong。
    """
    a, t = " ".join(answer.split()), " ".join(target.split())
    if a == t:
        return "correct", ""
    la, lt = a.lower(), t.lower()
    if la == lt:
        return "spelling", "大小寫不對"
    if a.strip(".,!?;:\"'") == t:
        return "spelling", "多打了標點符號"
    for longer, shorter in ((la, lt), (lt, la)):
        if longer.startswith(shorter) and longer[len(shorter):] in SUFFIXES:
            return "spelling", "字尾不對（單複數、時態或詞性）"
    if len(lt) >= 4 and difflib.SequenceMatcher(None, la, lt).ratio() >= 0.75:
        return "spelling", "拼字有誤，差一點"
    return "wrong", ""


def check_answer(answer, item):
    """依這題要填的確切詞形判分；英美拼法等 also 裡的寫法同樣算對。"""
    for target in (item["answer"], *item["also"]):
        if diagnose(answer, target)[0] == "correct":
            return "correct", ""
    if item["answer"] != item["word"] and " ".join(answer.split()) == item["word"]:
        return "spelling", "詞形不對：這題要配合句子的時態、單複數或詞性"
    return diagnose(answer, item["answer"])


def finish_quiz(client, key, session, reason="stop"):
    """結束測驗：移除 session、依表現調整程度，回傳總結文字。reason：stop / timeout。"""
    client.quizzes.pop(key, None)
    answered = session.index
    text = f"🏁 **測驗結束**：{session.correct}/{answered} 題答對"
    if session.missed:
        text += "\n需要加強：" + "、".join(session.missed)
    changes = {}
    if answered >= MIN_ANSWERED_TO_RATE:
        # 單字測驗讀、寫各計一半
        for skill in ("reading", "writing"):
            before, after = update_skill_items(
                skill, session.scores, 0.5, "quiz", f"quiz #{session.db_id}"
            )
            changes[skill] = [round(before.rating, 2), round(after.rating, 2)]
            text += "\n" + fmt_change(skill, before, after)
    else:
        text += f"\n（答滿 {MIN_ANSWERED_TO_RATE} 題才會更新評級）"

    # 回報每次 API 呼叫產生的題目最後實際被作答幾題（用來分析是否過度生成）
    consumed = Counter({it["call_id"]: 0 for it in session.items if it.get("call_id")})
    consumed.update(it["call_id"] for it in session.items[:answered] if it.get("call_id"))
    for call_id, n in consumed.items():
        mark_consumed(call_id, n)

    status = "completed" if answered >= len(session.items) else ("timeout" if reason == "timeout" else "stopped")
    try:
        close_quiz_session_row(session.db_id, status, changes)
    except Exception:
        log.exception("儲存測驗紀錄失敗")
        return text + "\n⚠️ 測驗紀錄存檔失敗，請看終端機錯誤訊息。"
    return text + weekly_note()


async def reply_long(message, text):
    """回覆並自動分段（Discord 單則上限 2000 字）；缺回覆權限時改直接發送。"""
    first = True
    for chunk in chunk_text(text):
        try:
            if first:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)
        except discord.Forbidden:
            log.warning("沒有回覆權限（缺『讀取訊息歷史記錄』？），改直接發送")
            await message.channel.send(chunk)
        first = False


async def handle_quiz_answer(client, message, session):
    key = (message.channel.id, message.author.id)
    text = message.content.strip()
    lowered = text.lower()

    if lowered in ("stop", "quit", "結束"):
        await reply_long(message, finish_quiz(client, key, session))
        return

    item = session.current
    word = item["word"]
    skipped = lowered in ("skip", "跳過")
    answer = item["answer"]  # 這題要填的確切詞形（可能是 word 的變化形）
    result, detail = ("skip", "") if skipped else check_answer(text, item)

    if result == "correct":
        record_quiz_result(word, True)
        session.correct += 1
        reply = "✅ 答對了！"
    else:
        record_quiz_result(word, False)
        session.missed.append(answer)
        if result == "spelling":
            # 很接近但仍算錯；告訴使用者錯在哪，並記成 spelling 方便日後分析常犯的拼字錯誤
            reply = f"❌ 拼字不對（{detail}）。你打的是 `{text}`，正確是 **{answer}**"
        else:
            reply = f"{'⏭️ 跳過' if skipped else '❌ 不對'}，答案是 **{answer}**"
    extras = format_extras(item)
    if extras:
        reply += "\n" + extras

    score = 1.0 if result == "correct" else 0.0
    session.scores.append((score, item["band"] or vocab_level()))
    log_quiz_answer(session.db_id, session.index + 1, item, "" if skipped else text, result)
    enrich_word(word, item["synonyms"], item["examples"])

    session.index += 1
    if session.index >= len(session.items):
        reply += "\n\n" + finish_quiz(client, key, session)
    else:
        reply += "\n\n" + format_question(session)
    await reply_long(message, reply)


@bot.tree.command(name="word_quiz", description="開始單字測驗：Bot 出題，你直接在頻道打出單字")
@app_commands.describe(
    count="題數（1~50，預設 5）",
    new_only="只考全新的單字（預設：先考學過的弱點字，不足再補新字）",
)
async def quiz_cmd(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 1, 50] = 5,
    new_only: bool = False,
):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.quizzes:
        await interaction.response.send_message(
            "你在這個頻道已經有測驗進行中，打 `stop` 可以結束。", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    rows = [] if new_only else pick_quiz_words(count)
    items = await build_quiz_items(rows) if rows else []

    shortfall = count - len(items)
    if shortfall > 0:
        known = known_words()
        try:
            items += await generate_new_items(shortfall, known)
        except usage.UsageLimitError as e:
            if not items:
                raise
            await interaction.followup.send(f"🚦 {e}\n先用現有的 {len(items)} 題開始。")
    if not items:
        await interaction.followup.send("⚠️ 產生題目失敗（可能是 Gemini 額度用完），請稍後再試。")
        return

    session = QuizSession(items=items)
    session.db_id = create_quiz_session_row(interaction.user.id, interaction.channel_id, len(items))
    bot.quizzes[key] = session
    await interaction.followup.send(
        "🎯 **單字測驗開始！** 直接在頻道輸入答案。\n"
        "⚠️ 拼字要完全正確：大小寫、單複數、字尾都算。\n"
        "輸入 `skip` 跳過、`stop` 結束。\n\n" + format_question(session)
    )


# ----------------- 6. 閱讀 / 聽力練習 -----------------
@dataclass
class ExerciseSession:
    kind: str  # "read" | "listen"
    title: str
    passage: str
    questions: list
    db_id: int = 0
    index: int = 0
    correct: int = 0
    scores: list = field(default_factory=list)  # [(得分, 題目難度 band)]
    call_id: int = None  # 產生這份題目的 API 呼叫
    last_active: float = field(default_factory=time.monotonic)
    source: object = None  # sources.Article：真實文章改編時的出處
    records: list = field(default_factory=list)  # 閱讀：[{"answer", "result"}]，最後總結用
    question_msg_id: int = None  # 目前顯示中的題目訊息，答完下一題前會先收掉

    @property
    def skill(self):
        return "reading" if self.kind == "read" else "listening"


def finish_exercise(client, key, session, reason="stop"):
    """結束練習。閱讀回傳多則訊息（總評＋逐題的答案、原文依據與解說）；聽力回傳單一字串。"""
    client.quizzes.pop(key, None)
    answered = session.index
    name = skills.SKILL_NAMES[session.skill]
    text = f"🏁 **{name}練習結束**：{session.correct}/{answered} 題答對"

    change = {}
    if answered >= MIN_ANSWERED_TO_RATE:
        before, after = update_skill_items(
            session.skill, session.scores, 1.0, session.kind, f"{session.kind} #{session.db_id}"
        )
        change[session.skill] = [round(before.rating, 2), round(after.rating, 2)]
        text += "\n" + fmt_change(session.skill, before, after) + f"（±{after.rd:.1f}）"
    else:
        text += f"\n（答滿 {MIN_ANSWERED_TO_RATE} 題才會更新評級）"

    if session.kind == "listen":
        text += "\n\n📜 **聽力原文**\n" + session.passage

    mark_consumed(session.call_id, answered)

    status = "completed" if answered >= len(session.questions) else ("timeout" if reason == "timeout" else "stopped")
    try:
        close_exercise_row(session.db_id, status, change)
        note = "\n" + weekly_note()
    except Exception:
        log.exception("儲存練習紀錄失敗")
        note = "\n⚠️ 練習紀錄存檔失敗，請看終端機錯誤訊息。"

    if session.kind == "read":
        blocks = ["### 📝 逐題解說（你的答案、正確答案、原文依據與解析）"] + exercises.summary_blocks(
            session.questions, session.records
        )
        messages = [text] + speaking.pack_blocks(blocks)
        messages[-1] += note
        return messages
    return text + note


async def delete_quietly(message):
    """刪掉使用者的作答訊息。沒有『管理訊息』權限時改在上面加個 ✅，讓使用者知道有收到。"""
    try:
        await message.delete()
    except discord.Forbidden:
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
    except discord.HTTPException:
        pass  # 已經被刪掉了


async def delete_message_id(channel, message_id):
    """刪掉 Bot 自己發的訊息（不需要特別權限）；找不到就算了。"""
    if message_id:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass


async def send_texts(channel, texts):
    """依序直接發在頻道（不用回覆，因為被回覆的訊息可能已經刪掉了）。"""
    for text in [texts] if isinstance(texts, str) else texts:
        for chunk in chunk_text(text):
            await channel.send(chunk)


async def handle_exercise_answer(client, message, session):
    key = (message.channel.id, message.author.id)
    text = message.content.strip()
    lowered = text.lower()
    # 閱讀：答完一題就收掉上一題，下一題才會緊貼在文章下面；全部答完才一次公布答案與解說
    quiet = session.kind == "read"

    if lowered in ("stop", "quit", "結束"):
        if quiet:
            await delete_message_id(message.channel, session.question_msg_id)
            await delete_quietly(message)
            await send_texts(message.channel, finish_exercise(client, key, session))
        else:
            await reply_long(message, finish_exercise(client, key, session))
        return

    item = session.questions[session.index]
    skipped = lowered in ("skip", "跳過")
    result = "skip" if skipped else exercises.check_answer(item, text)
    if result is None:  # 輸入格式不認得：不計分，請重答
        hint = "A / B / C / D" if item["type"] == "mcq" else "T / F / NG"
        await reply_long(message, f"看不懂這個答案，請輸入 {hint}（或 `skip`、`stop`）。")
        return

    correct = result == "correct"
    session.correct += correct
    difficulty = item["band"] or load_rating(session.skill).rating
    session.scores.append((1.0 if correct else 0.0, difficulty))
    session.records.append({"answer": "" if skipped else text, "result": result})
    log_exercise_answer(session.db_id, session.index + 1, item, "" if skipped else text, result)
    session.index += 1

    if quiet:
        await delete_quietly(message)
        await delete_message_id(message.channel, session.question_msg_id)
        session.question_msg_id = None
        if session.index >= len(session.questions):
            await send_texts(message.channel, finish_exercise(client, key, session))
        else:
            sent = await message.channel.send(
                exercises.format_question(session.questions[session.index], session.index, len(session.questions))
                + "\n（`skip` 跳過、`stop` 結束）"
            )
            session.question_msg_id = sent.id
        return

    # 聽力：維持逐題回饋
    if correct:
        reply = "✅ 答對了！"
    else:
        reply = f"{'⏭️ 跳過' if skipped else '❌ 不對'}，答案是 **{exercises.answer_text(item)}**"
    if item["explanation"]:
        reply += "\n" + item["explanation"]
    if session.index >= len(session.questions):
        reply += "\n\n" + finish_exercise(client, key, session)
    else:
        reply += "\n\n" + exercises.format_question(
            session.questions[session.index], session.index, len(session.questions)
        )
    await reply_long(message, reply)


async def generate_reading_real(interaction, level, topic, length):
    """真實文章改編：抓文章 → 一次呼叫 Gemini 改編並出題。

    某篇改編失敗就換一篇（最多試兩篇）；找不到文章或都失敗時回傳 None，由呼叫端退回原創短文。
    """
    cfg = exercises.LENGTHS[length]
    used = used_reading_urls()
    await interaction.followup.send("🔎 正在挑一篇適合你程度的真實文章並改編…（約 20~40 秒）")
    for _ in range(2):
        article, failures = await sources.find_article(topic, int(cfg["words"] * 0.6), READING_SOURCES, used)
        for name, why in failures:
            log.info("閱讀來源 %s 沒成功：%s", name, why)
        if article is None:
            return None
        try:
            text = await ask_gemini(
                exercises.build_adaptation_prompt(article, level, length),
                json_mode=True, temperature=0.5, feature="reading_real",
            )
            data = exercises.parse_exercise(text, "read")
            if len(data["questions"]) < 3:
                raise ValueError("可用題目太少")
            mark_call(text.call_id, data["raw_count"], len(data["questions"]))
            return data, text.call_id, article
        except usage.UsageLimitError:
            raise
        except Exception as e:
            log.warning("改編 %s 失敗，換一篇：%s", article.url, e)
            used.add(article.url)
    return None


async def start_exercise(interaction, kind, topic, length=exercises.DEFAULT_LENGTH, mode="real"):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.quizzes:
        await interaction.response.send_message(
            "你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True
        )
        return
    if kind == "listen" and not tts.available():
        await interaction.response.send_message(
            f"找不到語音模型檔案（{tts.MODEL_FILE}、{tts.VOICES_FILE}），聽力練習暫時無法使用。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    skill = "reading" if kind == "read" else "listening"
    level = load_rating(skill).rating
    note, result = "", None
    try:
        if kind == "read" and mode == "real":
            result = await generate_reading_real(interaction, level, topic, length)
            if result is None:
                note = "ℹ️ 這次找不到合適的真實文章（來源暫時不可用或都不符合），改用原創短文。\n\n"
        if result is None:
            text = await ask_gemini(
                exercises.build_prompt(kind, level, topic, length), json_mode=True, temperature=0.8, feature=kind
            )
            data = exercises.parse_exercise(text, kind)
            mark_call(text.call_id, data["raw_count"], len(data["questions"]))
            result = (data, text.call_id, None)
        data, call_id, article = result
    except usage.UsageLimitError as e:
        await interaction.followup.send(f"🚦 {e}")
        return
    except Exception as e:
        log.warning("產生%s練習失敗：%s", kind, e)
        await interaction.followup.send("⚠️ 產生題目失敗（可能是 Gemini 額度用完或格式錯誤），請稍後再試。")
        return

    audio_path = None
    if kind == "listen":
        audio_path = AUDIO_DIR / f"listen_{interaction.id}.wav"
        try:
            # 語音合成吃 CPU，放到執行緒避免卡住 Bot；程度較低時放慢語速
            await asyncio.to_thread(
                tts.synthesize, data["segments"], audio_path, 0.9 if level < 5.5 else 1.0
            )
        except Exception:
            log.exception("語音合成失敗")
            await interaction.followup.send("⚠️ 語音合成失敗，請看終端機錯誤訊息。")
            audio_path.unlink(missing_ok=True)
            return

    session = ExerciseSession(kind, data["title"], data["passage"], data["questions"], call_id=call_id, source=article)
    session.db_id = create_exercise_row(kind, data["title"], topic, data["passage"], len(data["questions"]), article)
    bot.quizzes[key] = session
    first_question = exercises.format_question(session.questions[0], 0, len(session.questions))
    try:
        if kind == "read":
            words = len(data["passage"].split())
            text = f"{note}📖 **{data['title']}**（約 {words} 字，{len(session.questions)} 題）\n\n{data['passage']}"
            if article:
                # 網址用 <> 包住，避免 Discord 展開成一大塊預覽
                text += f"\n\n📎 **來源**：改編自 {article.credit}\n<{article.url}>"
            await send_long(interaction, text)
            sent = await interaction.followup.send(
                "直接在頻道輸入答案，`skip` 跳過、`stop` 結束。\n"
                "📌 每答一題，上一題會自動收掉，下一題會貼在文章下面；全部答完才一次公布答案、原文依據與解說。\n\n"
                + first_question
            )
            session.question_msg_id = getattr(sent, "id", None)
        else:
            await interaction.followup.send(
                f"🎧 **{data['title']}**（可重複播放，原文會在練習結束後公開）\n"
                "直接在頻道輸入答案，`skip` 跳過、`stop` 結束。",
                file=discord.File(audio_path, filename="listening.wav"),
            )
            await interaction.followup.send(first_question)
    except Exception:
        bot.quizzes.pop(key, None)
        close_exercise_row(session.db_id, "abandoned")
        raise
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)


READING_MODES = [
    app_commands.Choice(name="真實文章改編（附來源連結，預設）", value="real"),
    app_commands.Choice(name="原創短文（Gemini 撰寫）", value="original"),
]
READING_LENGTHS = [
    app_commands.Choice(name="短｜約 250 字、5 題", value="short"),
    app_commands.Choice(name="標準｜約 500 字、7 題", value="standard"),
    app_commands.Choice(name="長｜約 800 字、10 題（預設）", value="long"),
]


@bot.tree.command(name="reading", description="閱讀練習：真實文章改編或原創短文，你在頻道作答")
@app_commands.describe(mode="文章來源（預設真實文章改編；抓不到時自動改用原創）", length="文章長度（預設長）",
                       topic="主題（選填，不填則自選）")
@app_commands.choices(mode=READING_MODES, length=READING_LENGTHS)
async def read_cmd(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str] = None,
    length: app_commands.Choice[str] = None,
    topic: str = "",
):
    await start_exercise(
        interaction, "read", topic,
        length=length.value if length else exercises.DEFAULT_LENGTH,
        mode=mode.value if mode else "real",
    )


MAX_PDF_BYTES = 15 * 1024 * 1024


@bot.tree.command(name="reading_pdf", description="上傳 PDF：Gemini 讀完後寫一篇全新的雅思風格文章並出題")
@app_commands.describe(file="PDF 檔案（15MB 以內）", length="文章長度（預設長）", topic="（選填）想著重的主題或角度")
@app_commands.choices(length=READING_LENGTHS)
async def reading_pdf_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    length: app_commands.Choice[str] = None,
    topic: str = "",
):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.quizzes:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not (file.filename.lower().endswith(".pdf") or (file.content_type or "").startswith("application/pdf")):
        await interaction.response.send_message("請上傳 PDF 檔案。", ephemeral=True)
        return
    if file.size > MAX_PDF_BYTES:
        await interaction.response.send_message("PDF 太大了，請控制在 15MB 以內。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    length_key = length.value if length else exercises.DEFAULT_LENGTH
    level = load_rating("reading").rating
    try:
        pdf = await file.read()  # 只放在記憶體裡，不存到硬碟
        text = await ask_gemini(
            [exercises.build_pdf_prompt(level, length_key, topic),
             types.Part.from_bytes(data=pdf, mime_type="application/pdf")],
            json_mode=True, temperature=0.6, feature="reading_pdf",
        )
        data = exercises.parse_exercise(text, "read")
        if len(data["questions"]) < 3:
            raise ValueError("可用題目太少")
        mark_call(text.call_id, data["raw_count"], len(data["questions"]))
    except usage.UsageLimitError as e:
        await interaction.followup.send(f"🚦 {e}")
        return
    except Exception as e:
        log.warning("PDF 閱讀練習失敗：%s", e)
        await interaction.followup.send(
            "⚠️ 讀取 PDF 或產生題目失敗。可能是 PDF 加密／損壞／內容太少，或是 Gemini 額度與回傳格式的問題，請換一份或稍後再試。"
        )
        return

    # 出處只記檔名；PDF 本身與原文都不會被貼到頻道或存下來
    origin = sources.Article(
        title=file.filename, text="", url=None, source="使用者上傳 PDF", license="使用者提供",
        credit=f"你上傳的 PDF「{file.filename}」",
    )
    session = ExerciseSession("read", data["title"], data["passage"], data["questions"],
                              call_id=text.call_id, source=origin)
    session.db_id = create_exercise_row("read", data["title"], topic, data["passage"], len(data["questions"]), origin)
    bot.quizzes[key] = session
    try:
        words = len(data["passage"].split())
        await send_long(
            interaction,
            f"📖 **{data['title']}**（約 {words} 字，{len(session.questions)} 題）\n\n{data['passage']}\n\n"
            f"📎 **來源**：依你上傳的 PDF「{file.filename}」重新撰寫的原創文章（PDF 內容不會貼到頻道）",
        )
        sent = await interaction.followup.send(
            "直接在頻道輸入答案，`skip` 跳過、`stop` 結束。\n"
            "📌 每答一題，上一題會自動收掉，下一題會貼在文章下面；全部答完才一次公布答案、原文依據與解說。\n\n"
            + exercises.format_question(session.questions[0], 0, len(session.questions))
        )
        session.question_msg_id = getattr(sent, "id", None)
    except Exception:
        bot.quizzes.pop(key, None)
        close_exercise_row(session.db_id, "abandoned")
        raise



# ----------------- 6.4 聽力模擬測驗（雅思格式：Section 1~4、每段 10 題）-----------------
LISTEN_PREVIEW_SECONDS = int(os.getenv("LISTEN_PREVIEW_SECONDS", "30"))  # 真實考試錄音前有時間先讀題


@dataclass
class ListenTestSession:
    sections: list  # 要做的 Section 編號，例如 [1] 或 [1, 2, 3, 4]
    topic: str
    level: float
    current: int = 0  # sections 的索引
    data: dict = None  # 目前這段：lt.parse_section 的結果
    answers: dict = field(default_factory=dict)  # 目前這段的 {題號(從 1): 使用者答案}
    db_id: int = 0
    call_id: int = None
    graded: bool = False  # 目前這段是否已經評分（避免重複評分）
    preparing: bool = True  # 正在產生題目與配音，這段時間收到的訊息先略過
    all_noted: bool = False  # 已經提醒過「全部答完了」
    results: list = field(default_factory=list)  # 已評分的段落：{"section","correct","total","blocks","script","title"}
    last_active: float = field(default_factory=time.monotonic)

    @property
    def section(self):
        return self.sections[self.current]

    @property
    def full(self):
        return len(self.sections) > 1


def recent_listen_titles(limit=20):
    with db_connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT title FROM exercise_sessions WHERE kind = 'listen_test' AND user_id = ? ORDER BY id DESC LIMIT ?",
            (uid(), limit))]


async def prepare_listen_section(session, tag):
    """產生這一段的腳本與題目（Gemini，最多兩次）並用本機語音配音。回傳 (資料, 音檔路徑)。"""
    section = session.section
    avoid = recent_listen_titles()
    data = None
    for attempt in range(2):
        prompt = lt.build_prompt(section, session.level, session.topic if not session.full else "", avoid)
        if attempt:  # 重試時換個說法，避免被重複 prompt 攔截
            prompt += "\n（上一次有題目的答案沒有出現在腳本裡，這次請確保每題答案都逐字出現在腳本中。）"
        text = await ask_gemini(prompt, json_mode=True, temperature=0.8, feature="listen_test")
        try:
            data = lt.parse_section(text, section)
        except ValueError as e:
            log.warning("聽力模擬 Section %d 第 %d 次不合格：%s", section, attempt + 1, e)
            log_event("ielts", "listen_test_invalid", f"section {section}", detail={"attempt": attempt + 1, "why": str(e)})
            continue
        mark_call(text.call_id, data["raw_count"], len(data["questions"]))
        session.call_id = text.call_id
        break
    if data is None:
        raise ValueError("題目連續兩次都不合格")
    path = AUDIO_DIR / f"listen_test_{tag}_s{section}.ogg"
    segments = [lt.narration(section, data)] + data["segments"]  # 開頭由旁白介紹這一段與說話者
    await asyncio.to_thread(tts.synthesize, segments, path, 0.9 if session.level < 5.5 else 1.0)
    return data, path


async def run_listen_section(session, send, tag):
    """準備並送出一段：題目卷 → 讀題時間 → 錄音。失敗回傳 False（呼叫端負責收尾與通知）。"""
    session.preparing = True
    path = None
    try:
        data, path = await prepare_listen_section(session, tag)
        session.data, session.answers, session.graded, session.all_noted = data, {}, False, False
        session.db_id = create_exercise_row("listen_test", data["title"], session.topic, data["script"], len(data["questions"]))
        sheet = lt.format_sheet(session.section, data)
        for chunk in chunk_text(sheet):
            await send(chunk)
        await send(lt.ANSWER_HELP + (f"\n⏳ {LISTEN_PREVIEW_SECONDS} 秒後開始播放錄音，**只會播一次**，先看一下題目。"
                                     if LISTEN_PREVIEW_SECONDS else ""))
        await asyncio.sleep(LISTEN_PREVIEW_SECONDS)
        await send(f"🎧 **Section {session.section}**", file=discord.File(path, filename=f"section{session.section}.ogg"))
        log_event("ielts", "listen_test_section", f"section {session.section}", detail={
            "title": data["title"], "questions": len(data["questions"]), "db_id": session.db_id})
        return True
    except usage.UsageLimitError as e:
        await send(f"🚦 {e}")
    except Exception as e:
        log.exception("聽力模擬產生失敗")
        log_error("ielts", "listen_test_prepare", e)
        await send("⚠️ 產生題目或配音失敗（可能是 Gemini 額度或格式問題），請稍後再試。")
    finally:
        session.preparing = False
        if path:
            path.unlink(missing_ok=True)
    return False


def grade_listen_section(session):
    """評分目前這段：記錄每題、更新聽力評級、存下結果供最後公布。"""
    if session.graded or not session.data:
        return
    session.graded = True
    data, section = session.data, session.section
    base = lt.difficulty(section, session.level)
    scores, correct = [], 0
    for i, q in enumerate(data["questions"], 1):
        given = session.answers.get(i)
        result = "skip" if given is None else lt.check_answer(q, given)
        correct += result == "correct"
        scores.append((1.0 if result == "correct" else 0.0, base))
        item = {**q, "band": base, "options": data["choices"] if q["type"] == "match" else q["options"]}
        log_exercise_answer(session.db_id, i, item, given or "", result)
    answered = len(session.answers)
    change = {}
    rating_line = ""
    if answered >= MIN_ANSWERED_TO_RATE:
        before, after = update_skill_items("listening", scores, 1.0, "listen_test", f"listen_test #{session.db_id}")
        change["listening"] = [round(before.rating, 2), round(after.rating, 2)]
        rating_line = fmt_change("listening", before, after) + f"（±{after.rd:.1f}）"
    mark_consumed(session.call_id, answered)
    try:
        status = "completed" if answered >= len(data["questions"]) else "stopped"
        close_exercise_row(session.db_id, status, change)
    except Exception:
        log.exception("儲存聽力模擬紀錄失敗")
    session.results.append({
        "section": section, "title": data["title"], "correct": correct, "total": len(data["questions"]),
        "answered": answered, "rating": rating_line, "script": data["script"],
        "blocks": lt.result_blocks(data["questions"], session.answers),
    })
    log_event("ielts", "listen_test_graded", f"section {section}", detail={"correct": correct, "answered": answered})


def finish_listen_test(client, key, session, reason="stop"):
    """結束測驗並回傳要發的訊息（清單）。目前這段還沒評分就先評分。"""
    client.quizzes.pop(key, None)
    if session.data and not session.graded and not session.preparing:
        grade_listen_section(session)
    results = session.results
    if not results:
        return ["🏁 聽力模擬測驗結束（還沒有完成任何一段，沒有計分）。"]
    total = sum(r["total"] for r in results)
    correct = sum(r["correct"] for r in results)
    lines = [f"🏁 **聽力模擬測驗結束**：{correct}/{total} 題答對"]
    for r in results:
        lines.append(f"　Section {r['section']}：{r['correct']}/{r['total']}　{r['title']}")
    if len(results) == 4:
        lines.append(f"📈 換算成績約 **band {lt.raw_to_band(correct, total):.1f}**（依 40 題換算表）")
    else:
        lines.append(f"📈 換算成 40 題約 band {lt.raw_to_band(correct, total):.1f}（只做了 {len(results)} 段，僅供參考）")
    lines += [r["rating"] for r in results if r["rating"]][-1:]
    messages = ["\n".join(lines)]
    for r in results:
        blocks = [f"### 📝 Section {r['section']} 逐題解說"] + r["blocks"] + ["### 📜 錄音原文\n" + r["script"]]
        messages += speaking.pack_blocks(blocks)
    messages[-1] += "\n" + weekly_note()
    return messages


async def handle_listen_answer(client, message, session):
    key = (message.channel.id, message.author.id)
    text = message.content.strip()
    lowered = text.lower()
    if session.preparing or not session.data:
        return  # 題目還在準備，這時的訊息不算作答
    if lowered in ("done", "submit", "交卷", "stop", "quit", "結束"):
        grade_listen_section(session)
        r = session.results[-1]
        last = session.current + 1 >= len(session.sections)
        if lowered in ("stop", "quit", "結束") or last:
            await send_texts(message.channel, finish_listen_test(client, key, session))
            return
        await message.channel.send(
            f"✅ Section {r['section']} 已交卷（成績最後一起公布）。正在準備 Section {session.sections[session.current + 1]}…（約 1~2 分鐘）")
        session.current += 1
        if not await run_listen_section(session, message.channel.send, message.id):
            session.current -= 1  # 這段沒出成：回到已評分的段落，直接收尾
            await send_texts(message.channel, finish_listen_test(client, key, session))
        return
    answers = lt.parse_answers(text, len(session.data["questions"]))
    if not answers:
        await message.reply("看不懂這則答案。請一行一題，例如 `1 Brown`、`2 B`；答完輸入 `done` 交卷。", mention_author=False)
        return
    session.answers.update(answers)
    log_event("ielts", "listen_test_answer", f"section {session.section}", message.author.id, message.channel.id,
              {"numbers": sorted(answers)})
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    total = len(session.data["questions"])
    if len(session.answers) >= total and not session.all_noted:
        session.all_noted = True
        await message.channel.send("📋 十題都答了。要修改可以再答一次同一題，確定了就輸入 `done` 交卷。")


LISTEN_TEST_SECTIONS = [
    app_commands.Choice(name="Section 1｜日常對話｜表格填空 10 題｜約 4 分鐘", value="1"),
    app_commands.Choice(name="Section 2｜日常獨白｜選擇＋配對 10 題｜約 4 分鐘", value="2"),
    app_commands.Choice(name="Section 3｜學術討論｜選擇＋填空 10 題｜約 4 分鐘", value="3"),
    app_commands.Choice(name="Section 4｜學術講座｜筆記填空 10 題｜約 4 分鐘", value="4"),
    app_commands.Choice(name="完整測驗｜Section 1~4 共 40 題｜約 25 分鐘｜換算 band", value="full"),
]


LISTEN_MODES = [
    app_commands.Choice(name="快速練習｜短錄音 5 題，每題立刻對答案（預設）", value="quick"),
    app_commands.Choice(name="雅思模擬｜Section 1~4，先讀題、交卷後才公布，換算 band", value="test"),
]


@bot.tree.command(name="listen", description="聽力練習：快速練習，或雅思格式的模擬測驗（Section 1~4）")
@app_commands.describe(mode="練習方式（預設快速練習）", section="（雅思模擬才需要）要做哪一段，預設 Section 1",
                       topic="主題（選填；完整測驗時忽略）")
@app_commands.choices(mode=LISTEN_MODES, section=LISTEN_TEST_SECTIONS)
async def listen_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str] = None,
                     section: app_commands.Choice[str] = None, topic: str = ""):
    if mode and mode.value == "test":
        await start_listen_test(interaction, section.value if section else "1", topic)
    else:
        await start_exercise(interaction, "listen", topic)


async def start_listen_test(interaction, choice, topic):
    key = (interaction.channel_id, interaction.user.id)
    if key in bot.quizzes:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not tts.available():
        await interaction.response.send_message(
            f"找不到語音模型檔案（{tts.MODEL_FILE}、{tts.VOICES_FILE}），聽力模擬測驗暫時無法使用。", ephemeral=True)
        return
    sections =[1, 2, 3, 4] if choice == "full" else [int(choice)]
    await interaction.response.defer(thinking=True)
    session = ListenTestSession(sections, topic, load_rating("listening").rating)
    bot.quizzes[key] = session
    await interaction.followup.send(
        f"🎧 **雅思聽力模擬測驗**（{'完整 4 段' if len(sections) > 1 else f'Section {sections[0]}'}）"
        f"正在準備題目與配音，約 1~2 分鐘…")
    if not await run_listen_section(session, interaction.channel.send, interaction.id):
        bot.quizzes.pop(key, None)


# ----------------- 6.5 口說模擬測驗（語音訊息作答）-----------------
# 選項文字直接標出題數、時間、額度與 Part 3 會不會追問，選的時候就看得到差別
SPEAKING_MENU = [
    ("all", "完整測驗｜Part 1+2+3｜約 12 分鐘｜Part 3 依你的 Part 2 回答追問｜3 次額度"),
    ("1", "Part 1｜日常問答 6 題｜約 4 分鐘｜2 次額度"),
    ("2", "Part 2｜題卡獨白 1 題｜約 3 分鐘｜2 次額度"),
    ("3", "Part 3｜抽象討論 4 題｜約 4 分鐘｜題目固定、不會追問｜2 次額度"),
    ("12", "Part 1+2｜日常問答＋題卡｜約 7 分鐘｜2 次額度"),
    ("23", "Part 2+3｜題卡＋追問｜約 7 分鐘｜Part 3 依你的回答追問｜3 次額度"),
    ("dialogue", "對話追問｜3 輪｜考官依你的回答接話｜回答由本機辨識，只用 2 次額度"),
]
DIALOGUE_MAX_LIVE = int(os.getenv("DIALOGUE_MAX_LIVE", "1"))  # 對話追問：每場最多讓 Gemini 即時接話幾次（其餘全靠事先備好的分支）
SPEAKING_PARTS = {"all": {1, 2, 3}, "1": {1}, "2": {2}, "3": {3}, "12": {1, 2}, "23": {2, 3}}


def is_adaptive(parts):
    """有 Part 2 又有 Part 3 時，Part 3 才有可以追問的依據。"""
    return {2, 3} <= set(parts)


def speaking_calls(parts):
    """一場測驗要用的 Gemini 呼叫：出題 1 + 評分 1，加上 Part 3 追問 1（若有）。"""
    return 2 + (1 if is_adaptive(parts) else 0)


@dataclass
class SpeakingSession:
    topic: str
    steps: list  # speaking.parse_set 的步驟，另外多一個 "audio"（考官配音檔路徑）
    closing_audio: str
    db_id: int = 0
    index: int = 0
    answers: list = field(default_factory=list)  # [{"seq", "step", "path", "mime", "seconds", "skipped"}]
    call_id: int = None
    followup_call_id: int = None  # 依 Part 2 回答生成 Part 3 追問的那次 API 呼叫
    level: float = 5.5
    speed: float = 1.0
    prefix: str = ""  # 暫存音檔的檔名前綴
    notice: str = ""  # 下一則題目訊息要附帶的說明（只顯示一次）
    dialogue: object = None  # 對話追問模式的狀態（dialogue.Dialogue）；一般口說測驗是 None
    last_active: float = field(default_factory=time.monotonic)

    @property
    def current(self):
        return self.steps[self.index]


def unlink_quiet(path):
    if path:
        Path(path).unlink(missing_ok=True)


def cleanup_speaking(session):
    for step in session.steps:
        unlink_quiet(step.get("audio"))
    unlink_quiet(session.closing_audio)
    for a in session.answers:
        unlink_quiet(a.get("path"))


EXAMINER_ACCENT = "en-gb"  # 同一場測驗考官口音一致


def render_steps_audio(steps, prefix, speed, only_pending=False):
    """把題目（含轉場說明）配成語音。CPU 運算，請用 to_thread 呼叫。"""
    for i, step in enumerate(steps):
        if bool(step.get("pending")) != only_pending:
            continue  # 一開始先配非追問的題目；追問題等 Part 2 答完才配
        path = AUDIO_DIR / f"{prefix}_{i}.wav"
        tts.synthesize(speaking.segments_for(step), path, speed, EXAMINER_ACCENT)
        step["audio"] = str(path)


def render_examiner_audio(steps, prefix, speed):
    """配好目前能配的題目與結尾語，回傳結尾語音檔路徑。"""
    render_steps_audio(steps, prefix, speed)
    closing = AUDIO_DIR / f"{prefix}_end.wav"
    tts.synthesize([{"speaker": "A", "text": speaking.CLOSING}], closing, speed, EXAMINER_ACCENT)
    return str(closing)


def audio_attachment(message):
    """找出回答用的音檔：Discord 語音訊息（audio/ogg）或一般音檔都可以。"""
    for a in message.attachments:
        ctype = (a.content_type or "").lower()
        if ctype.startswith("audio/") or Path(a.filename).suffix.lower() in AUDIO_MIME:
            return a
    return None


def speaking_step_text(session):
    step = session.current
    if session.dialogue is not None:
        head = f"**💬 對話追問 · 第 {session.index + 1}/{dialogue.ROUNDS} 輪**"
    else:
        head = f"**{speaking.PART_NAMES[step['part']]} · 第 {session.index + 1}/{len(session.steps)} 題**"
    if step["card"]:
        body = (
            f"📝 **題卡**\n>>> {step['card']}\n"
            "⏱️ 你有 1 分鐘準備，準備好就用語音訊息回答（說 1~2 分鐘）。"
        )
    else:
        body = f"🎧 聽考官的問題，用語音訊息回答。想核對文字：||{step['question']}||"
    notice, session.notice = session.notice, ""
    return (f"{notice}\n\n" if notice else "") + f"{head}\n{body}\n（`skip` 跳過、`stop` 結束）"


async def prepare_part3(message, session):
    """Part 2 答完、要進 Part 3 時：聽你的回答生成追問（失敗就用預備題），再配音。"""
    await message.channel.send("🧠 考官正在聽你的 Part 2 回答，準備追問…（約 20~30 秒）")
    pending = [s for s in session.steps if s.get("pending")]
    p2 = next((a for a in session.answers if a["step"]["part"] == 2 and a["path"]), None)
    note = "🧠 這幾題追問是考官依你剛才 Part 2 的回答產生的。"

    if p2 is None:
        note = "ℹ️ 你跳過了 Part 2，考官沒有可以追問的內容，這幾題是預備題。"
    elif today_call_count() + 2 > guard.limits.rpd:  # 還要留 1 次給最後的評分
        note = "ℹ️ 今天的 Gemini 額度不夠，這幾題用預備題，不依你的回答追問。"
    else:
        try:
            contents = [
                speaking.build_followup_prompt(session.level, p2["step"]["question"], len(pending)),
                types.Part.from_bytes(data=Path(p2["path"]).read_bytes(), mime_type=p2["mime"]),
            ]
            text = await ask_gemini(
                contents, json_mode=True, temperature=0.8, feature="speaking_followup"
            )
            questions = speaking.parse_followups(text, len(pending))
            for step, question in zip(pending, questions):
                step["question"] = question  # 不夠的題數保留預備題
            pending[0]["prefix"] = speaking.FOLLOWUP_PREFIX
            session.followup_call_id = text.call_id
            mark_call(text.call_id, len(pending), len(questions))
        except Exception as e:
            log.warning("生成 Part 3 追問失敗，改用預備題：%s", e)
            note = "ℹ️ 這次追問生成失敗（可能是額度或網路），改用預備題。"

    await asyncio.to_thread(render_steps_audio, session.steps, session.prefix, session.speed, True)
    for step in pending:
        step.pop("pending", None)
    session.notice = note


async def advance_dialogue(message, session):
    """對話追問：本地聽懂剛才的回答 -> 比對備好的分支 -> 決定下一題（沒命中才叫 Gemini）-> 配音後接在題目後面。"""
    d = session.dialogue
    if d.finished:
        return
    answer = session.answers[-1]
    transcript = ""
    if answer["path"]:
        try:
            transcript = await asyncio.to_thread(asr.transcribe, answer["path"])
        except Exception as e:
            log.exception("本地語音辨識失敗")
            log_error("ielts", "asr", e, message.author.id, message.channel.id)
    answer["local_transcript"] = transcript

    decision = d.decide(transcript)
    live_question = None
    if decision["how"] == "need_live":
        if today_call_count() + 2 > guard.limits.rpd:  # 還要留 1 次給最後的評分
            decision = {"how": "generic", "question": d.next_generic, "label": "", "node": None}
        else:
            try:
                text = await ask_gemini(
                    dialogue.build_live_prompt(
                        session.topic, d.history + [{"question": answer["step"]["question"], "transcript": transcript}],
                        session.level),
                    json_mode=True, temperature=0.7, feature="speaking_dialogue",
                )
                live_question = dialogue.parse_live(text)
                mark_call(text.call_id, 1, 1)
            except Exception as e:
                log.warning("對話追問即時接話失敗，改用通用追問：%s", e)
                decision = {"how": "generic", "question": d.next_generic, "label": "", "node": None}
    question = d.advance(decision, live_question)
    d.history.append({"round": d.round - 1, "question": answer["step"]["question"], "transcript": transcript,
                      "how": decision["how"], "label": decision["label"]})
    log_event("ielts", "dialogue_turn", decision["how"], message.author.id, message.channel.id,
              {"round": d.round - 1, "transcript": transcript[:500], "label": decision["label"], "next": question})

    step = {"part": 3, "question": question, "prefix": "", "card": None, "dialogue": True}
    path = AUDIO_DIR / f"{session.prefix}_d{d.round}.wav"
    await asyncio.to_thread(tts.synthesize, speaking.segments_for(step), path, session.speed, EXAMINER_ACCENT)
    step["audio"] = str(path)
    session.steps.append(step)

    how_text = {"branch": f"接話方式：你的回答命中「{decision['label']}」這個方向",
                "generic": "接話方式：沒有命中預備方向，用通用追問",
                "need_live": "接話方式：Gemini 即時接話"}[decision["how"]]
    heard = transcript if transcript else "（沒有辨識到內容）"
    session.notice = f"🗣️ 本機聽到的：||{heard[:300]}||\n🧭 {how_text}"


async def send_audio_reply(message, text, path):
    """回覆並附上考官語音；缺回覆權限時改直接發送。"""
    try:
        await message.reply(text, file=discord.File(path, filename="examiner.wav"))
    except discord.Forbidden:
        await message.channel.send(text, file=discord.File(path, filename="examiner.wav"))


async def reply_many(message, texts):
    """依序送出多則訊息：第一則用回覆，其餘直接發在頻道。每則都已控制在 Discord 長度上限內。"""
    await reply_long(message, texts[0])
    for text in texts[1:]:
        for chunk in chunk_text(text):
            await message.channel.send(chunk)


async def finish_speaking(client, key, session, reason="stop"):
    """結束口說測驗：評分、更新評級、存檔，回傳要依序顯示的訊息（list）。"""
    client.quizzes.pop(key, None)
    answered = [a for a in session.answers if not a["skipped"] and a["path"]]
    status = "completed" if session.index >= len(session.steps) else ("timeout" if reason == "timeout" else "stopped")
    mark_consumed(session.call_id, session.index)
    # 追問題實際被回答（或跳過）了幾題，用來分析這次額外呼叫值不值得
    mark_consumed(session.followup_call_id, sum(1 for a in session.answers if a["step"]["part"] == 3))
    try:
        if len(answered) < MIN_ANSWERED_TO_RATE:
            close_speaking_row(session.db_id, status)
            return [f"🏁 **口說模擬測驗結束**：只回答了 {len(answered)} 題，"
                    f"不足 {MIN_ANSWERED_TO_RATE} 題，這次不評分。" + weekly_note()]

        contents = [f"這是一場雅思口說模擬測驗，主題：{session.topic}。以下依序是每一題與學生的語音回答。"]
        for a in session.answers:
            contents.append(speaking.question_label(a["seq"], a["step"]))
            if a["skipped"]:
                contents.append("（學生跳過這題，沒有錄音）")
            else:
                contents.append(types.Part.from_bytes(data=Path(a["path"]).read_bytes(), mime_type=a["mime"]))
        try:
            text = await ask_gemini(
                contents, speaking.SCORING_SYSTEM, json_mode=True, feature="speaking_score"
            )
            data = speaking.parse_scoring(text)
        except usage.UsageLimitError as e:
            close_speaking_row(session.db_id, "unscored")
            return [f"🚦 {e}\n這次測驗的錄音無法評分，已丟棄；等額度恢復後再考一次吧。"]
        except Exception as e:
            log.warning("口說評分失敗：%s", e)
            close_speaking_row(session.db_id, "unscored")
            return ["⚠️ 評分失敗（可能是額度、網路問題或回傳格式不對），這次測驗無法評分，請稍後再考一次。"]

        transcripts = data.get("transcripts") if isinstance(data.get("transcripts"), list) else []
        mark_call(text.call_id, len(answered), sum(1 for t in transcripts if str(t).strip()))

        qa = [
            {"seq": a["seq"], "part": a["step"]["part"], "question": a["step"]["question"], "skipped": a["skipped"]}
            for a in session.answers
        ]
        built = speaking.build_report(data, qa)
        result = apply_band(data, "speaking", 0.5, f"speaking test #{session.db_id}", source="speaking_test")
        change = {"speaking": [round(result[1].rating, 2), round(result[2].rating, 2)]} if result else {}
        close_speaking_row(
            session.db_id, status, band=result[0] if result else None,
            criteria=data.get("criteria"), report=built["markdown"], transcripts=transcripts, skill_change=change,
        )
        # 第一則：總評與四項細項，後面接評級變化；接著是建議例句，最後是（可點開的）逐字稿
        tail = band_line("speaking", result) if result else "（這次沒有取得有效分數，評級未更新。）"
        messages = built["messages"]
        messages[0] = f"🏁 **口說模擬測驗結束**\n\n{messages[0]}\n\n{tail}"
        messages[-1] += weekly_note()
        return messages
    finally:
        cleanup_speaking(session)


async def handle_speaking_answer(client, message, session):
    key = (message.channel.id, message.author.id)
    lowered = message.content.strip().lower()

    if lowered in ("stop", "quit", "結束"):
        await reply_many(message, await finish_speaking(client, key, session))
        return

    step = session.current
    clip = audio_attachment(message)
    skipped = False
    path, mime, seconds = None, None, None
    if clip:
        if clip.size > MAX_AUDIO_BYTES:
            await reply_long(message, "這個音檔太大了，請控制在 15MB 以內（約幾分鐘的語音）。")
            return
        ext = Path(clip.filename).suffix.lower() or ".ogg"
        path = AUDIO_DIR / f"spk_{session.db_id}_{session.index + 1}{ext}"
        await clip.save(path)
        mime = AUDIO_MIME.get(ext) or (clip.content_type or "audio/ogg").split(";")[0]
        seconds = getattr(clip, "duration", None)
    elif lowered in ("skip", "跳過"):
        skipped = True
    else:
        await reply_long(
            message,
            "請用 **Discord 語音訊息**回答（輸入框右邊的麥克風圖示），也可以直接傳音檔。\n"
            "（`skip` 跳過這題、`stop` 結束測驗）",
        )
        return

    session.answers.append({
        "seq": session.index + 1, "step": step, "path": str(path) if path else None,
        "mime": mime, "seconds": seconds, "skipped": skipped,
    })
    log_speaking_answer(session.db_id, session.index + 1, step, seconds, skipped)
    session.index += 1
    if session.dialogue is not None:
        await advance_dialogue(message, session)

    if session.index >= len(session.steps):
        await send_audio_reply(message, "🎙️ 收到了，考官正在評分，大約要半分鐘…", session.closing_audio)
        await reply_many(message, await finish_speaking(client, key, session))
    else:
        if session.current.get("pending"):
            await prepare_part3(message, session)
        await send_audio_reply(message, speaking_step_text(session), session.current["audio"])


@bot.tree.command(name="speaking", description="口說模擬測驗：Gemini 出類雅思套題，考官配音，你用語音訊息作答")
@app_commands.describe(part="要練哪一部分（選項後面標了題數、時間、額度，以及 Part 3 會不會追問）",
                       topic="主題（選填，不填則自選）")
@app_commands.choices(part=[app_commands.Choice(name=name, value=value) for value, name in SPEAKING_MENU])
async def speaking_cmd(interaction: discord.Interaction, part: app_commands.Choice[str] = None, topic: str = ""):
    key = (interaction.channel_id, interaction.user.id)
    if part and part.value == "dialogue":
        await start_dialogue(interaction, key, topic)
        return
    parts = SPEAKING_PARTS[part.value if part else "all"]
    need = speaking_calls(parts)
    if key in bot.quizzes:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not tts.available():
        await interaction.response.send_message(
            f"找不到語音模型檔案（{tts.MODEL_FILE}、{tts.VOICES_FILE}），口說模擬測驗暫時無法使用。", ephemeral=True
        )
        return
    if today_call_count() + need > guard.limits.rpd:
        await interaction.response.send_message(
            f"今天剩下的 Gemini 額度不夠做這場測驗（需要 {need} 次），明天再來吧。", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    level = load_rating("speaking").rating
    try:
        with db_connect() as conn:  # 以前出過的題目（所有人），這次避開
            previous = [r[0] for r in conn.execute(
                "SELECT DISTINCT question FROM speaking_answers ORDER BY id DESC LIMIT 150")]
        for attempt in range(3):  # 有題目跟以前一樣或太像就重出（最多 3 次）
            text = await ask_gemini(
                speaking.build_generation_prompt(level, parts, topic, previous), json_mode=True, temperature=1.0,
                feature="speaking_gen",
            )
            call_id = text.call_id
            data = speaking.parse_set(text, parts)
            repeats = speaking.repeated_questions(data, previous)
            if not repeats:
                break
            log_event("ielts", "duplicate_speaking_set", "speaking", interaction.user.id, interaction.channel_id,
                      {"attempt": attempt + 1, "repeated": repeats})
        expected = (6 if 1 in parts else 0) + (1 if 2 in parts else 0) + (4 if 3 in parts else 0)
        mark_call(call_id, expected, len(data["steps"]))
    except usage.UsageLimitError as e:
        await interaction.followup.send(f"🚦 {e}")
        return
    except Exception as e:
        log.warning("產生口說題組失敗：%s", e)
        await interaction.followup.send("⚠️ 產生題目失敗（可能是 Gemini 額度用完或格式錯誤），請稍後再試。")
        return

    # 先把考官的題目配好音，測驗進行中就不用等。
    # 有 Part 2 的話，Part 3 先標成待定：等你答完 Part 2 再依回答生成追問並配音。
    steps = data["steps"]
    adaptive = is_adaptive(parts)
    if adaptive:
        for step in steps:
            if step["part"] == 3:
                step["pending"] = True
    prefix = f"spk_{interaction.id}"
    speed = 0.9 if level < 5.5 else 1.0
    try:
        closing = await asyncio.to_thread(render_examiner_audio, steps, prefix, speed)
    except Exception:
        log.exception("考官配音失敗")
        for f in AUDIO_DIR.glob(f"{prefix}_*"):
            f.unlink(missing_ok=True)
        await interaction.followup.send("⚠️ 考官配音失敗，請看終端機錯誤訊息。")
        return

    session = SpeakingSession(
        data["topic"], steps, closing, call_id=call_id, level=level, speed=speed, prefix=prefix
    )
    session.db_id = create_speaking_row(interaction.user.id, interaction.channel_id, data["topic"], len(steps))
    bot.quizzes[key] = session
    minutes = SESSION_IDLE_SECONDS / 60
    if adaptive:
        part3_line = "🧠 **Part 3 的追問會依你 Part 2 的回答產生**，答完 Part 2 後要等約 20~30 秒。\n"
    elif 3 in parts:
        part3_line = "ℹ️ 只練 Part 3：題目是固定的，不會依你的回答追問（要追問請選含 Part 2 的選項）。\n"
    else:
        part3_line = ""
    intro = (
        f"🎙️ **口說模擬測驗：{data['topic']}**（共 {len(steps)} 題）\n"
        "考官會用語音問你問題。請用 **Discord 語音訊息**回答（輸入框右邊的麥克風圖示），也可以傳音檔。\n"
        f"{part3_line}"
        "📋 **全部答完後才會統一評分**，中途不會給回饋；答滿 "
        f"{MIN_ANSWERED_TO_RATE} 題後打 `stop` 可以提前評分。\n"
        f"⏰ 超過 {minutes:g} 分鐘沒有回應，測驗會自動結束。\n\n"
    )
    try:
        await interaction.followup.send(
            intro + speaking_step_text(session), file=discord.File(session.current["audio"], filename="examiner.wav")
        )
    except Exception:
        bot.quizzes.pop(key, None)
        close_speaking_row(session.db_id, "abandoned")
        cleanup_speaking(session)
        raise


async def start_dialogue(interaction, key, topic):
    """口說「對話追問」：Gemini 一次產出對話樹，之後每輪由本機辨識＋關鍵字比對決定怎麼接話。"""
    need = 2 + DIALOGUE_MAX_LIVE
    if key in bot.quizzes:
        await interaction.response.send_message("你在這個頻道已經有練習進行中，打 `stop` 可以結束。", ephemeral=True)
        return
    if not tts.available():
        await interaction.response.send_message(
            f"找不到語音模型檔案（{tts.MODEL_FILE}、{tts.VOICES_FILE}），口說模擬測驗暫時無法使用。", ephemeral=True
        )
        return
    if not asr.available():
        await interaction.response.send_message(
            "對話追問需要本機語音辨識套件：請在環境裡執行 `pip install faster-whisper` 後重新啟動。", ephemeral=True
        )
        return
    if today_call_count() + need > guard.limits.rpd:
        await interaction.response.send_message(
            f"今天剩下的 Gemini 額度不夠做這場對話（最多需要 {need} 次），明天再來吧。", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    level = load_rating("speaking").rating
    try:
        with db_connect() as conn:
            previous = [r[0] for r in conn.execute(
                "SELECT DISTINCT question FROM speaking_answers ORDER BY id DESC LIMIT 150")]
        preload = asyncio.create_task(asyncio.to_thread(asr.preload))  # 語音辨識模型邊出題邊載入
        for attempt in range(2):
            text = await ask_gemini(
                dialogue.build_tree_prompt(level, topic, previous), json_mode=True, temperature=1.0,
                feature="speaking_dialogue_gen",
            )
            call_id = text.call_id
            tree = dialogue.parse_tree(text)
            if not speaking.repeated_questions({"steps": [{"question": tree["opening"]}]}, previous):
                break
            log_event("ielts", "duplicate_dialogue_opening", "speaking", interaction.user.id, interaction.channel_id,
                      {"attempt": attempt + 1, "opening": tree["opening"]})
        await preload
        mark_call(call_id, 1, 1)
    except usage.UsageLimitError as e:
        await interaction.followup.send(f"🚦 {e}")
        return
    except Exception as e:
        log.warning("產生對話追問題組失敗：%s", e)
        log_error("ielts", "dialogue_gen", e, interaction.user.id, interaction.channel_id)
        await interaction.followup.send("⚠️ 產生對話失敗（可能是 Gemini 額度、語音辨識模型下載或格式錯誤），請稍後再試。")
        return

    step = {"part": 3, "question": tree["opening"], "card": None, "dialogue": True,
            "prefix": f"Good morning. This is the IELTS speaking practice. Let's have a short conversation about {tree['topic']}."}
    prefix = f"spk_{interaction.id}"
    speed = 0.9 if level < 5.5 else 1.0
    try:
        closing = await asyncio.to_thread(render_examiner_audio, [step], prefix, speed)
    except Exception:
        log.exception("考官配音失敗")
        for f in AUDIO_DIR.glob(f"{prefix}_*"):
            f.unlink(missing_ok=True)
        await interaction.followup.send("⚠️ 考官配音失敗，請看終端機錯誤訊息。")
        return

    session = SpeakingSession(tree["topic"], [step], closing, call_id=call_id, level=level, speed=speed, prefix=prefix)
    session.dialogue = dialogue.Dialogue(tree, DIALOGUE_MAX_LIVE)
    session.db_id = create_speaking_row(interaction.user.id, interaction.channel_id, tree["topic"], dialogue.ROUNDS)
    bot.quizzes[key] = session
    intro = (
        f"💬 **口說對話追問：{tree['topic']}**（共 {dialogue.ROUNDS} 輪）\n"
        "考官問一題，你用 **Discord 語音訊息**回答；考官會依你說的內容接著追問。\n"
        "🧠 你的回答由**本機**辨識並比對事先備好的追問方向，沒對上才會請 Gemini 即時接話。\n"
        f"📋 {dialogue.ROUNDS} 輪都答完才會統一評分（至少答 {MIN_ANSWERED_TO_RATE} 題才計入評級）。\n"
        f"⏰ 超過 {SESSION_IDLE_SECONDS / 60:g} 分鐘沒有回應，會自動結束。\n\n"
    )
    try:
        await interaction.followup.send(
            intro + speaking_step_text(session), file=discord.File(session.current["audio"], filename="examiner.wav")
        )
    except Exception:
        bot.quizzes.pop(key, None)
        close_speaking_row(session.db_id, "abandoned")
        cleanup_speaking(session)
        raise


# ----------------- 6.9 逾時自動結束 -----------------
SESSION_KIND_NAME = {QuizSession: "單字測驗", ExerciseSession: "練習", SpeakingSession: "口說模擬測驗",
                     ListenTestSession: "聽力模擬測驗"}


async def expire_session(key, session):
    """使用者太久沒回應：收尾（必要時評分）並在頻道提示。"""
    usage.current_user.set(key[1])  # 背景工作沒有指令或訊息可以帶出身分，這裡自己設定
    if isinstance(session, SpeakingSession):
        summary = await finish_speaking(bot, key, session, reason="timeout")
    elif isinstance(session, ListenTestSession):
        summary = finish_listen_test(bot, key, session, reason="timeout")
    elif isinstance(session, ExerciseSession):
        summary = finish_exercise(bot, key, session, reason="timeout")
    else:
        summary = finish_quiz(bot, key, session, reason="timeout")
    name = SESSION_KIND_NAME.get(type(session), "練習")
    if isinstance(session, ExerciseSession):
        name = f"{skills.SKILL_NAMES[session.skill]}練習"
    notice = f"⏰ <@{key[1]}> 超過 {SESSION_IDLE_SECONDS / 60:g} 分鐘沒有回應，{name}已自動結束。"
    # 口說測驗的結果是多則訊息；提示放在最前面獨立一則，確保一定會 @ 到人
    texts = [notice] + summary if isinstance(summary, list) else [f"{notice}\n\n{summary}"]
    channel = bot.get_channel(key[0]) or await bot.fetch_channel(key[0])
    if isinstance(session, ExerciseSession) and session.kind == "read":
        await delete_message_id(channel, session.question_msg_id)
    for text in texts:
        for chunk in chunk_text(text):
            await channel.send(chunk)


async def session_watchdog():
    while True:
        await asyncio.sleep(15)
        now = time.monotonic()
        for key, session in list(bot.quizzes.items()):
            if now - session.last_active < SESSION_IDLE_SECONDS:
                continue
            if bot.quizzes.get(key) is not session:
                continue  # 剛好被使用者結束了
            try:
                async with bot.locks.setdefault(key, asyncio.Lock()):
                    if bot.quizzes.get(key) is session:
                        log_event("ielts", "timeout", type(session).__name__, key[1], key[0])
                        await expire_session(key, session)
            except Exception as e:
                log.exception("自動結束逾時的練習失敗")
                log_error("ielts", "expire", e, key[1], key[0])
                bot.quizzes.pop(key, None)


# ----------------- 7. 評級查看與曲線 -----------------
@bot.tree.command(name="level", description="查看聽說讀寫四項評級；也可手動設定某一項")
@app_commands.describe(skill="要手動設定的技能（留空則只查看）", band="要設定的 band（1.0~9.0）")
@app_commands.choices(
    skill=[app_commands.Choice(name=skills.SKILL_NAMES[s], value=s) for s in skills.SKILLS]
)
async def level_cmd(
    interaction: discord.Interaction,
    skill: app_commands.Choice[str] = None,
    band: app_commands.Range[float, 1.0, 9.0] = None,
):
    if (skill is None) != (band is None):
        await interaction.response.send_message("要手動設定，請同時填『技能』和『band』。", ephemeral=True)
        return
    if skill:
        before = load_rating(skill.value)
        save_rating(skill.value, skills.Rating(band, 0.5), "manual", "", before)

    ratings = {s: load_rating(s) for s in skills.SKILLS}
    lines = [f"📊 **目前評級**（預估 Overall band **{skills.overall([r.rating for r in ratings.values()]):.1f}**）"]
    for s in skills.SKILLS:
        rt = ratings[s]
        hist = [r[1] for r in skill_history_rows(s, limit=12)]
        change = week_change(s)
        change_text = f"，近 7 天 {change:+.1f}" if change is not None else "，尚無練習紀錄"
        lines.append(
            f"{SKILL_EMOJI[s]} {skills.SKILL_NAMES[s]} **{rt.rating:.1f}** ±{rt.rd:.1f} "
            f"{skills.sparkline(hist)}{change_text}"
        )
    lines.append("\n±代表不確定度：練習越多越小，評級越穩定；太久沒練習會回升。`/progress` 可看曲線圖。")
    await interaction.response.send_message("\n".join(lines))


def render_progress_chart():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    plotted = False
    for s in skills.SKILLS:
        rows = skill_history_rows(s)
        if len(rows) < 2:
            continue
        xs = [datetime.datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        ys = [r[1] for r in rows]
        rds = [r[2] for r in rows]
        ax.plot(xs, ys, marker="o", markersize=3, label=s.capitalize())
        ax.fill_between(xs, [y - d for y, d in zip(ys, rds)], [y + d for y, d in zip(ys, rds)], alpha=0.12)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.set_ylabel("IELTS band (shaded = uncertainty)")
    ax.set_title("Skill rating curve")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


@bot.tree.command(name="progress", description="畫出聽說讀寫的評級曲線圖")
async def progress_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    buf = await asyncio.to_thread(render_progress_chart)
    if buf is None:
        await interaction.followup.send("還沒有足夠的練習紀錄可以畫曲線，先做幾場練習吧！")
        return
    await interaction.followup.send(file=discord.File(buf, filename="progress.png"))


@bot.tree.command(name="stats", description="查看學習統計")
async def stats_cmd(interaction: discord.Interaction):
    me = uid()
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM training_logs WHERE user_id = ?", (me,)).fetchone()[0]
        by_cat = conn.execute(
            "SELECT category, COUNT(*), ROUND(AVG(mastery), 1) FROM training_logs WHERE user_id = ? GROUP BY category",
            (me,),
        ).fetchall()
        weak = conn.execute(
            "SELECT target_word, mastery FROM training_logs WHERE category='Vocabulary' AND user_id = ? "
            "ORDER BY mastery ASC, id DESC LIMIT 5",
            (me,),
        ).fetchall()
    if not total:
        await interaction.response.send_message("還沒有任何紀錄。")
        return
    lines = [f"📊 **總紀錄：{total} 筆**"]
    lines += [f"- {c}：{n} 筆，平均掌握度 {avg}" for c, n, avg in by_cat]
    if weak:
        lines.append("\n⚠️ **最弱單字**：" + "、".join(f"{w}({m})" for w, m in weak))
    await interaction.response.send_message("\n".join(lines))


# ----------------- 8. API 用量與效益報表 -----------------
def pct(num, den):
    return f"{num / den:.0%}" if den else "-"


def collect_usage(days=1, start=None, end=None, user_id=None):
    """統計 [start, end) 的用量；沒指定就是最近 days 天（含今天）。user_id 有給就只算這個人。"""
    since = start or (datetime.date.today() - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    until = end or "9999-12-31 00:00:00"
    who = " AND user_id = ?" if user_id else ""
    span = (since, until) + ((str(user_id),) if user_id else ())
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT feature, COUNT(*), SUM(ok), SUM(input_tokens), SUM(output_tokens), SUM(thinking_tokens),
                   AVG(latency_ms), SUM(COALESCE(units_requested, 0)), SUM(COALESCE(units_useful, 0)),
                   SUM(CASE WHEN units_consumed IS NOT NULL THEN units_useful END), SUM(units_consumed)
            FROM api_calls WHERE ts >= ? AND ts < ?""" + who + """
            GROUP BY feature ORDER BY COUNT(*) DESC
            """,
            span,
        ).fetchall()
        errors = conn.execute(
            "SELECT error_kind, COUNT(*) FROM api_calls WHERE ok = 0 AND ts >= ? AND ts < ?" + who
            + " GROUP BY error_kind",
            span,
        ).fetchall()
        events = conn.execute(
            "SELECT kind, reason, COUNT(*) FROM api_events WHERE ts >= ? AND ts < ?" + who
            + " GROUP BY kind, reason ORDER BY COUNT(*) DESC",
            span,
        ).fetchall()
        daily = conn.execute(
            "SELECT substr(ts, 1, 10), COUNT(*), SUM(input_tokens + output_tokens + thinking_tokens), "
            "SUM(1 - ok) FROM api_calls WHERE ts >= ? AND ts < ?" + who
            + " GROUP BY substr(ts, 1, 10) ORDER BY substr(ts, 1, 10) DESC LIMIT 14",
            span,
        ).fetchall()[::-1]

    stats = {}
    for f, calls, ok, itok, otok, ttok, lat, req, useful, base, cons in rows:
        stats[f] = dict(
            calls=calls, ok=ok or 0, in_tok=itok or 0, out_tok=otok or 0, think_tok=ttok or 0,
            latency=lat or 0, requested=req or 0, useful=useful or 0,
            consumed_base=base or 0, consumed=cons or 0,
        )
    today_calls = today_call_count(user_id)
    return {
        "days": days, "stats": stats, "errors": errors, "events": events, "daily": daily,
        "today_calls": today_calls, "user_id": user_id,
        "tips": usage.suggest(stats, today_calls, guard.limits.rpd),
    }


def breaker_text():
    remaining = guard.breaker_remaining()
    if remaining <= 0:
        return "正常"
    return f"⛔ 暫停中（{guard.open_reason}，約 {int(remaining) + 1} 秒後恢復）"


def usage_text(d, personal=False):
    """personal=True：一般使用者只看自己的用量，不顯示全域額度、斷路器與優化建議。"""
    stats = d["stats"]
    calls = sum(s["calls"] for s in stats.values())
    ok = sum(s["ok"] for s in stats.values())
    in_tok = sum(s["in_tok"] for s in stats.values())
    out_tok = sum(s["out_tok"] + s["think_tok"] for s in stats.values())
    lat = sum(s["latency"] * s["calls"] for s in stats.values()) / calls / 1000 if calls else 0
    lim = guard.limits
    label = "今天" if d["days"] == 1 else f"最近 {d['days']} 天"

    if personal:
        head = f"🚦 **你的 API 用量**｜今日 {d['today_calls']} 次"
    else:
        scope = f"（使用者 <@{d['user_id']}>）" if d.get("user_id") else "（全域）"
        head = (
            f"🚦 **API 用量{scope}**（{GEMINI_MODEL}）\n"
            f"今日 **{d['today_calls']}/{lim.rpd}**（{pct(d['today_calls'], lim.rpd)}）｜"
            f"近 1 分鐘 {guard.calls_last_minute()}/{lim.rpm}｜斷路器：{breaker_text()}"
        )
    lines = [
        head,
        f"{label}：{calls} 次，tokens 輸入 {in_tok:,} / 輸出 {out_tok:,}｜平均延遲 {lat:.1f}s｜"
        f"失敗 {calls - ok} 次" + (f"（{'、'.join(f'{k}×{n}' for k, n in d['errors'])}）" if d["errors"] else ""),
    ]
    if d["events"] and not personal:
        lines.append("被攔截/事件：" + "、".join(f"{k}:{r}×{n}" for k, r, n in d["events"]))
    if stats:
        lines.append("\n**各功能**（可用率 = 回覆可用的比例；使用率 = 產出的內容實際被用到的比例）")
        for f, s in stats.items():
            per_unit = (s["in_tok"] + s["out_tok"] + s["think_tok"]) / s["consumed"] if s["consumed"] else None
            lines.append(
                f"`{f}` {s['calls']} 次｜成功 {pct(s['ok'], s['calls'])}｜可用 {pct(s['useful'], s['requested'])}｜"
                f"使用 {pct(s['consumed'], s['consumed_base'])}"
                + (f"｜約 {per_unit:,.0f} tokens/項" if per_unit else "")
            )
    if d["daily"]:
        counts = [r[1] for r in d["daily"]]
        lines.append(f"\n**每日呼叫次數**（近 {len(counts)} 天）{skills.sparkline(counts)}  最近：{counts[-1]}")
    if not personal:
        lines.append("\n💡 **優化建議**\n" + "\n".join(f"- {t}" for t in d["tips"]))
    return "\n".join(lines)


def usage_lines(d):
    """週報用的 API 用量段落（標題從 ### 開始，掛在『## API 用量』底下）。"""
    lim = guard.limits
    lines = [
        f"- 模型：{GEMINI_MODEL}",
        f"- 防護上限：全域 {lim.rpm}/分鐘、每人 {lim.user_rpm}/分鐘、每日 {lim.rpd}",
        "",
        "### 各功能效益",
        "",
        "- 可用率 = 回覆中可用的項目 / 要求的項目",
        "- 使用率 = 產出的項目最後實際被使用者用到的比例（低表示過度生成）",
        "",
        "| 功能 | 次數 | 成功率 | 輸入 tokens | 輸出 tokens | 平均延遲 | 可用率 | 使用率 | tokens/被用到的項目 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for f, s in d["stats"].items():
        per_unit = (s["in_tok"] + s["out_tok"] + s["think_tok"]) / s["consumed"] if s["consumed"] else None
        lines.append(
            f"| {f} | {s['calls']} | {pct(s['ok'], s['calls'])} | {s['in_tok']:,} | "
            f"{s['out_tok'] + s['think_tok']:,} | {s['latency'] / 1000:.1f}s | "
            f"{pct(s['useful'], s['requested'])} | {pct(s['consumed'], s['consumed_base'])} | "
            f"{f'{per_unit:,.0f}' if per_unit else '-'} |"
        )
    lines += ["", "### 每日歷史", "", "| 日期 | 呼叫次數 | tokens | 失敗 |", "| --- | --- | --- | --- |"]
    lines += [f"| {day} | {n} | {tok or 0:,} | {fail or 0} |" for day, n, tok, fail in d["daily"]]
    lines += ["", "### 防護事件與錯誤", ""]
    lines += [f"- 事件 {k}：{r} × {n}" for k, r, n in d["events"]]
    lines += [f"- 錯誤 {k}：{n}" for k, n in d["errors"]]
    if not d["events"] and not d["errors"]:
        lines.append("- 無")
    lines += ["", "### 優化建議", ""] + [f"- {t}" for t in d["tips"]]
    return lines


# ----------------- 9. 每週紀錄 -----------------
def refresh_weekly(day=None, user=None):
    """重新產生某位使用者某一週（預設本週）的週報檔；失敗只記錄錯誤，不影響主流程。"""
    day = day or datetime.date.today()
    user = str(user or uid())
    try:
        week = reports.week_info(day)
        d = collect_usage(start=week["start"], end=week["end"], user_id=user)
        return reports.write_weekly(db_connect, day, RECORDS_ROOT / user, usage_lines(d), user)
    except Exception:
        log.exception("產生週報失敗")
        return None


def weekly_note():
    # 其他使用者看不到 Bot 機器上的檔案路徑，所以只提示怎麼取得
    path = refresh_weekly()
    return "\n📄 本週紀錄已更新，用 `/weekly` 可以下載。" if path else "\n⚠️ 週報產生失敗，請看終端機錯誤訊息。"


def refresh_all_weekly():
    """啟動時為每位使用者補齊所有有資料的週。"""
    for (user_id,) in accounts_user_ids():
        for monday in reports.weeks_with_data(db_connect, user_id):
            refresh_weekly(monday, user_id)


def accounts_user_ids():
    with db_connect() as conn:
        return conn.execute("SELECT user_id FROM users").fetchall()


def recover_abandoned_sessions():
    """Bot 重啟時，把上次沒正常結束的練習收尾（週報會在之後一併重建）。"""
    with db_connect() as conn:
        quiz_ids = [r[0] for r in conn.execute("SELECT id FROM quiz_sessions WHERE status = 'active'")]
        ex_ids = [r[0] for r in conn.execute("SELECT id FROM exercise_sessions WHERE status = 'active'")]
        sp_ids = [r[0] for r in conn.execute("SELECT id FROM speaking_sessions WHERE status = 'active'")]
    for sid in quiz_ids:
        close_quiz_session_row(sid, "abandoned")
    for sid in ex_ids:
        close_exercise_row(sid, "abandoned")
    for sid in sp_ids:
        close_speaking_row(sid, "abandoned")


@bot.tree.command(name="usage", description="查看 Gemini 用量（管理員看全域與各功能效益；一般使用者看自己的）")
@app_commands.describe(days="統計最近幾天（1~30，預設 1 = 只看今天）", member="（管理員）只看某位使用者")
async def usage_cmd(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 1, 30] = 1,
    member: discord.User = None,
):
    admin = is_admin(interaction.user.id)
    if member is not None and not admin:
        await interaction.response.send_message("只有管理員可以查看別人的用量。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)  # 用量屬於個人資訊，只有本人看得到
    if admin:
        d = collect_usage(days, user_id=member.id if member else None)
        text = usage_text(d)
    else:
        d = collect_usage(days, user_id=interaction.user.id)
        text = usage_text(d, personal=True)
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="weekly", description="產生並下載你本週的學習週報檔")
async def weekly_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)  # 週報含你的作答內容，只有本人看得到
    path = refresh_weekly()
    if path is None:
        await interaction.followup.send("⚠️ 週報產生失敗，請看終端機錯誤訊息。", ephemeral=True)
        return
    await interaction.followup.send(f"📄 {path.name}", file=discord.File(path), ephemeral=True)


def prepare():
    """啟動前的準備（單獨執行雅思寶寶，或由 run_all.py 同時啟動兩隻 bot 時都會呼叫）。"""
    for leftover in AUDIO_DIR.glob("*"):  # 重啟後進行中的練習都已失效，清掉殘留的暫存音檔
        leftover.unlink(missing_ok=True)
    init_db()
    prune_logs()
    recover_abandoned_sessions()
    refresh_all_weekly()


if __name__ == "__main__":
    prepare()
    bot.run(DISCORD_TOKEN)
