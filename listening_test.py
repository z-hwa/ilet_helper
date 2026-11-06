"""雅思聽力模擬測驗（4 個 Section、每段 10 題）的純邏輯：出題 prompt、解析驗證、作答解析、判分、換算 band、顯示格式。
不碰 Discord、資料庫、網路。

一個 Section = 一段只播一次的錄音（Gemini 寫腳本、本機 Kokoro 配音）＋ 10 題。題型：
  blank  填空（表格/筆記/句子完成，限字數）；答案必須真的出現在腳本裡，否則丟掉
  mcq    選擇題 A~C（或 A~D）
  match  配對題：共用一組選項 A~E，每題選一個
"""
import re

import exercises
import jsonutil

SECTIONS = {
    1: {
        "name": "Section 1",
        "setting": "日常社交情境的兩人對話（例如訂位、報名課程、租屋、詢問行程），speaker A 與 B",
        "types": "10 題全部是 blank：像一張表格或表單（姓名、電話、日期、金額、地址等），"
                 "其中至少 1 題是需要聽拼字的姓名或郵遞區號（腳本裡用 B-R-O-W-N 這種逐字母寫法）",
        "offset": -0.5,
    },
    2: {
        "name": "Section 2",
        "setting": "日常社交情境的單人獨白（例如導覽、設施介紹、活動說明），全部 speaker A",
        "types": "第 1~5 題 mcq（3 個選項 A~C），第 6~10 題 match（把 5 個項目配對到共用的 A~E 選項，例如地點或特色）",
        "offset": 0.0,
    },
    3: {
        "name": "Section 3",
        "setting": "教育情境的 2~4 人討論（例如兩位學生與一位導師討論報告、研究或課程），用 speaker A、B、C（需要時 D），"
                   "每個人立場或角色不同，要有不同意見與轉折；同一個人的 speaker 字母全程固定",
        "types": "第 1~5 題 mcq（3 個選項 A~C），第 6~10 題 blank（句子完成，限一至兩個字）",
        "offset": 0.5,
    },
    4: {
        "name": "Section 4",
        "setting": "學術講座的單人獨白（大學課程的一段講課），全部 speaker A，內容有學術詞彙與條理",
        "types": "10 題全部是 blank（講課筆記完成，依順序，限一至兩個字加或數字）",
        "offset": 1.0,
    },
}
QUESTIONS_PER_SECTION = 10
MIN_VALID = 8  # 通過驗證的題目少於這個數就重出
MAX_WORDS_NOTE = "NO MORE THAN TWO WORDS AND/OR A NUMBER"

# 學術組聽力：40 題答對題數 -> band（官方換算表的常見版本）
BAND_TABLE = [(39, 9.0), (37, 8.5), (35, 8.0), (32, 7.5), (30, 7.0), (26, 6.5), (23, 6.0), (18, 5.5),
              (16, 5.0), (13, 4.5), (10, 4.0), (8, 3.5), (6, 3.0), (4, 2.5)]


def raw_to_band(correct, total):
    """答對 correct/total 題，換算成「40 題版本」的 band（不是 40 題就按比例換算，只是估計）。"""
    if total <= 0:
        return 0.0
    raw40 = round(correct / total * 40)
    for need, band in BAND_TABLE:
        if raw40 >= need:
            return band
    return 2.0


def difficulty(section, level):
    """Section 越後面越難；題目的難度 band 拿來更新評級。"""
    return max(3.0, min(9.0, level + SECTIONS[section]["offset"]))


# ---------------- 出題 ----------------
def build_prompt(section, level, topic="", avoid=()):
    cfg = SECTIONS[section]
    topic_line = f"主題：{topic}。" if topic else "主題請自選，要貼近真實的雅思聽力情境。"
    avoid_line = ("\n以前出過的主題，這次不要重複：\n- " + "\n- ".join(list(avoid)[:20])) if avoid else ""
    return (
        f"你是雅思聽力出題老師。請設計雅思聽力 {cfg['name']} 的一段錄音腳本與 {QUESTIONS_PER_SECTION} 題。"
        f"學習者程度約 band {level:.1f}，這一段難度約 band {difficulty(section, level):.1f}。{topic_line}{avoid_line}\n"
        f"情境：{cfg['setting']}。\n題型：{cfg['types']}。\n"
        "腳本規則：\n"
        "- **至少 600 字**（約 650~750 字，錄音要有 4 分鐘左右，不要寫短），口語自然，用 segments 陣列（每個 segment 是一個人連續講的一小段話，單人獨白就切成多段）。\n"
        "- 不要括號舞台指示；電話號碼、郵遞區號用數字逐位寫（例如 0 7 4 1 2），金額與日期照一般寫法；需要拼字的姓名先講出整個字再逐字母拼（例如 Brown, B-R-O-W-N）。\n"
        "- 要有雅思聽力常見的『陷阱』：講者先說一個答案再更正、同時提到幾個選項只有一個正確。\n"
        "- 題目依腳本出現的順序排列，每題的答案都要能明確從腳本聽到。\n"
        + exercises.NO_FABRICATION + "\n"
        '輸出 JSON 物件：{"title": 這段錄音的標題, "context": 給考生看的一句英文情境說明（例如 You will hear a conversation between…）, '
        '"speakers": 物件，每個出現在 segments 的說話者代號對應一句英文人物說明（名字加身分，例如 {"A": "Sarah, a customer", "B": "Tom, the receptionist"}）, '
        '"segments": [{"speaker": "A"~"D", "text": 一小段話}], '
        '"choices": 只有 match 題才要：5 個選項字串（不要加 A. 前綴），沒有 match 題就給空陣列, '
        '"questions": 陣列，共 10 題，每題：{"type": "blank" | "mcq" | "match", '
        '"question": 題目（英文）；blank 題要用 ____ 代表空格，例如「Customer name: ____」, '
        '"options": 僅 mcq：3 個選項字串（不要加 A. 前綴）, '
        '"answer": blank 填答案原文（一至兩個英文單字或數字，必須和腳本裡一字不差）；mcq 填 "A"~"C"；match 填 "A"~"E", '
        '"alternatives": 僅 blank：其他也算對的寫法（陣列，例如英美拼法、數字寫成英文；沒有就給空陣列）, '
        '"evidence": 腳本裡支持答案的原句（必須原樣引用 1~2 句）, '
        '"explanation": 繁體中文，一兩句說明為什麼是這個答案（可點出陷阱）}。'
    )


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace("’", "'").replace("-", " ")).strip()


NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}


def _canon(text):
    """比對用的正規化：小寫、去標點與連字號、小數字寫成阿拉伯數字，並去掉開頭的 a/an/the。"""
    words = [NUMBER_WORDS.get(w, w) for w in _norm(text).split()]
    while len(words) > 1 and words[0] in ("a", "an", "the"):
        words.pop(0)
    if len(words) > 1 and all(w.isdigit() for w in words):
        return "".join(words)  # 電話、郵遞區號：0 7 4 1 2 與 07412 視為同一個答案
    return " ".join(words)


def in_script(answer, spoken):
    """答案是否出現在腳本裡（整字比對；逐字母拼字、逐位數字的情況改用去空白後比對）。"""
    a = _canon(answer)
    if not a:
        return False
    if f" {a} " in f" {spoken} ":
        return True
    compact = a.replace(" ", "")
    return (len(compact) >= 4 or compact.isdigit()) and compact in spoken.replace(" ", "")


def _speaker(raw):
    """說話者代號只收 A~D（配音只有四個聲音），其他一律當 A。"""
    s = str(raw or "A").strip().upper()[:1]
    return s if s in "ABCD" and s else "A"


def _letters(options):
    return "ABCDE"[: len(options)]


def parse_section(text, section):
    """解析並驗證 Gemini 回傳；格式不合丟 ValueError。有效題少於 MIN_VALID 也丟 ValueError。"""
    data = jsonutil.loads_lenient(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("格式錯誤")
    segments = [{"speaker": _speaker(s.get("speaker")), "text": " ".join(str(s["text"]).split())}
                for s in data.get("segments", []) if isinstance(s, dict) and str(s.get("text", "")).strip()]
    if sum(len(s["text"].split()) for s in segments) < 300:
        raise ValueError("腳本太短")
    raw_speakers = data.get("speakers") if isinstance(data.get("speakers"), dict) else {}
    used = sorted({s["speaker"] for s in segments})
    speakers = {k: " ".join(str(raw_speakers[k]).split())[:80] for k in used if str(raw_speakers.get(k, "")).strip()}
    script = "\n".join(f"**{s['speaker']}**: {s['text']}" for s in segments)
    if speakers and len(used) > 1:
        script = "👥 " + "；".join(f"{k} = {v}" for k, v in speakers.items()) + "\n" + script
    spoken = _canon(" ".join(s["text"] for s in segments))

    choices = [re.sub(r"^\(?[A-Ea-e][.)]\s*", "", str(c)).strip() for c in data.get("choices") or [] if str(c).strip()]
    questions = []
    for raw in data.get("questions", []):
        item = _clean_question(raw, choices)
        if not item:
            continue
        if item["type"] == "blank" and not any(in_script(a, spoken) for a in [item["answer"]] + item["alternatives"]):
            continue  # 答案沒有出現在腳本裡：多半是 Gemini 編的，丟掉
        item["evidence"] = exercises.locate_evidence(script, item["evidence"])
        questions.append(item)
    questions = questions[:QUESTIONS_PER_SECTION]
    if len(questions) < MIN_VALID:
        raise ValueError(f"可用題目不足（{len(questions)}/{QUESTIONS_PER_SECTION}）")
    if any(q["type"] == "match" for q in questions) and len(choices) < 3:
        raise ValueError("配對題沒有選項")
    return {
        "title": " ".join(str(data.get("title", "")).split()) or SECTIONS[section]["name"],
        "context": " ".join(str(data.get("context", "")).split()),
        "segments": segments,
        "speakers": speakers,
        "script": script,
        "choices": choices,
        "questions": questions,
        "raw_count": len(data.get("questions", [])),
    }


def _clean_question(raw, choices):
    if not isinstance(raw, dict) or not str(raw.get("question", "")).strip():
        return None
    qtype = str(raw.get("type", "")).lower()
    answer = " ".join(str(raw.get("answer", "")).split())
    if not answer:
        return None
    evidence = raw.get("evidence") or ""
    if isinstance(evidence, list):  # Gemini 偶爾把依據包成陣列
        evidence = " ".join(str(e) for e in evidence)
    item = {"type": qtype, "question": str(raw["question"]).strip(), "options": [], "answer": answer,
            "alternatives": [], "explanation": str(raw.get("explanation", "")).strip(),
            "evidence": " ".join(str(evidence).split()), "band": None}
    if qtype == "blank":
        if len(answer.split()) > 3 and not all(w.isdigit() for w in answer.split()):
            return None
        if "____" not in item["question"]:
            item["question"] += " ____"
        item["alternatives"] = [" ".join(str(a).split()) for a in raw.get("alternatives") or [] if str(a).strip()]
    elif qtype == "mcq":
        options = [re.sub(r"^\(?[A-Da-d][.)]\s*", "", str(o)).strip() for o in raw.get("options") or []]
        answer = answer.upper()
        if len(options) not in (3, 4) or len(answer) != 1 or answer not in _letters(options):
            return None
        item["options"], item["answer"] = options, answer
    elif qtype == "match":
        answer = answer.upper()
        if len(answer) != 1 or answer not in "ABCDE" or ("ABCDE".index(answer) >= len(choices)):
            return None
        item["answer"] = answer
    else:
        return None
    return item


# ---------------- 作答 ----------------
_LINE = re.compile(r"^\s*(?:q(?:uestion)?\.?\s*)?(\d{1,2})\s*[.):：\-]?\s*(.+?)\s*$", re.I)
_COMPACT = re.compile(r"^(\s*\d{1,2}\s*[.):\-]?\s*[A-Ea-e]\s*[,;，]?)+$")


def parse_answers(text, total):
    """把使用者的訊息拆成 {題號: 答案}。每行一題（`3 Brown`、`3. Brown`、`Q3: Brown`）；
    全是選擇題的簡寫（`1A 2B 3C`）也可以。認不得的行忽略。題號超出範圍的忽略。"""
    text = str(text).strip()
    answers = {}
    if _COMPACT.match(text.replace("\n", " ")):
        for num, letter in re.findall(r"(\d{1,2})\s*[.):\-]?\s*([A-Ea-e])", text):
            answers[int(num)] = letter.upper()
    else:
        for line in text.splitlines():
            answers.update(_split_line(line))
    return {n: a for n, a in answers.items() if 1 <= n <= total}


_NUM_AT = re.compile(r"(?<!\S)(\d{1,2})\s*[.):：]?(?=\s|$)")


def _split_line(line):
    """一行一題；若同一行接著出現「下一個題號」（`1 word1 2 word2`），也拆開。題號必須連續遞增，
    所以答案本身是數字（`4 25`）不會被誤拆。"""
    m = _LINE.match(line)
    if not m:
        return {}
    first = int(m.group(1))
    body_start = m.start(2)
    cuts, expect = [], first + 1
    for c in _NUM_AT.finditer(line, body_start):
        if int(c.group(1)) == expect:
            cuts.append((expect, c.start(), c.end()))
            expect += 1
    pieces, begin, number = {}, body_start, first
    for n, start, end in cuts:
        pieces[number] = line[begin:start].strip()
        begin, number = end, n
    pieces[number] = line[begin:].strip()
    if any(not v for v in pieces.values()):  # 例如 `1 2 3`：分不出來，當成第 1 題的答案
        return {first: m.group(2).strip()}
    return pieces


def check_answer(item, text):
    """回傳 "correct" / "wrong"。blank 不分大小寫、容許標點與連字號差異，拼字要對；mcq/match 只看字母。"""
    if item["type"] in ("mcq", "match"):
        m = re.match(r"^\(?([A-Ea-e])\)?[.)]?\s*", str(text).strip())
        return "correct" if m and m.group(1).upper() == item["answer"] else "wrong"
    given = _canon(text)
    return "correct" if given and given in {_canon(a) for a in [item["answer"]] + item["alternatives"]} else "wrong"


def answer_text(item):
    if item["type"] == "mcq":
        return f"{item['answer']}. {item['options']['ABCDE'.index(item['answer'])]}"
    return item["answer"]


# ---------------- 顯示 ----------------
def format_sheet(section, data, number_offset=0):
    """考前先給考生看的題目卷（可以在錄音開始前先讀）。number_offset 用在完整測驗，題號接續。"""
    lines = [f"📝 **{SECTIONS[section]['name']}**　{data['context']}".rstrip()]
    if len(data.get("speakers") or {}) > 1:
        lines.append("👥 " + "｜".join(f"**{k}** {v}" for k, v in data["speakers"].items()))
    questions = data["questions"]
    shown_choices = shown_blank = False
    for i, q in enumerate(questions, 1):
        n = i + number_offset
        if q["type"] == "blank" and not shown_blank:
            shown_blank = True
            lines.append(f"\n填空題：{MAX_WORDS_NOTE}")
        if q["type"] == "match" and not shown_choices:
            shown_choices = True
            lines.append("\n配對題：選出最符合的選項（每個選項可重複使用）")
            lines += [f"　**{letter}**　{c}" for letter, c in zip("ABCDE", data["choices"])]
        lines.append(f"**{n}.** {q['question']}")
        if q["type"] == "mcq":
            lines += [f"　{letter}. {opt}" for letter, opt in zip("ABCD", q["options"])]
    return "\n".join(lines)


def narration(section, data):
    """錄音開頭由旁白念的介紹（像真實考試：Section 3. You will hear …），含說話者是誰。"""
    parts = [f"{SECTIONS[section]['name']}.", data["context"]]
    if len(data.get("speakers") or {}) > 1:
        parts.append("The speakers are " + "; ".join(data["speakers"].values()) + ".")
    return {"speaker": "N", "text": " ".join(p for p in parts if p)}


ANSWER_HELP = (
    "✍️ **作答方式**：一則訊息一行一題，例如 `1 Brown`、`2 B`（選擇題也可以一次寫 `3A 4C 5B`）；"
    "同一題再答一次會覆蓋。答完輸入 `done` 交卷（`stop` 也一樣），超過 10 分鐘沒回應會自動交卷。"
)


def result_blocks(questions, answers, number_offset=0):
    """逐題結果：每題一個區塊（不會被切開）。answers: {題號(從 1): 使用者答案}。"""
    blocks = []
    for i, q in enumerate(questions, 1):
        given = answers.get(i)
        if given is None:
            icon, mine = "➖", "（未作答）"
        elif check_answer(q, given) == "correct":
            icon, mine = "✅", given
        else:
            icon, mine = "❌", given
        lines = [f"**第 {i + number_offset} 題 {icon}**　{q['question']}",
                 f"你的答案：{mine}｜正確答案：**{answer_text(q)}**"]
        if q["alternatives"]:
            lines[-1] += f"（也接受：{'、'.join(q['alternatives'][:3])}）"
        if q["evidence"]:
            lines.append(f"📖 錄音依據：*{q['evidence'][:400]}*")
        if q["explanation"]:
            lines.append(f"💡 {q['explanation']}")
        blocks.append("\n".join(lines))
    return blocks
