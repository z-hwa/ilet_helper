"""口說「對話追問」的純邏輯（不碰 Discord、資料庫、網路）。

做法：開場時 Gemini 一次產出一棵「對話樹」——開場題＋每種可能回答類型（關鍵字）對應的追問，還有任何回答都適用的通用追問。
之後每一輪：本地語音辨識（asr.py）把使用者的回答轉成文字 -> 用關鍵字比對走哪個分支 -> 不用再叫 Gemini。
只有回答很長又完全沒命中任何分支時，才允許叫 Gemini 即時接話（每場有上限）。

樹的格式（共 3 輪：開場題、第 2 輪追問、第 3 輪追問）：
{"topic", "opening": 題目,
 "branches": [{"label", "keywords": [...], "question": 第 2 輪,
               "branches": [{"label", "keywords": [...], "question": 第 3 輪}, ...],
               "generic": 這個分支下，第 3 輪的通用追問}, ...],
 "generic": 第 2 輪的通用追問, "generic_next": 走通用追問後，第 3 輪的通用追問}
"""
import random
import re

import jsonutil
import speaking

ROUNDS = 3
MIN_SCORE = 2  # 分支至少要命中幾個辨識字（實測：2 分幾乎不會接錯，沒把握的改用通用追問；1 分約 7% 接錯）
MIN_MARGIN = 1  # 贏過第二名至少幾分
MIN_WORDS_FOR_LIVE = 6  # 沒命中分支、字數不少才值得叫 Gemini 即時接話；太短的回答（Yes / I don't know）用通用追問就夠
DEFAULT_GENERIC = ["Can you tell me a bit more about that?", "Why do you think that is?"]

SCHEMA = (
    '輸出 JSON 物件：{"topic": 英文主題簡稱, "opening": 開場題（一句英文口說題）, '
    '"branches": [3 個物件，代表學生回答開場題時最常見的 3 種不同方向（用「聊的內容」來分，例如不同的活動、原因、對象、情況；不要用喜歡/不喜歡這種立場來分，因為「Not really, it is boring」這類話很難靠關鍵字分辨），每個：'
    '{"label": 英文短標籤, "keywords": [15~20 個學生在這個方向最可能說出的小寫英文「單字」（一個字一項，不要片語），'
    '要含同義詞、口語說法，以及相關的常見名詞、動詞、形容詞（例如談網購就要有 online、internet、amazon、cheap、deliver、click 這類），'
    '也要有學生可能用的字形變化；不要放 the/is/and/like/good/thing 這種太泛的字，也盡量不要和其他方向共用同一個字], "question": 考官針對這個方向的第 2 輪追問（一句英文，要接得上學生剛說的方向）, '
    '"branches": [2 個物件，代表學生回答這個追問時的 2 種方向，每個：{"label", "keywords": [10~15 個單字], '
    '"question": 第 3 輪追問}], "generic": 不管學生怎麼回答這個追問都適用的第 3 輪追問}], '
    '"generic": 不管學生怎麼回答開場題都適用的第 2 輪追問（例如請他舉例或說原因）, '
    '"generic_next": 走通用追問之後，不管怎麼回答都適用的第 3 輪追問}。'
    "每題都是考官口頭會問的自然英文，一句話，不要編號、不要答案。"
)


def build_tree_prompt(level, topic="", avoid=()):
    topic_line = (f"主題請以「{topic}」為主。" if topic else
                  f"主題請以「{random.choice(speaking.PART1_TOPICS)}」這個日常話題為出發點（要貼近生活、容易展開）。")
    avoid_line = ("\n以前出過的題目，這次不要重複：\n- " + "\n- ".join(list(avoid)[:30])) if avoid else ""
    return (
        f"你是雅思口說考官，正在設計一段 3 輪的口說對話練習，學習者口說程度約 band {level:.1f}，"
        f"問題難度約 band {level:.1f}~{level + 1.0:.1f}。{topic_line}{avoid_line}\n"
        "對話流程：考官問開場題 -> 學生回答 -> 考官依學生回答的方向追問 -> 學生回答 -> 再追問一次。"
        "因為要事先備好各種可能的方向，請盡量讓三個方向彼此差異明顯、涵蓋大多數學生會說的內容。\n"
        "開場題請用 What / Which / How / Tell me about 這類開放式問句，不要出 Yes/No 問題或 Do you like… 這種只能回答立場的題目。"
        "每個追問都要在學生是正面、負面或中立的情況下都說得通（不要預設學生喜歡或討厭），也不要假設學生沒說過的細節。\n" + SCHEMA
    )


# ---------------- 解析與驗證 ----------------
def _text(v):
    return " ".join(str(v or "").split())


def _keywords(items):
    out = []
    for k in items or []:
        k = _text(k).lower().strip(" .,!?\"'")
        if k and k not in out:
            out.append(k)
    return out


def _leaf(raw):
    if not isinstance(raw, dict):
        return None
    question, keywords = _text(raw.get("question")), _keywords(raw.get("keywords"))
    if not question or not keywords:
        return None
    return {"label": _text(raw.get("label")) or keywords[0], "keywords": keywords, "question": question}


def parse_tree(text):
    """解析並檢查對話樹；至少要有開場題與 2 個有效的第 2 輪分支，否則丟 ValueError。缺的通用追問用預設值補。"""
    data = jsonutil.loads_lenient(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("格式錯誤")
    opening = _text(data.get("opening"))
    if not opening:
        raise ValueError("沒有開場題")
    branches = []
    for raw in data.get("branches") or []:
        node = _leaf(raw)
        if not node:
            continue
        node["branches"] = [leaf for leaf in (_leaf(b) for b in raw.get("branches") or []) if leaf]
        node["generic"] = _text(raw.get("generic")) or DEFAULT_GENERIC[1]
        branches.append(node)
    if len(branches) < 2:
        raise ValueError(f"可用的分支不足（{len(branches)} 個）")
    return {
        "topic": _text(data.get("topic")) or "Conversation",
        "opening": opening,
        "branches": branches,
        "generic": _text(data.get("generic")) or DEFAULT_GENERIC[0],
        "generic_next": _text(data.get("generic_next")) or DEFAULT_GENERIC[1],
    }


# ---------------- 關鍵字比對（本地，不用 API）----------------
def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def normalize(text):
    return [_stem(w) for w in re.findall(r"[a-z']+", str(text).lower().replace("’", "'"))]


STOPWORDS = set(
    "a an the and or but so to of in on at for with from by as is are am was were be been being it its it's i i'm i've "
    "you your he she they them we our my me his her this that these those there here do does did done have has had not no "
    "yes yeah well very really just also too quite much many more most some any can could would should will may might "
    "like good bad thing things lot lots kind sort way ways time times one get got make made go going think know say said "
    "what which who when where why how if then than because about into out up down over after before again always never "
    "usually sometimes often maybe actually basically honestly mean".split()
)


def content_words(text):
    """英文句子 -> 有意義的詞幹集合（去掉 the/is/very 這種太泛的字）。"""
    return {w for w in normalize(text) if w not in STOPWORDS and len(w) > 2}


def branch_words(branches):
    """每個分支的辨識字：關鍵字拆成單字後，扣掉其他分支也有的字（共用字沒有辨識力）。"""
    own = [set().union(*(content_words(k) for k in b["keywords"])) if b["keywords"] else set() for b in branches]
    result = []
    for i, words in enumerate(own):
        others = set().union(*(w for j, w in enumerate(own) if j != i)) if len(own) > 1 else set()
        result.append((words - others) or words)
    return result


def keyword_hits(transcript, keywords):
    """transcript 的有意義單字裡，命中這組關鍵字幾個（片語整句出現再多算 1）。"""
    spoken = content_words(transcript)
    words = set().union(*(content_words(k) for k in keywords)) if keywords else set()
    score = len(spoken & words)
    joined = " " + " ".join(normalize(transcript)) + " "
    for k in keywords:
        parts = normalize(k)
        if len(parts) > 1 and (" " + " ".join(parts) + " ") in joined:
            score += 1
    return score


def best_branch(transcript, branches):
    """回傳命中最多辨識字的分支。要有明確的贏家才採用：至少命中 MIN_SCORE 個，且比第二名多 MIN_MARGIN 分；
    平手或分數太低寧可回傳 None（改用通用追問，不花 API），也不要接錯方向。"""
    spoken = content_words(transcript)
    scored = sorted(((len(spoken & words), -i, b) for i, (b, words) in enumerate(zip(branches, branch_words(branches)))),
                    key=lambda t: (t[0], t[1]), reverse=True)
    if not scored or scored[0][0] < MIN_SCORE:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None
    return scored[0][2]


def word_count(text):
    return len(re.findall(r"[A-Za-z']+", str(text)))


# ---------------- 對話進行 ----------------
class Dialogue:
    """一場對話的狀態：目前在哪個節點、已經問到第幾輪。"""

    def __init__(self, tree, max_live=1):
        self.tree = tree
        self.round = 1  # 目前考官問的是第幾輪
        self.node = tree  # 目前這題對應的節點（有 branches / generic）
        self.next_generic = tree["generic"]  # 沒命中分支時，下一輪用的通用追問
        self.max_live = max_live
        self.live_used = 0
        self.history = []  # [{"round", "question", "transcript", "how", "label"}]

    @property
    def opening(self):
        return self.tree["opening"]

    @property
    def finished(self):
        return self.round >= ROUNDS

    def decide(self, transcript):
        """使用者答完目前這題後，決定下一題。回傳 {"question", "how": "branch"|"generic"|"need_live", "label"}。
        need_live 時呼叫端要自己問 Gemini，再用 accept_live 接上。已經是最後一輪就回傳 None。"""
        if self.finished:
            return None
        branches = self.node.get("branches") or []
        hit = best_branch(transcript, branches)
        if hit:
            return {"question": hit["question"], "how": "branch", "label": hit["label"], "node": hit}
        if word_count(transcript) >= MIN_WORDS_FOR_LIVE and self.live_used < self.max_live:
            return {"question": None, "how": "need_live", "label": ""}
        return {"question": self.next_generic, "how": "generic", "label": "", "node": None}

    def advance(self, decision, question=None):
        """採用 decision（need_live 要帶 Gemini 給的 question）。回傳實際的下一題文字。"""
        how = decision["how"]
        if how == "need_live":
            self.live_used += 1
            question = question or self.next_generic
            self.node = {"branches": [], "generic": self.tree["generic_next"]}
            self.next_generic = self.tree["generic_next"]
        elif how == "branch":
            question = decision["question"]
            self.node = decision["node"]
            self.next_generic = decision["node"].get("generic") or self.tree["generic_next"]
        else:
            question = decision["question"]
            self.node = {"branches": [], "generic": self.tree["generic_next"]}
            self.next_generic = self.tree["generic_next"]
        self.round += 1
        return question


# ---------------- 即時接話（沒命中分支時才用）----------------
def build_live_prompt(topic, history, level):
    """history 的最後一筆是學生剛答完、還沒有追問的那題。"""
    lines = "\n".join(f"考官：{h['question']}\n學生：{h.get('transcript') or '（沒有內容）'}" for h in history)
    return (
        f"你是雅思口說考官，目前的對話主題是 {topic}，學生口說程度約 band {level:.1f}。以下是目前為止的對話"
        f"（學生的話是語音辨識的結果，可能有些小錯字）：\n{lines}\n"
        '請接著問「一個」自然的英文追問，要針對學生剛說的內容。輸出 JSON：{"question": 一句英文追問}'
    )


def parse_live(text):
    data = jsonutil.loads_lenient(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    question = _text(data.get("question")) if isinstance(data, dict) else ""
    if not question:
        raise ValueError("沒有追問")
    return question
