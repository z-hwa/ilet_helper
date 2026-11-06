"""德語寶寶的 gossip 閒聊模式（純邏輯：提示詞、解析、即時點評排版、結算報告；不碰 Discord 與 Gemini）。

流程：老師先開場 → 使用者每回合傳一則語音 → Gemini 聽音檔，一次回傳
「逐字稿＋英文翻譯＋文法對錯＋即時點評＋老師的回應」→ 指定回合數到了（或 stop）再結算評分。
"""
import re

import german_logic as gl
import speaking

MIN_ROUNDS, MAX_ROUNDS, DEFAULT_ROUNDS = 3, 12, 6
MIN_ANSWERED_TO_SCORE = 2
TRANSLATION = "English"  # 逐字稿旁邊的翻譯語言（要改成中文就改這裡，並把下面提示詞的「英文」換掉）
STATUSES = ("perfect", "ok", "errors", "unclear")
UNCLEAR_REPLY = ("Entschuldigung, das habe ich nicht richtig verstanden. Kannst du es bitte noch einmal sagen?",
                 "Sorry, I didn't quite catch that. Could you say it once more?")

PERSONA = (
    "你是德語學習夥伴「Lena」，正在跟學生用德語『聊八卦』：輕鬆的生活八卦，像同事、鄰居、朋友、社區、"
    "網紅或名人的小趣事、尷尬糗事、感情小插曲。人物一律用虛構的名字，不要影射真實的人，不要惡意中傷。"
)


def build_opening_prompt(level, topic, rounds):
    topic_line = f"八卦的主題：{topic}。" if topic else "八卦的主題請自選一個有趣的（辦公室、鄰居、朋友聚會、社群媒體、旅行糗事等擇一）。"
    return (
        f"{PERSONA}學生母語是繁體中文，程度 CEFR {gl.clean(level)}，這場會聊 {rounds} 個回合。{topic_line}\n"
        f"請寫開場白：用德語、{level} 學生聽得懂的詞彙與句長，1~3 句，先打招呼，丟出一則你聽到的（虛構）小八卦，"
        f"最後問學生一個問題，讓他接話。再附{TRANSLATION}翻譯。\n"
        '輸出 JSON 物件：{"topic": 德語主題（短），"german": 開場白（德語）, "english": 開場白的英文翻譯}'
    )


def parse_opening(text):
    data = gl._load(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("格式錯誤")
    german = gl.clean(data.get("german"))
    if not german:
        raise ValueError("缺少開場白")
    return {"topic": gl.clean(data.get("topic")) or "Klatsch und Tratsch", "german": german,
            "english": gl.clean(data.get("english"))}


def turn_system(level, topic, round_no, rounds):
    last = round_no >= rounds
    ending = ("這是最後一回合：reply 用來收尾（回應他說的內容、道謝、道別），不要再提問。"
              if last else "reply 最後要拋出一個問題接話。")
    return f"""
{PERSONA}學生母語是繁體中文，程度 CEFR {level}。聊天主題：{topic}。現在是第 {round_no}/{rounds} 回合。
每一輪你會收到：先前的對話紀錄，以及學生這一輪的語音。請依序做：
1. 聽出學生說的話（transcript）：逐字寫下，保留他的錯誤，不要自己修正；äh、ähm 這類填充音可以省略。
2. 只檢查『這一輪』學生說的話；對話紀錄只是上下文，前面回合的錯誤不要再算一次，errors 的 said 必須出自這一輪的 transcript。
   檢查他『說出來的德語』：文法（格、冠詞與詞性、動詞位置與變位、時態、詞尾、助動詞）、用字、自然度。
   這是口說，不要挑大小寫與標點；只挑真正的錯，小瑕疵不影響理解就別提，不要為了挑而挑。
3. 判定 status：perfect＝完全正確又自然；ok＝沒有文法錯誤，但有更自然的說法（寫在 better）；
   errors＝有錯誤（列在 errors）；unclear＝沒聽清楚或沒有德語內容（此時 errors 留空，reply 請他再說一次）。
4. verdict：即時點評，繁體中文一兩句，誠實又鼓勵（不要每次都說一樣的話）。
5. errors：每項 {{"said": 他說的那一小段（逐字引用逐字稿，一定要包含出錯的那個字，例如助動詞 habe 要連同過去分詞一起引用）, "fix": 改好的說法（一定要和 said 不一樣）, "why": 繁體中文一句話說明規則}}，最多 4 項。
6. better：status 為 ok 或 errors 時，給整句更自然的完整說法（保留他原本的意思）；其他情況留空字串。
7. pronunciation_note：你有把握聽出的發音問題（如 ch、r、ü、ö、重音），沒有就留空字串。不要編造。
8. reply：以朋友身分用德語回應他說的內容，用 {level} 學生聽得懂的詞彙，1~3 句，先接他的話、分享一點你的八卦。{ending}
9. reply_en：reply 的{TRANSLATION}翻譯。transcript_en：學生那段話的{TRANSLATION}翻譯（照他原本的意思，不是修正版）。
請只輸出一個 JSON 物件（不要 Markdown、不要 ``` 圍欄）：
{{"transcript": "...", "transcript_en": "...", "status": "perfect|ok|errors|unclear", "verdict": "...",
  "errors": [{{"said": "...", "fix": "...", "why": "..."}}], "better": "...", "pronunciation_note": "...",
  "reply": "...", "reply_en": "..."}}
"""


def history_lines(opening, turns):
    lines = [f"老師：{opening['german']}"]
    for t in turns:
        lines += [f"學生：{t['transcript']}", f"老師：{t['teacher_de']}"]
    return "\n".join(lines)


def build_turn_text(opening, turns, attempt):
    """送給 Gemini 的文字部分（音檔另外附）。attempt 讓重傳的請求不會被『重複請求』防護擋掉。"""
    return (f"【對話紀錄】\n{history_lines(opening, turns)}\n\n"
            f"【學生這一輪的語音在下面的音檔】（內部序號 {attempt}）請依規則輸出 JSON。")


def _said_in(said, transcript):
    """錯誤引用的原話是不是真的出現在這一輪的逐字稿裡（容許用 [...] 或 … 省略中間；忽略標點與大小寫）。"""
    haystack = gl.comparable(transcript).lower()
    fragments = [f for f in re.split(r"\[?\s*(?:\.\.\.|…)\s*\]?", said) if gl.comparable(f)]
    return bool(fragments) and all(gl.comparable(f).lower() in haystack for f in fragments)


def _errors(raw, transcript):
    out = []
    for e in (raw or []):
        if (isinstance(e, dict) and gl.clean(e.get("said")) and gl.clean(e.get("fix"))
                and gl.comparable(e["said"]).lower() != gl.comparable(e["fix"]).lower()  # 前後一樣的不是錯誤
                and _said_in(gl.clean(e["said"]), transcript)):  # 不是這一輪說的（模型把前面回合的錯誤再報一次）也不算
            out.append({"said": gl.clean(e["said"]), "fix": gl.clean(e["fix"]), "why": gl.clean(e.get("why"))})
    return out[:4]


def parse_turn(text):
    data = gl._load(text)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("格式錯誤")
    status = gl.clean(data.get("status")).lower()
    if status not in STATUSES:
        raise ValueError("status 不認得")
    transcript = gl.clean(data.get("transcript"))
    reply, reply_en = gl.clean(data.get("reply")), gl.clean(data.get("reply_en"))
    if status == "unclear" or not transcript:
        return {"status": "unclear", "transcript": "", "transcript_en": "", "verdict": "", "errors": [], "better": "",
                "pronunciation_note": "", "reply": reply or UNCLEAR_REPLY[0], "reply_en": reply_en or UNCLEAR_REPLY[1]}
    if not reply:
        raise ValueError("缺少老師的回應")
    errors = _errors(data.get("errors"), transcript)
    if status == "errors" and not errors:
        status = "ok"  # 說有錯卻列不出來：不當成有錯，免得誤導
    if status == "perfect":
        errors = []
    return {"status": status, "transcript": transcript, "transcript_en": gl.clean(data.get("transcript_en")),
            "verdict": gl.clean(data.get("verdict")), "errors": errors, "better": gl.clean(data.get("better")),
            "pronunciation_note": gl.clean(data.get("pronunciation_note")), "reply": reply, "reply_en": reply_en}


def format_turn(turn, round_no, rounds):
    """每回合的即時回饋：逐字稿（遮擋）、文法對錯與點評、老師的回應（德文與翻譯遮擋，語音另外附）。"""
    lines = [f"**第 {round_no}/{rounds} 回合**　你說：||{turn['transcript'].replace('||', '')}||"]
    status, errors = turn["status"], turn["errors"]
    if status == "perfect":
        lines.append("✅ **文法正確、說得很自然！**")
    elif status == "ok":
        lines.append("👍 **文法沒有錯**，不過有更自然的說法")
    else:
        lines.append(f"✏️ **有 {len(errors)} 個地方可以調整**")
    if turn["verdict"]:
        lines.append(turn["verdict"])
    for e in errors:
        lines.append(f"• 「{e['said']}」→「**{e['fix']}**」" + (f"：{e['why']}" if e["why"] else ""))
    if turn["better"] and status != "perfect":
        lines.append(f"💡 更自然的說法：**{turn['better']}**")
    if turn["pronunciation_note"]:
        lines.append(f"🗣️ 發音：{turn['pronunciation_note']}")
    return "\n".join(lines)


def format_teacher(text_de, text_en, heading="🎧 **Lena 說**"):
    """老師的話：只放遮擋的文字（語音附在同一則訊息），先聽再核對。"""
    return (f"{heading}（聽音檔；想核對文字：||{text_de.replace('||', '')}||）"
            + (f"\n🌐 ||{text_en.replace('||', '')}||" if text_en else ""))


FINAL_SYSTEM = """
你是嚴謹的德語口說老師，學生母語是繁體中文，請用繁體中文講解，德語例句保持德語。
下面是一場德語『閒聊（gossip）』的逐回合紀錄：每回合有學生說的話（逐字稿）、當下找到的文法錯誤與發音備註（發音是聽音檔當場記下的）。
請只輸出一個 JSON 物件（不要 Markdown、不要 ``` 圍欄）：
{
  "level_estimate": "依這場表現估計的 CEFR 程度，如 A2、B1+（不要灌水）",
  "summary": "一句話總結（繁體中文）",
  "criteria": {"pronunciation": 1~5 的數字, "vocabulary": 1~5, "grammar": 1~5, "fluency": 1~5},
  "criteria_notes": {"pronunciation": "依各回合的發音備註整理並註明是估計；若完全沒有備註，發音分數給 3，並寫明這次沒有足夠依據判斷", "vocabulary": "...",
                     "grammar": "指出反覆出現的錯誤類型", "fluency": "依句子長度與連貫度判斷"},
  "priorities": ["最優先要練的 3 件事，每件一句（繁體中文）"],
  "suggestions": [{"seq": 回合數, "original": "學生說過的原話（逐字引用逐字稿）", "better": "改好的德語句子", "why": "為什麼（繁體中文一句）"}]
}
規則：suggestions 要 3~6 項，優先挑反覆出現或最影響理解的錯誤；沒有錯的回合不用硬挑；不要編造學生沒說過的內容。
"""


def build_final_text(level, topic, turns):
    parts = [f"一場德語閒聊（CEFR {level}），主題：{topic}，共 {len(turns)} 回合。"]
    for t in turns:
        parts.append(f"第 {t['seq']} 回合｜學生說：{t['transcript']}")
        for e in t["errors"]:
            parts.append(f"  錯誤：「{e['said']}」→「{e['fix']}」（{e['why']}）")
        if t["status"] == "ok" and t["better"]:
            parts.append(f"  更自然：{t['better']}")
        if t["pronunciation_note"]:
            parts.append(f"  發音備註：{t['pronunciation_note']}")
    return "\n".join(parts)


def build_report(data, turns):
    """評分 JSON ＋逐回合紀錄 → {"messages": 總評與建議, "items": 逐字稿（每回合一組）, "markdown": 週報用文字}。"""
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    notes = data.get("criteria_notes") if isinstance(data.get("criteria_notes"), dict) else {}
    head = ["### 總評", f"**程度估計：{gl.clean(data.get('level_estimate')) or '未取得'}**"]
    if gl.clean(data.get("summary")):
        head.append(gl.clean(data["summary"]))
    head += ["", "**四項評分（滿分 5）**"]
    for key, name in gl.CRITERIA_NAMES.items():
        score = gl._score(criteria.get(key))
        line = f"- **{name}｜{f'{score:.1f}' if score else '-'}**"
        if gl.clean(notes.get(key)):
            line += f"：{gl.clean(notes[key])}"
        head.append(line)
    priorities = [gl.clean(p) for p in (data.get("priorities") or []) if gl.clean(p)]
    if priorities:
        head += ["", "**最優先要練的 3 件事**"] + [f"{i}. {p}" for i, p in enumerate(priorities[:3], 1)]

    sugg, count = ["### ✏️ 建議例句（依你說的話）"], 0
    for s in data.get("suggestions") or []:
        if not isinstance(s, dict) or not gl.clean(s.get("original")) or not gl.clean(s.get("better")):
            continue
        count += 1
        try:
            where = f"第 {int(s.get('seq'))} 回合"
        except (TypeError, ValueError):
            where = f"例句 {count}"
        block = [f"**{where}**", f"你說：「{gl.clean(s['original'])}」", f"建議：「{gl.clean(s['better'])}」"]
        if gl.clean(s.get("why")):
            block.append(f"💡 {gl.clean(s['why'])}")
        sugg.append("\n".join(block))

    items = []
    for t in turns:
        blocks = ["### 📜 逐字稿與英文翻譯（點黑色區塊展開）"] if not items else []
        label = f"**第 {t['seq']} 回合**" if t["seq"] else "**開場**"
        first = True

        def add(who, text):
            nonlocal first
            for i, piece in enumerate(speaking.spoiler_blocks(text)):
                blocks.append((f"{label}\n" if first else "") + f"{who}：{piece}" if i == 0 else piece)
                first = False

        def add_translation(text):
            if text:
                blocks.append("🌐 " + " ".join(speaking.spoiler_blocks(text)))

        if t["seq"]:  # 先是你說的，再是 Lena 的回應（seq 0 是 Lena 的開場，沒有使用者的話）
            add("你", t["transcript"])
            add_translation(t["transcript_en"])
        add("Lena", t["teacher_de"])
        add_translation(t["teacher_en"])
        items.append({"seq": t["seq"], "texts": speaking.pack_blocks(blocks)})

    md = list(head) + (["", "\n\n".join(sugg)] if count else [])
    return {"messages": ["\n".join(head)] + (speaking.pack_blocks(sugg) if count else []),
            "items": items, "markdown": "\n".join(md)}
