"""量測「事先備好的分支」對自然回答的命中率與正確率。
Gemini 先出對話樹，再另外寫一批「像真人學習者」的回答並標註屬於哪個分支（約 10 次呼叫，結果存在 /tmp/measure_cache.json）；
之後加 --offline 可以不花 API 重新調整比對門檻。
執行：DB_FILE=/tmp/x.db LOG_DIR=/tmp/xl python tests/measure_matching.py [--offline]
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as core  # noqa: E402
import dialogue  # noqa: E402
import jsonutil  # noqa: E402
import usage  # noqa: E402

usage.current_user.set(4242)
CACHE = "/tmp/measure_cache.json"
TREES = int(os.getenv("TREES", "5"))


async def collect():
    core.init_db()
    cases = json.load(open(CACHE, encoding="utf-8")) if "--more" in sys.argv else []
    for _ in range(TREES):
        tree = dialogue.parse_tree(await core.ask_gemini(dialogue.build_tree_prompt(5.5, "", []), json_mode=True,
                                                          temperature=1.0, feature="speaking_dialogue_gen"))
        await asyncio.sleep(6)
        labels = [b["label"] for b in tree["branches"]]
        prompt = (
            f"考官問：{tree['opening']}\n請你扮演 9 位不同的英文學習者（口說約 band 5.5），各用 1~2 句口語、自然的英文回答，"
            f"每人的回答要屬於下列方向之一（每個方向 3 人）：{labels}。不要刻意使用書面或過於標準的詞，像真人隨口講的，"
            '有些人可以講得比較短或沒那麼切題。輸出 JSON 陣列：[{"label": 方向（必須和上面完全一樣）, "answer": 回答}]'
        )
        data = jsonutil.loads_lenient(await core.ask_gemini(prompt, json_mode=True, temperature=1.0, feature="other"))
        await asyncio.sleep(6)
        cases.append({"tree": tree, "answers": data})
    json.dump(cases, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return cases


def evaluate(cases, verbose=False):
    hit = right = wrong = total = 0
    for case in cases:
        branches = case["tree"]["branches"]
        if verbose:
            print(f"\n{case['tree']['opening']}  分支={[b['label'] for b in branches]}")
        for item in case["answers"]:
            b = dialogue.best_branch(item["answer"], branches)
            total += 1
            ok = bool(b) and b["label"] == item["label"]
            hit += bool(b)
            right += ok
            wrong += bool(b) and not ok
            if verbose:
                print(f"  {'✅' if ok else ('❌錯分支' if b else '－通用追問')} 應為[{item['label']}] 判為[{b['label'] if b else '-'}] "
                      f"{item['answer'][:80]}")
    return hit, right, wrong, total


if __name__ == "__main__":
    cases = json.load(open(CACHE, encoding="utf-8")) if "--offline" in sys.argv else asyncio.run(collect())
    for score, margin in [(1, 0), (1, 1), (2, 1), (2, 2), (3, 1)]:
        dialogue.MIN_SCORE, dialogue.MIN_MARGIN = score, margin
        hit, right, wrong, total = evaluate(cases)
        print(f"MIN_SCORE={score} MIN_MARGIN={margin}｜走分支 {hit}/{total}（正確 {right}、接錯 {wrong}）｜"
              f"接錯率 {wrong / total:.0%}｜正確率 {right / total:.0%}")
    dialogue.MIN_SCORE, dialogue.MIN_MARGIN = 2, 1
    evaluate(cases, verbose=True) if "--verbose" in sys.argv else None
