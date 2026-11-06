"""帳號：以 Discord 使用者 ID 區分每個人的紀錄與評級（不碰 Discord 與 Gemini）。

Discord 帳號本身已經通過驗證，所以『登入』= 第一次使用時註冊：填四項基礎分數，之後所有資料都綁在這個 ID 上。
"""
import datetime
from contextlib import closing

PER_USER_TABLES = ("training_logs", "exercise_sessions", "skill_history", "quiz_sessions", "speaking_sessions")


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_band(text):
    """使用者輸入的 band → 浮點數，四捨五入到 0.5；格式不對或超出 1~9 時丟 ValueError。"""
    value = float(str(text).strip().replace(",", "."))
    if not 1.0 <= value <= 9.0:
        raise ValueError("band 必須在 1.0 到 9.0 之間")
    return round(value * 2) / 2


def create_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            created_at TEXT,
            last_seen TEXT,
            is_admin INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            base_listening REAL,
            base_speaking REAL,
            base_reading REAL,
            base_writing REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_ratings (
            user_id TEXT,
            skill TEXT,
            rating REAL,
            rd REAL,
            updated_at TEXT,
            PRIMARY KEY (user_id, skill)
        )
        """
    )


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _derive_owner(conn):
    """舊版單人資料庫沒有使用者欄位時，從既有紀錄裡出現最多次的 Discord ID 推斷擁有者。"""
    row = conn.execute(
        """
        SELECT user_id FROM (
            SELECT user_id FROM quiz_sessions WHERE user_id IS NOT NULL
            UNION ALL SELECT user_id FROM speaking_sessions WHERE user_id IS NOT NULL
            UNION ALL SELECT user_id FROM api_calls WHERE user_id IS NOT NULL
        ) GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1
        """
    ).fetchone()
    return row[0] if row else None


def migrate(conn, owner_id, default_level):
    """單人資料庫 → 多人：把既有紀錄與評級全部歸給擁有者，並設為管理員。可重複執行。

    default_level：擁有者的基礎分數（舊版全域程度；沒有就用預設）。
    """
    create_tables(conn)
    for table in PER_USER_TABLES:
        if "user_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")

    # 舊版 skill_ratings 以 skill 當主鍵（沒有 user_id）：重建成 (user_id, skill)
    legacy_ratings = []
    if "user_id" not in _columns(conn, "skill_ratings"):
        legacy_ratings = conn.execute("SELECT skill, rating, rd, updated_at FROM skill_ratings").fetchall()
        conn.execute("DROP TABLE skill_ratings")
        create_tables(conn)

    owner_id = str(owner_id) if owner_id else _derive_owner(conn)
    if not owner_id:
        return None

    for skill, rating, rd, updated in legacy_ratings:
        conn.execute(
            "INSERT OR REPLACE INTO skill_ratings (user_id, skill, rating, rd, updated_at) VALUES (?, ?, ?, ?, ?)",
            (owner_id, skill, rating, rd, updated),
        )
    for table in PER_USER_TABLES:
        conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner_id,))

    ts = now_str()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, created_at, last_seen, is_admin, "
        "base_listening, base_speaking, base_reading, base_writing) VALUES (?, '（管理員）', ?, ?, 1, ?, ?, ?, ?)",
        (owner_id, ts, ts, default_level, default_level, default_level, default_level),
    )
    conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (owner_id,))
    return owner_id


# ---- 查詢與註冊（connect 是回傳新連線的函式，方便測試時換資料庫）----
def get_user(connect, user_id):
    keys = ("user_id", "name", "created_at", "is_admin", "blocked",
            "base_listening", "base_speaking", "base_reading", "base_writing")
    with closing(connect()) as conn:
        row = conn.execute(f"SELECT {', '.join(keys)} FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    return dict(zip(keys, row)) if row else None


def register_user(connect, user_id, name, baselines):
    """建立使用者；沒有任何管理員時，第一個註冊的人成為管理員。回傳 user dict。"""
    with closing(connect()) as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone() is not None
        ts = now_str()
        conn.execute(
            "INSERT INTO users (user_id, name, created_at, last_seen, is_admin, base_listening, base_speaking, "
            "base_reading, base_writing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(user_id), name, ts, ts, 0 if has_admin else 1,
             baselines["listening"], baselines["speaking"], baselines["reading"], baselines["writing"]),
        )
        conn.commit()
    return get_user(connect, user_id)


def touch_user(connect, user_id, name):
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE users SET last_seen = ?, name = ? WHERE user_id = ?", (now_str(), name, str(user_id))
        )
        conn.commit()


def set_blocked(connect, user_id, blocked):
    with closing(connect()) as conn:
        cur = conn.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (int(blocked), str(user_id)))
        conn.commit()
        return cur.rowcount > 0


def list_users(connect):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT user_id, name, created_at, last_seen, is_admin, blocked FROM users ORDER BY created_at"
        ).fetchall()


def base_level(connect, user_id, skill, default):
    """這位使用者註冊時填的某項基礎分數；沒註冊就用預設值。"""
    user = get_user(connect, user_id)
    value = user.get(f"base_{skill}") if user else None
    return float(value) if value else float(default)
