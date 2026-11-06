"""Gemini 用量防護與效益分析（純邏輯，不碰 Discord 與資料庫）。

防護（Guard，所有 Gemini 呼叫都要先過這一關）：
- 全域每分鐘上限：快滿時短暫等待排隊，等太久才拒絕。
- 個別使用者每分鐘上限：避免有人狂按。
- 每日上限：低於免費額度，留一點餘裕。
- 重複請求、超大請求：直接攔下。
- 斷路器：連續失敗或被 429 擋下時暫停呼叫（指數退避），避免越撞越多。

分析：suggest() 依各功能的效益統計給出優化建議；detect_anomaly() 偵測當天用量暴增。
"""
import asyncio
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass

# 目前發出指令的使用者；由 CommandTree.interaction_check 設定，ask_gemini 讀取
current_user = ContextVar("current_user", default=None)


class UsageLimitError(Exception):
    """被用量防護攔下。訊息可以直接顯示給使用者。"""


@dataclass
class Limits:
    rpm: int = 12  # 全域每分鐘（Flash Lite 免費額度 15）
    rpd: int = 450  # 每日（Flash Lite 免費額度 500）
    user_rpm: int = 8  # 單一使用者每分鐘（一場 50 題測驗會分批呼叫約 5 次，要留得下）
    max_prompt_chars: int = 30000
    duplicate_window: float = 20.0  # 同一人這麼多秒內送出完全相同的請求會被擋
    max_wait: float = 8.0  # 全域每分鐘額滿時，最多願意等多久
    error_streak_trip: int = 3  # 連續失敗幾次就暫停
    base_backoff: float = 30.0
    max_backoff: float = 900.0


def classify_error(exc):
    """把例外歸類，供統計與斷路器使用。"""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if code in (400, 401, 403, 404):  # 有明確的錯誤碼就以它為準，不要被訊息裡的字樣誤導
        return "client"
    if code == 429 or "resource_exhausted" in msg:
        if "perday" in msg or "per day" in msg or "daily" in msg:
            return "quota_daily"
        return "rate_limit"
    if code in (500, 502, 503, 504) or "unavailable" in msg or "deadline" in msg:
        return "server"
    if code in (400, 401, 403, 404):
        return "client"
    return "other"


class Guard:
    def __init__(self, limits=None):
        self.limits = limits or Limits()
        self.times = deque()  # 全域呼叫時間戳
        self.user_times = defaultdict(deque)
        self.recent = {}  # (user, prompt_key) -> 時間戳
        self.open_until = 0.0
        self.open_reason = ""
        self.error_streak = 0
        self.trip_count = 0

    # ---- 查詢 ----
    def breaker_remaining(self):
        return max(0.0, self.open_until - time.monotonic())

    def calls_last_minute(self):
        self._prune(time.monotonic())
        return len(self.times)

    def _prune(self, now):
        while self.times and now - self.times[0] >= 60:
            self.times.popleft()
        for q in self.user_times.values():
            while q and now - q[0] >= 60:
                q.popleft()
        window = self.limits.duplicate_window
        for k in [k for k, t in self.recent.items() if now - t >= window]:
            del self.recent[k]

    # ---- 呼叫前檢查 ----
    async def acquire(self, user_id, prompt_key, prompt_chars, today_count):
        """通過就登記這次呼叫；不通過丟 UsageLimitError（reason 在 .reason）。"""
        lim = self.limits
        while True:
            now = time.monotonic()
            self._prune(now)
            if now < self.open_until:
                raise self._blocked(
                    "breaker",
                    f"Gemini 呼叫暫停保護中（{self.open_reason}），約 {int(self.open_until - now) + 1} 秒後自動恢復。",
                )
            if prompt_chars > lim.max_prompt_chars:
                raise self._blocked("oversize", f"請求內容過大（{prompt_chars} 字），已攔下以免浪費額度。")
            if today_count >= lim.rpd:
                raise self._blocked("daily_cap", f"今天的 Gemini 呼叫已達上限（{lim.rpd} 次），明天再來吧。")
            last = self.recent.get((user_id, prompt_key))
            if last is not None:
                raise self._blocked("duplicate", "剛剛才送過一模一樣的請求，已略過以節省額度。")
            user_q = self.user_times[user_id]
            if len(user_q) >= lim.user_rpm:
                wait = int(60 - (now - user_q[0])) + 1
                raise self._blocked("user_rate", f"操作太快了（每人每分鐘最多 {lim.user_rpm} 次 AI 請求），請等 {wait} 秒。")
            if len(self.times) >= lim.rpm:
                wait = 60 - (now - self.times[0])
                if wait > lim.max_wait:
                    raise self._blocked("global_rate", f"現在 AI 請求太多，請約 {int(wait) + 1} 秒後再試。")
                await asyncio.sleep(wait + 0.05)  # 短暫排隊，醒來後重新檢查
                continue
            self.times.append(now)
            user_q.append(now)
            self.recent[(user_id, prompt_key)] = now
            return

    @staticmethod
    def _blocked(reason, message):
        err = UsageLimitError(message)
        err.reason = reason
        return err

    # ---- 呼叫後回報 ----
    def record_result(self, ok, error_kind=None):
        """回報結果；若因此開啟斷路器，回傳原因字串（供記錄事件），否則回傳 None。"""
        if ok:
            self.error_streak = 0
            if self.breaker_remaining() == 0:
                self.trip_count = 0
            return None
        lim = self.limits
        if error_kind == "quota_daily":
            return self._open(3600.0, "每日額度用完")
        if error_kind == "rate_limit":
            return self._open(self._backoff(), "被 Gemini 限流 (429)")
        self.error_streak += 1
        if self.error_streak >= lim.error_streak_trip:
            self.error_streak = 0
            return self._open(self._backoff(), f"連續失敗（{error_kind}）")
        return None

    def _backoff(self):
        seconds = min(self.limits.max_backoff, self.limits.base_backoff * 2 ** self.trip_count)
        self.trip_count += 1
        return seconds

    def _open(self, seconds, reason):
        self.open_until = time.monotonic() + seconds
        self.open_reason = reason
        return f"{reason}，暫停 {int(seconds)} 秒"


def detect_anomaly(today_calls, previous_days):
    """當天用量遠高於過去平均時回傳說明，否則 None。previous_days: 過去幾天各自的呼叫次數。"""
    days = [d for d in previous_days if d > 0]
    if not days:
        return f"今天已呼叫 {today_calls} 次（沒有歷史可比較）" if today_calls >= 100 else None
    mean = sum(days) / len(days)
    if today_calls >= 50 and today_calls >= 3 * mean:
        return f"今天已呼叫 {today_calls} 次，是過去平均（{mean:.0f} 次/天）的 {today_calls / mean:.1f} 倍"
    return None


def suggest(stats, today_calls, rpd):
    """依各功能的效益統計給出優化建議。

    stats: {feature: {calls, ok, requested, useful, consumed, consumed_base, in_tok, out_tok}}
    """
    tips = []
    for feature, s in sorted(stats.items()):
        calls = s["calls"]
        if calls < 3:
            continue
        if s["consumed_base"] and s["consumed_base"] >= 5:
            rate = s["consumed"] / s["consumed_base"]
            if rate < 0.6:
                tips.append(
                    f"`{feature}`：生成的內容只有 {rate:.0%} 被實際用到。"
                    "可以改成先產一小批，作答快到尾聲再補（逐批生成），或降低預設題數。"
                )
        if s["requested"] and s["useful"] / s["requested"] < 0.85:
            tips.append(
                f"`{feature}`：可用率只有 {s['useful'] / s['requested']:.0%}，"
                "常有重複或格式錯誤。可精簡 prompt、明確給 JSON 範例，或改用 response schema。"
            )
        if calls >= 5 and 1 - s["ok"] / calls > 0.1:
            tips.append(f"`{feature}`：失敗率 {1 - s['ok'] / calls:.0%}，請檢查額度或模型名稱。")
        if s["in_tok"] / calls > 3000:
            tips.append(f"`{feature}`：平均每次輸入 {s['in_tok'] // calls} tokens，偏長，可考慮縮短提示或排除已出現清單。")
    if rpd and today_calls >= 0.8 * rpd:
        tips.append(f"今日用量已達上限的 {today_calls / rpd:.0%}，建議暫停高耗用功能（如 50 題測驗）。")
    return tips or ["目前沒有明顯可優化的地方。"]
