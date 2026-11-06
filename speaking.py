"""口說模擬測驗（類雅思 Part 1 / 2 / 3）：出題 prompt、解析、評分 prompt（純函式，不碰 Discord 與資料庫）。"""
import difflib
import json
import random
import re

import jsonutil

CLOSING = "Thank you. That is the end of the speaking test."
PART_NAMES = {1: "Part 1", 2: "Part 2", 3: "Part 3"}

GENERATION_SCHEMA = (
    '輸出 JSON 物件：{"topic": 整組題目的英文主題簡稱, '
    '"part1": [{"topic": Part 1 的英文話題（如 Hometown、Daily routine）, "questions": [3 題英文問題]}, ...共 2 個話題], '
    '"part2": {"prompt": 一句英文題目（Describe ...）, "points": [3 個英文提示，如 what it was / when ... / why ...], '
    '"closing": 以 and explain ... 開頭的一句收尾}, '
    '"part3": [4 題與 Part 2 話題相關、較抽象的英文討論題]}。'
    "每題都是考官會口頭問的自然英文，一句話，不要編號。沒有被要求的部分給 null。"
)


# 沒指定主題時，從這裡隨機抽，避免每次都出一樣的題目
PART1_TOPICS = [
    "Hometown", "Work or study", "Daily routine", "Family", "Friends", "Food and cooking", "Weather", "Hobbies",
    "Music", "Reading", "Sports", "Shopping", "Transport", "Technology", "Social media", "Holidays", "Neighbours",
    "Clothes", "Sleep", "Health", "Art", "Museums", "Photography", "Gifts", "Birthdays", "Weekends", "Shoes",
    "Plants and gardens", "Home and accommodation", "Time management", "Languages", "Mornings", "Money", "Walking",
]
PART2_THEMES = [
    "a person who influenced you", "a place you visited", "a memorable meal", "a book or film you enjoyed",
    "a skill you learned", "a time you helped someone", "a piece of technology you use", "a celebration you attended",
    "a building you like", "a journey you remember", "an object that is special to you", "a time you were busy",
    "a teacher or colleague", "a decision you made", "a public place you enjoy", "a sport or game you tried",
    "an event you are looking forward to", "a gift you gave or received", "a historical place", "a piece of clothing",
]


def build_generation_prompt(level, parts, topic="", avoid=()):
    wanted = "、".join(PART_NAMES[p] for p in sorted(parts))
    if topic:
        topic_line = f"主題請以「{topic}」為主。"
    else:
        p1 = "、".join(random.sample(PART1_TOPICS, 2))
        topic_line = (f"請用這些方向出題：Part 1 的兩個話題用 {p1}；Part 2 的題卡主題是「{random.choice(PART2_THEMES)}」，"
                      "Part 3 與 Part 2 相關。")
    avoid_line = ("\n以下是以前出過的題目，這次絕對不要重複，也不要只換幾個字：\n- " + "\n- ".join(avoid[:40])) if avoid else ""
    return (
        f"你是雅思口說考官。請出一組模擬口說測驗，學習者口說程度約 band {level:.1f}，"
        f"題目難度對應 band {level:.1f}~{level + 1.0:.1f}。{topic_line}"
        f"這次要包含：{wanted}。{avoid_line}\n" + GENERATION_SCHEMA
    )


def set_questions(data):
    """把 parse_set 的結果取出所有題目文字（Part 2 取題卡主題句）。"""
    return [s["question"] for s in data["steps"]]


def repeated_questions(data, previous, threshold=0.8):
    """這組題目裡，有幾題跟以前出過的一樣或太像。"""
    olds = [" ".join(re.sub(r"[^\w\s]", "", q.lower()).split()) for q in previous]
    count = 0
    for q in set_questions(data):
        new = " ".join(re.sub(r"[^\w\s]", "", q.lower()).split())
        if any(new == o or difflib.SequenceMatcher(None, new, o).ratio() >= threshold for o in olds):
            count += 1
    return count


def _clean_questions(items):
    return [str(q).strip() for q in (items or []) if str(q).strip()]


def parse_set(text, parts):
    """解析 Gemini 的出題結果，轉成依序要問的步驟。格式不合時丟 ValueError。

    每個 step：{"part", "question"（顯示用文字）, "prefix"（考官轉場說明）, "card"（Part 2 題卡或 None）}
    """
    data = jsonutil.loads_lenient(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("格式錯誤")

    steps = []
    if 1 in parts:
        for i, group in enumerate(data.get("part1") or []):
            if not isinstance(group, dict):
                continue
            topic = str(group.get("topic", "")).strip().lower() or "yourself"
            questions = _clean_questions(group.get("questions"))
            for j, q in enumerate(questions):
                prefix = ""
                if j == 0:
                    prefix = (
                        f"In Part 1, I'd like to ask you some questions about yourself. Let's talk about {topic}."
                        if not any(s["part"] == 1 for s in steps)
                        else f"Now let's talk about {topic}."
                    )
                steps.append({"part": 1, "question": q, "prefix": prefix, "card": None})

    part2 = data.get("part2") if 2 in parts else None
    if isinstance(part2, dict) and str(part2.get("prompt", "")).strip():
        prompt = str(part2["prompt"]).strip()
        points = _clean_questions(part2.get("points"))
        closing = str(part2.get("closing", "")).strip()
        card = prompt + "\nYou should say:\n" + "\n".join(f"- {p}" for p in points)
        if closing:
            card += "\n" + closing
        spoken = f"{prompt} You should say: {', '.join(points)}. {closing}".strip()
        steps.append({
            "part": 2,
            "question": spoken,
            "card": card,
            "prefix": (
                "Now, in Part 2, I'm going to give you a topic, and I'd like you to talk about it for one to two "
                "minutes. You have one minute to think about what you are going to say. Here is your topic."
            ),
        })

    if 3 in parts:
        first = True
        for q in _clean_questions(data.get("part3")):
            prefix = (
                "Now, in Part 3, I'd like to discuss some more general questions related to this topic."
                if first and 2 in parts
                else "In Part 3, I'd like to discuss some general questions." if first else ""
            )
            first = False
            steps.append({"part": 3, "question": q, "prefix": prefix, "card": None})

    if not steps:
        raise ValueError("沒有題目")
    # 整場測驗的第一題前面加開場白
    steps[0]["prefix"] = ("Good morning. This is the IELTS speaking practice test. " + steps[0]["prefix"]).strip()
    return {"topic": str(data.get("topic", "")).strip() or "IELTS Speaking Test", "steps": steps}


def build_followup_prompt(level, part2_question, count):
    """Part 2 答完後，依學生實際說的內容設計 Part 3 追問（錄音會一併交給 Gemini）。"""
    return (
        f"你是雅思口說考官。學生剛回答完 Part 2（題目：{part2_question}）。請聽他的錄音，"
        f"依他實際說的內容設計 {count} 題 Part 3 追問：前半數的題目要直接連結他提到的具體內容"
        "（可以引用他說過的人事物），後半數的題目擴展到較抽象的社會層面。"
        f"每題一句自然的英文，難度約 band {level:.1f}~{level + 1.0:.1f}，不要編號。\n"
        '輸出 JSON 物件：{"part3": [英文問題, ...]}'
    )


def parse_followups(text, limit):
    data = jsonutil.loads_lenient(text)
    if isinstance(data, list):
        data = {"part3": data}
    questions = _clean_questions(data.get("part3") if isinstance(data, dict) else None)[:limit]
    if not questions:
        raise ValueError("沒有追問題目")
    return questions


FOLLOWUP_PREFIX = (
    "Thank you. Now, in Part 3, I'd like to discuss some more general questions related to what you have just talked about."
)


def segments_for(step):
    """考官這一題要念的內容（交給本地語音合成）。"""
    return [{"speaker": "A", "text": t} for t in (step["prefix"], step["question"]) if t]


SCORING_SYSTEM = """
你是嚴謹的雅思口說考官與教練，請用繁體中文講解，英文例句保持英文。
使用者會依序提供每一題的題目與他的語音回答（有些題目可能被跳過，沒有錄音）。
請只輸出一個 JSON 物件（不要 Markdown、不要 ``` 圍欄），格式：
{
  "band": 整體 band（1.0~9.0，0.5 為單位的數字；請嚴格、不要灌水）,
  "summary": "一句話總結這次表現（繁體中文）",
  "criteria": {"fluency": 數字, "lexical": 數字, "grammar": 數字, "pronunciation": 數字},
  "criteria_notes": {
    "fluency": "流暢與連貫的細項說明：優點與最大問題各一點，盡量引用學生的原話（繁體中文，1~2 句）",
    "lexical": "詞彙資源的細項說明（同上）",
    "grammar": "文法多樣性與準確性的細項說明，指出具體錯誤（同上）",
    "pronunciation": "發音的細項說明。只能依錄音大致判斷，請保守，只列你有把握聽出的問題，並註明這是估計"
  },
  "parts": {"1": 數字或 null, "2": 數字或 null, "3": 數字或 null},
  "priorities": ["最優先要練的 3 件事，每件一句（繁體中文）"],
  "suggestions": [
    {"seq": 題號, "original": "學生說過的原話（必須逐字引用逐字稿裡的片段）",
     "better": "升級後的英文句子（保留學生原本想表達的意思，只升級用字、文法或連貫，不要改成別的內容）",
     "why": "為什麼這樣改更好（繁體中文，一句）"}
  ],
  "part2_sample": "Part 2 的升級示範回答，約 100~150 字英文；沒有 Part 2 就給 null",
  "transcripts": ["第 1 題學生回答的逐字稿（保留他說的錯誤，不要修正）", "第 2 題…", ...依題號順序，沒回答的給空字串],
  "target_word": "這組測驗的主題",
  "polished_sentence": "學生最值得升級的一句話的升級版",
  "synonyms": "建議學起來的詞彙或片語（逗號分隔）",
  "feedback": "最重要的弱點摘要（繁體中文）",
  "mastery": 1 到 5 的整數
}
規則：suggestions 要 4~8 項，優先挑最能提升分數的句子，每個有作答的 Part 至少一項；
沒回答的題目不計分，也不要編造學生沒說過的內容。
"""

CRITERIA_NAMES = {
    "fluency": "流暢與連貫",
    "lexical": "詞彙資源",
    "grammar": "文法多樣性與準確性",
    "pronunciation": "發音",
}


def parse_scoring(text):
    """解析評分 JSON；格式不對時丟 ValueError。"""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip())
    data = jsonutil.loads_lenient(raw)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise ValueError("評分格式錯誤")
    return data


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 1.0 <= number <= 9.0 else None


def _text(value):
    return str(value).strip() if value not in (None, "") else ""


def spoiler_blocks(text, limit=1700):
    """把長文字切成多段（盡量在句子邊界），每段各自包成劇透。文字裡的 || 會被拿掉避免破壞標記。"""
    text = text.replace("||", "")
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
        while len(sentence) > limit:  # 沒有標點的超長句硬切
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [f"||{p}||" for p in pieces] or ["||（無內容）||"]


def pack_blocks(blocks, limit=1900):
    """把區塊合併成多則訊息；絕不在區塊中間切開（不然劇透標記會壞掉）。"""
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


def build_report(data, qa):
    """把評分 JSON 排成 Discord 訊息與週報用的 Markdown。

    qa：[{"seq", "part", "question", "skipped"}]（依題號，只含實際問過的題目）。
    回傳 {"messages": [Discord 訊息…], "markdown": 週報用的文字（不含劇透）}。
    """
    band = _num(data.get("band"))
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    notes = data.get("criteria_notes") if isinstance(data.get("criteria_notes"), dict) else {}
    parts = data.get("parts") if isinstance(data.get("parts"), dict) else {}
    transcripts = data.get("transcripts") if isinstance(data.get("transcripts"), list) else []

    # ---- 總評與四項細項 ----
    head = ["### 總評", f"**整體估分：{f'band {band:.1f}' if band else '未取得'}**"]
    if _text(data.get("summary")):
        head.append(_text(data["summary"]))
    head += ["", "**四項口說評分**"]
    for key, name in CRITERIA_NAMES.items():
        score = _num(criteria.get(key))
        line = f"- **{name}｜{f'{score:.1f}' if score else '-'}**"
        if _text(notes.get(key)):
            line += f"：{_text(notes[key])}"
        head.append(line)
    part_scores = [f"Part {k} {_num(v):.1f}" for k, v in sorted(parts.items()) if _num(v)]
    if part_scores:
        head.append("\n**各 Part 估分**：" + "｜".join(part_scores))
    priorities = [_text(p) for p in (data.get("priorities") or []) if _text(p)]
    if priorities:
        head += ["", "**最優先要練的 3 件事**"] + [f"{i}. {p}" for i, p in enumerate(priorities[:3], 1)]

    # ---- 依你的回答給的建議例句 ----
    part_of = {item["seq"]: item["part"] for item in qa}
    sugg = ["### ✏️ 建議例句（依你的回答）"]
    count = 0
    for s in data.get("suggestions") or []:
        if not isinstance(s, dict) or not _text(s.get("original")) or not _text(s.get("better")):
            continue
        count += 1
        try:
            seq = int(s.get("seq"))
        except (TypeError, ValueError):
            seq = None
        where = f"第 {seq} 題 · Part {part_of[seq]}" if seq in part_of else f"例句 {count}"
        block = [f"**{where}**", f"你說：「{_text(s['original'])}」", f"建議：「{_text(s['better'])}」"]
        if _text(s.get("why")):
            block.append(f"💡 {_text(s['why'])}")
        sugg.append("\n".join(block))
    sample = _text(data.get("part2_sample"))
    if sample:
        sugg.append("**Part 2 升級示範回答**\n> " + sample.replace("\n", "\n> "))
    sugg_messages = pack_blocks(sugg) if count or sample else []

    # ---- 逐字稿（劇透，點開才看得到）----
    blocks = ["### 📜 逐字稿（點黑色區塊展開）"]
    for item in qa:
        seq = item["seq"]
        said = _text(transcripts[seq - 1]) if seq - 1 < len(transcripts) else ""
        if item["skipped"]:
            said = "（跳過）"
        title = f"**第 {seq} 題 · Part {item['part']}**"
        # 長文字會被切成多個劇透段；每一段各自成為一個區塊，才不會讓單則訊息超過長度上限
        for i, piece in enumerate(spoiler_blocks(item["question"])):
            blocks.append(f"{title}\n考官：{piece}" if i == 0 else piece)
        for i, piece in enumerate(spoiler_blocks(said or "（沒有取得逐字稿）")):
            blocks.append(f"你：{piece}" if i == 0 else piece)
    transcript_messages = pack_blocks(blocks)

    # ---- 週報用的 Markdown（不含劇透）----
    md = list(head) + [""]
    if count or sample:
        md += ["", "\n\n".join(sugg)]
    return {
        "messages": ["\n".join(head)] + sugg_messages + transcript_messages,
        "markdown": "\n".join(md),
    }


def question_label(index, step):
    return f"第 {index} 題（{PART_NAMES[step['part']]}）：{step['question']}"
