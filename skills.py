"""聽說讀寫的評級模型（純計算，不碰資料庫與 Discord）。

每個技能用 (rating, rd) 表示：
- rating：能力估計，單位是 IELTS band。
- rd：不確定度（band）。資料少或太久沒練習就大，每次更新的幅度也跟著大；
  練習越多 rd 越小，評級越穩定，不會被單次表現大幅拉動。

兩種更新方式：
- update_from_items：對/錯題（單字測驗、閱讀、聽力）。類似 Elo，
  答對高於自己程度的題目加得多，答錯低於自己程度的題目扣得多。
- update_from_band：Gemini 對口說/寫作給的 band 估分。類似卡爾曼濾波，
  依 rd 與估分的雜訊決定要相信這次估分多少。
"""
import math
from dataclasses import dataclass

SKILLS = ("listening", "speaking", "reading", "writing")
SKILL_NAMES = {"listening": "聽力", "speaking": "口說", "reading": "閱讀", "writing": "寫作"}

RATING_MIN, RATING_MAX = 1.0, 9.0
RD_MIN, RD_MAX = 0.2, 1.0
IDLE_VAR_PER_DAY = 0.01  # 每天沒練習，不確定度的變異數增加多少
SCALE = 0.7  # 預期答對機率的 logistic 尺度：程度差 0.7 band 約 73% 答對
K_MIN, K_MAX = 0.03, 0.15  # 每題最大調整幅度（rd 最小 / 最大時）
RD_DECAY_PER_ITEM = 0.96  # 每答一題，不確定度縮小一點
SESSION_CAP = 0.4  # 單次練習對單一技能最多調整多少 band


@dataclass
class Rating:
    rating: float
    rd: float = RD_MAX


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def inflate(rd, idle_days):
    """太久沒練習，不確定度慢慢回升。"""
    return min(RD_MAX, math.sqrt(rd ** 2 + IDLE_VAR_PER_DAY * max(0.0, idle_days)))


def expected(rating, difficulty):
    """程度為 rating 的人答對難度 difficulty 的題目的機率。"""
    return 1.0 / (1.0 + math.exp(-(rating - difficulty) / SCALE))


def _cap(start, new):
    return clamp(start + clamp(new - start, -SESSION_CAP, SESSION_CAP), RATING_MIN, RATING_MAX)


def update_from_items(rt, items, weight=1.0):
    """items: [(得分 0~1, 題目難度 band)]；weight 讓一場練習只部分計入該技能。"""
    r, rd = rt.rating, rt.rd
    for score, difficulty in items:
        k = K_MIN + (K_MAX - K_MIN) * (rd - RD_MIN) / (RD_MAX - RD_MIN)
        r += weight * k * (score - expected(r, difficulty))
        rd = max(RD_MIN, rd * RD_DECAY_PER_ITEM ** weight)
    return Rating(_cap(rt.rating, r), rd)


def update_from_band(rt, band, sigma):
    """band：Gemini 估的分數；sigma：這種估分的雜訊（單句比錄音更不可靠，值更大）。"""
    gain = rt.rd ** 2 / (rt.rd ** 2 + sigma ** 2)
    r = rt.rating + gain * (band - rt.rating)
    rd = max(RD_MIN, math.sqrt(1.0 - gain) * rt.rd)
    return Rating(_cap(rt.rating, r), rd)


def overall(ratings):
    """四項平均，依雅思規則進位到 0.5。"""
    mean = sum(ratings) / len(ratings)
    return math.floor(mean * 2 + 0.5) / 2


def sparkline(values):
    ticks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return ticks[3] * len(values)
    return "".join(ticks[round((v - lo) / (hi - lo) * 7)] for v in values)
