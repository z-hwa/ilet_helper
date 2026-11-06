"""每週學習紀錄：把一週（週一到週日）內的所有練習整理成一個 Markdown 檔。

內容全部由資料庫即時產生，所以檔案被刪掉或想改格式，隨時都能重建，不會遺失資料。
"""
import datetime
import json
from collections import Counter
from contextlib import closing
from pathlib import Path

import skills

RESULT_ICON = {"correct": "✅", "spelling": "🔤", "wrong": "❌", "skip": "⏭️", "typo": "✅"}
# typo 是舊版（容許小拼錯、算對）留下的紀錄；現在改成嚴格判分，不會再產生
RESULT_TEXT = {"correct": "答對", "spelling": "拼字錯誤", "wrong": "答錯", "skip": "跳過", "typo": "答對（舊版容許拼錯）"}
STATUS_TEXT = {"completed": "完成", "stopped": "中途結束", "timeout": "逾時自動結束",
               "abandoned": "異常中斷", "active": "進行中", "unscored": "未評分"}
CATEGORY_TEXT = {"Vocabulary": "查單字", "Sentence Building": "造句", "Speaking": "口說"}


def week_info(day):
    monday = day - datetime.timedelta(days=day.weekday())
    next_monday = monday + datetime.timedelta(days=7)
    year, week, _ = monday.isocalendar()
    return {
        "label": f"{year}-W{week:02d}",
        "monday": monday,
        "sunday": next_monday - datetime.timedelta(days=1),
        "start": f"{monday} 00:00:00",
        "end": f"{next_monday} 00:00:00",  # 不含
    }


def weeks_with_data(connect, user_id):
    """這位使用者所有有資料的週（以週一日期表示），用來一次補齊歷史週報。"""
    queries = [
        "SELECT DISTINCT substr(started_at, 1, 10) FROM quiz_sessions WHERE user_id = ?",
        "SELECT DISTINCT substr(started_at, 1, 10) FROM exercise_sessions WHERE user_id = ?",
        "SELECT DISTINCT substr(started_at, 1, 10) FROM speaking_sessions WHERE user_id = ?",
        "SELECT DISTINCT substr(timestamp, 1, 10) FROM training_logs WHERE user_id = ?",
        "SELECT DISTINCT substr(ts, 1, 10) FROM skill_history WHERE user_id = ?",
        "SELECT DISTINCT substr(ts, 1, 10) FROM api_calls WHERE user_id = ?",
    ]
    days = set()
    with closing(connect()) as conn:
        for q in queries:
            days.update(r[0] for r in conn.execute(q, (str(user_id),)) if r[0])
    mondays = set()
    for d in days:
        day = datetime.date.fromisoformat(d)
        mondays.add(day - datetime.timedelta(days=day.weekday()))
    return sorted(mondays)


def fmt_changes(changes):
    if not changes:
        return "未調整（答題數不足）"
    return "、".join(f"{skills.SKILL_NAMES[s]} {a:.1f} → {b:.1f}" for s, (a, b) in changes.items())


def pct(num, den):
    return f"{num / den:.0%}" if den else "-"


def cell(text):
    return str(text if text not in (None, "") else "（無）").replace("|", "\\|").replace("\n", " ")


def short_time(started, ended):
    end = f" ~ {ended[11:16]}" if ended else ""
    return f"{started[5:16]}{end}"


def build_weekly(connect, day, usage_lines=None, user_id=None):
    w = week_info(day)
    user = str(user_id)
    with closing(connect()) as conn:
        quiz = conn.execute(
            "SELECT id, started_at, ended_at, total, answered, correct, skill_changes, status "
            "FROM quiz_sessions WHERE started_at >= ? AND started_at < ? AND user_id = ? ORDER BY started_at",
            (w["start"], w["end"], user),
        ).fetchall()
        exer = conn.execute(
            "SELECT id, kind, title, topic, passage, started_at, ended_at, total, answered, correct, "
            "skill_change, status, source_name, source_url, source_credit "
            "FROM exercise_sessions WHERE started_at >= ? AND started_at < ? "
            "AND user_id = ? ORDER BY started_at",
            (w["start"], w["end"], user),
        ).fetchall()
        speak = conn.execute(
            "SELECT id, topic, started_at, ended_at, total, answered, band, criteria, report, skill_change, status "
            "FROM speaking_sessions WHERE started_at >= ? AND started_at < ? AND user_id = ? ORDER BY started_at",
            (w["start"], w["end"], user),
        ).fetchall()
        speak_blocks = [_speaking_block(conn, s) for s in speak]
        logs = conn.execute(
            "SELECT timestamp, category, target_word, user_input, polished_sentence, synonyms, feedback, mastery "
            "FROM training_logs WHERE timestamp >= ? AND timestamp < ? AND user_id = ? AND "
            "(source IN ('word', 'submit', 'speak') OR (source IS NULL AND category IN ('Sentence Building', 'Speaking'))) "
            "ORDER BY timestamp",
            (w["start"], w["end"], user),
        ).fetchall()
        mistakes = conn.execute(
            "SELECT a.word, COALESCE(a.expected, a.word), a.user_answer, a.result FROM quiz_answers a "
            "JOIN quiz_sessions s ON s.id = a.session_id "
            "WHERE s.started_at >= ? AND s.started_at < ? AND s.user_id = ? "
            "AND a.result IN ('wrong', 'spelling', 'skip')",
            (w["start"], w["end"], user),
        ).fetchall()
        skill_lines = []
        for s in skills.SKILLS:
            rows = conn.execute(
                "SELECT rating FROM skill_history WHERE user_id = ? AND skill = ? AND ts >= ? AND ts < ? ORDER BY id",
                (user, s, w["start"], w["end"]),
            ).fetchall()
            if not rows:
                skill_lines.append(f"- {skills.SKILL_NAMES[s]}：本週沒有練習")
                continue
            before = conn.execute(
                "SELECT rating FROM skill_history WHERE user_id = ? AND skill = ? AND ts < ? "
                "ORDER BY id DESC LIMIT 1",
                (user, s, w["start"]),
            ).fetchone()
            start_rating = before[0] if before else rows[0][0]
            end_rating = rows[-1][0]
            skill_lines.append(
                f"- {skills.SKILL_NAMES[s]}：{start_rating:.1f} → {end_rating:.1f}（{end_rating - start_rating:+.1f}）"
            )

        quiz_blocks = [_quiz_block(conn, q) for q in quiz]

    read_n = sum(1 for e in exer if e[1] == "read")
    listen_n = sum(1 for e in exer if e[1] in ("listen", "listen_test"))
    q_answered = sum(q[4] or 0 for q in quiz)
    q_correct = sum(q[5] or 0 for q in quiz)
    counts = Counter(r[1] for r in logs)

    lines = [
        f"# 學習週報 {w['label']}（{w['monday']:%m/%d} ~ {w['sunday']:%m/%d}）",
        "",
        "## 本週總覽",
        "",
        f"- 單字測驗：{len(quiz)} 場，作答 {q_answered} 題，答對 {q_correct} 題（{pct(q_correct, q_answered)}）",
        f"- 閱讀練習：{read_n} 場｜聽力練習：{listen_n} 場｜口說模擬測驗：{len(speak)} 場",
        f"- 查單字：{counts['Vocabulary']} 次｜造句：{counts['Sentence Building']} 次｜口說：{counts['Speaking']} 次",
        "",
        "**評級變化（本週起點 → 終點）**",
        "",
        *skill_lines,
    ]

    # 本週需加強：答錯過的單字，以及拼字很接近卻不算對的情況
    if mistakes:
        wrong_count = Counter(m[1] for m in mistakes)
        lines += ["", "## 本週需加強", "", "| 正確答案 | 失誤次數 | 你打過的錯誤答案 |", "| --- | --- | --- |"]
        for expected, n in wrong_count.most_common(15):
            typed = sorted({m[2] for m in mistakes if m[1] == expected and m[2]})
            lines.append(f"| {expected} | {n} | {cell('、'.join(typed))} |")

    lines += ["", "## 單字測驗", ""]
    lines += [line for block in quiz_blocks for line in block] or ["本週沒有單字測驗。", ""]

    lines += ["## 閱讀與聽力", ""]
    if exer:
        for e in exer:
            lines += _exercise_block(connect, e)
    else:
        lines += ["本週沒有閱讀或聽力練習。", ""]

    lines += ["## 口說模擬測驗", ""]
    lines += [line for block in speak_blocks for line in block] or ["本週沒有口說模擬測驗。", ""]

    lines += ["## 查單字、造句、口說", ""]
    if logs:
        for ts, category, word, user_input, polished, synonyms, feedback, mastery in logs:
            lines.append(f"- **{ts[5:16]} {CATEGORY_TEXT.get(category, category)}｜{word}**（掌握度 {mastery}）")
            if category != "Vocabulary" and user_input:
                lines.append(f"  - 你的內容：{user_input}")
            if polished:
                lines.append(f"  - {'升級句' if category != 'Vocabulary' else '例句'}：{polished}")
            if synonyms:
                lines.append(f"  - 同義字：{synonyms}")
            if feedback:
                lines.append(f"  - 回饋：{feedback}")
        lines.append("")
    else:
        lines += ["本週沒有紀錄。", ""]

    if usage_lines:
        lines += ["## API 用量", ""] + usage_lines
    return "\n".join(lines).rstrip() + "\n"


def _quiz_block(conn, q):
    sid, started, ended, total, answered, correct, changes, status = q
    answers = conn.execute(
        "SELECT seq, word, COALESCE(expected, word), question, user_answer, result, explanation, synonyms, examples "
        "FROM quiz_answers WHERE session_id = ? ORDER BY seq",
        (sid,),
    ).fetchall()
    lines = [
        f"### 單字測驗 #{sid}｜{short_time(started, ended)}",
        "",
        f"- 狀態：{STATUS_TEXT.get(status, status)}（共 {total} 題，作答 {answered or 0} 題）",
        f"- 成績：{correct or 0}/{answered or 0}（{pct(correct or 0, answered or 0)}）",
        f"- 評級變化：{fmt_changes(json.loads(changes) if changes else {})}",
        "",
        "| # | 結果 | 正確答案 | 你的答案 |",
        "| --- | --- | --- | --- |",
    ]
    for seq, _, expected, _, user_answer, result, *_ in answers:
        lines.append(f"| {seq} | {RESULT_ICON.get(result, '')} {RESULT_TEXT.get(result, result)} | {expected} | {cell(user_answer)} |")

    if answers:
        lines += ["", "<details><summary>逐題詳細</summary>", ""]
        for seq, word, expected, question, user_answer, result, explanation, synonyms, examples in answers:
            form = f"（原形：{word}）" if word != expected else ""
            lines += [
                f"**{seq}. {RESULT_ICON.get(result, '')} {expected}** {form}",
                "",
                f"- 題目：{question.replace(chr(10), ' ')}",
                f"- 你的答案：{user_answer or '（無）'}",
                f"- 解釋：{explanation or '-'}",
                f"- 同義字：{synonyms or '-'}",
            ]
            example_list = json.loads(examples) if examples else []
            if example_list:
                lines.append("- 例句：")
                lines += [f"  {i}. {e}" for i, e in enumerate(example_list, 1)]
            lines.append("")
        lines += ["</details>", ""]
    else:
        lines.append("")
    return lines


CRITERIA_TEXT = {"fluency": "流暢連貫", "lexical": "詞彙", "grammar": "文法", "pronunciation": "發音"}


def _speaking_block(conn, s):
    sid, topic, started, ended, total, answered, band, criteria, report, change, status = s
    answers = conn.execute(
        "SELECT seq, part, question, seconds, skipped, transcript FROM speaking_answers "
        "WHERE session_id = ? ORDER BY seq",
        (sid,),
    ).fetchall()
    crit = json.loads(criteria) if criteria else {}
    crit_text = "｜".join(f"{CRITERIA_TEXT.get(k, k)} {v}" for k, v in crit.items() if v is not None)
    lines = [
        f"### 口說模擬測驗 #{sid}：{topic}｜{short_time(started, ended)}",
        "",
        f"- 狀態：{STATUS_TEXT.get(status, status)}（共 {total} 題，作答 {answered or 0} 題）",
        f"- 整體估分：{f'band {band:.1f}' if band else '未評分'}" + (f"（{crit_text}）" if crit_text else ""),
        f"- 評級變化：{fmt_changes(json.loads(change) if change else {})}",
        "",
        "| # | Part | 題目 | 狀態 | 長度 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for seq, part, question, seconds, skipped, _ in answers:
        lines.append(
            f"| {seq} | {part} | {cell(question)} | {'跳過' if skipped else '已回答'} | "
            f"{f'{seconds:.0f} 秒' if seconds else '-'} |"
        )
    if any(a[5] for a in answers):
        lines += ["", "<details><summary>逐字稿</summary>", ""]
        for seq, part, question, _, skipped, transcript in answers:
            if transcript:
                lines += [f"**{seq}. (Part {part}) {question}**", "", f"> {transcript}", ""]
        lines += ["</details>"]
    if report:
        lines += ["", "<details><summary>考官評語</summary>", "", report, "", "</details>"]
    lines.append("")
    return lines


def _exercise_block(connect, e):
    sid, kind, title, topic, passage, started, ended, total, answered, correct, change, status, \
        src_name, src_url, src_credit = e
    name = "閱讀" if kind == "read" else "聽力"
    with closing(connect()) as conn:
        answers = conn.execute(
            "SELECT seq, question, options, correct_answer, user_answer, result, explanation, difficulty, evidence "
            "FROM exercise_answers WHERE session_id = ? ORDER BY seq",
            (sid,),
        ).fetchall()
    lines = [
        f"### {name}練習 #{sid}：{title}｜{short_time(started, ended)}",
        "",
        f"- 主題：{topic or '自選'}｜狀態：{STATUS_TEXT.get(status, status)}（共 {total} 題，作答 {answered or 0} 題）",
        f"- 成績：{correct or 0}/{answered or 0}（{pct(correct or 0, answered or 0)}）",
        f"- 評級變化：{fmt_changes(json.loads(change) if change else {})}",
        (f"- 文章來源：改編自 {src_credit}（{src_url}）" if src_url
         else f"- 文章來源：依 {src_credit} 重新撰寫的原創文章" if src_name
         else "- 文章來源：原創（Gemini 撰寫）" if kind == "read" else ""),
        "",
        "<details><summary>" + ("原文" if kind == "read" else "聽力原文") + "</summary>",
        "",
        passage,
        "",
        "</details>",
        "",
        "| # | 結果 | 題目 | 正確答案 | 你的答案 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for seq, question, _, correct_answer, user_answer, result, _, _, _ in answers:
        lines.append(
            f"| {seq} | {RESULT_ICON.get(result, '')} {RESULT_TEXT.get(result, result)} | "
            f"{cell(question)} | {cell(correct_answer)} | {cell(user_answer)} |"
        )
    if answers:
        lines += ["", "<details><summary>逐題解析</summary>", ""]
        for seq, question, options, correct_answer, user_answer, result, explanation, difficulty, evidence in answers:
            lines += [f"**{seq}. {RESULT_ICON.get(result, '')} {question}**", ""]
            option_list = json.loads(options) if options else []
            lines += [f"- {letter}. {opt}" for letter, opt in zip("ABCD", option_list)]
            lines += [
                f"- 正確答案：{correct_answer}｜你的答案：{user_answer or '（無）'}",
                f"- 題目難度：{f'band {difficulty:.1f}' if difficulty else '-'}｜解析：{explanation or '-'}",
                *([f"- 原文依據：{evidence}"] if evidence else []),
                "",
            ]
        lines += ["</details>"]
    lines.append("")
    return lines


def write_weekly(connect, day, out_dir, usage_lines=None, user_id=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly_{week_info(day)['label']}.md"
    path.write_text(build_weekly(connect, day, usage_lines, user_id), encoding="utf-8")
    return path
