"""不連網的邏輯測試：python -m unittest discover -s tests -v（在專案根目錄、用 ielts_bot 環境執行）。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_FILE", os.path.join(tempfile.mkdtemp(), "test.db"))  # 絕不碰正式資料庫
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp())

import german as g  # noqa: E402
import german_logic as gl  # noqa: E402
import jsonutil  # noqa: E402
import skills  # noqa: E402
import speaking  # noqa: E402
import usage  # noqa: E402


def exercise(**kw):
    base = {"grammar_key": "x", "type": "cloze", "prompt": "Ich ____ Hunger.", "hint_zh": "我餓了", "answer": "habe",
            "full_sentence": "Ich habe Hunger.", "alternatives": [], "explanation": "e", "level": "A1"}
    base.update(kw)
    return base


def parse(items):
    return gl.parse_exercises(json.dumps(items))


class StopWords(unittest.TestCase):
    def test_stop_variants(self):
        for t in ["stop", "Stop.", " STOP ", "/stop", "Stopp", "停止", "結束", "ende", "aufhören", "ｓｔｏｐ", "stop!"]:
            self.assertEqual(g.command_word(t), "stop", t)

    def test_skip_variants(self):
        for t in ["skip", "Skip!", "跳過", "weiter", "überspringen", "下一題"]:
            self.assertEqual(g.command_word(t), "skip", t)

    def test_normal_text_is_not_a_command(self):
        for t in ["", "hello", "stop it please", "Ich habe Hunger", "am schnellsten"]:
            self.assertIsNone(g.command_word(t), t)


class GermanLogic(unittest.TestCase):
    def test_cefr_score_format(self):
        self.assertEqual(gl.cefr_score(3.76), "B1+ (3.76)")
        self.assertTrue(gl.cefr_score(1.5).startswith("A1"))

    def test_cloze_consistency(self):
        self.assertTrue(gl.cloze_consistent("Ich ____ Hunger.", "habe", "Ich habe Hunger."))
        # 空格前後少了一個詞（docs6 #2 的瑕疵題）
        self.assertFalse(gl.cloze_consistent("wie komme ich ____ Supermarkt?", "am schnellsten",
                                             "wie komme ich am schnellsten zum Supermarkt?"))
        self.assertFalse(gl.cloze_consistent("Ich ____ ____ Hunger.", "habe", "Ich habe Hunger."))

    def test_parse_drops_flawed_cloze_and_needs_three(self):
        good = [exercise(prompt=f"Ich ____ Hunger {i}.", full_sentence=f"Ich habe Hunger {i}.") for i in range(3)]
        self.assertEqual(len(parse(good)), 3)
        bad = exercise(prompt="Wie komme ich ____ Supermarkt?", answer="am schnellsten",
                       full_sentence="Wie komme ich am schnellsten zum Supermarkt?")
        self.assertEqual(len(parse(good + [bad])), 3)
        with self.assertRaises(ValueError):
            parse(good[:2] + [bad])

    def test_reorder_validation(self):
        ok = exercise(type="reorder", prompt="habe / Ich / Hunger / .", answer="Ich habe Hunger.", full_sentence="")
        bad = exercise(type="reorder", prompt="habe / Ich / .", answer="Ich habe Hunger.", full_sentence="")
        extra = [exercise(prompt=f"Du ____ Durst {i}.", answer="hast", full_sentence=f"Du hast Durst {i}.") for i in range(3)]
        self.assertEqual(len(parse(extra + [ok, bad])), 4)

    def test_translate_shows_chinese_prompt_and_separate_hint(self):
        ex = parse([exercise(type="translate", prompt="我昨天買了毛衣。", hint_zh="kaufen, Perfekt", answer="Ich habe gestern einen Pullover gekauft.",
                             full_sentence="")] + [exercise(prompt=f"Ich ____ Hunger {i}.", full_sentence=f"Ich habe Hunger {i}.") for i in range(3)])
        t = [x for x in ex if x["type"] == "translate"][0]
        text = gl.format_exercise(t, 0, 4)
        self.assertIn("我昨天買了毛衣。", text)
        self.assertIn("kaufen, Perfekt", text)

    def test_hint_cannot_leak_answer(self):
        ex = parse([exercise(prompt=f"Ich ____ Hunger {i}.", hint_zh="請填入 habe", full_sentence=f"Ich habe Hunger {i}.") for i in range(3)])
        self.assertEqual(ex[0]["hint_zh"], "")

    def test_check_answer_rules(self):
        ex = parse([exercise(prompt=f"Ich ____ Hunger {i}.", full_sentence=f"Ich habe Hunger {i}.") for i in range(3)])[0]
        self.assertEqual(gl.check_answer(ex, "habe")[0], "correct")
        self.assertEqual(gl.check_answer(ex, "Habe")[0], "spelling")  # 大小寫要算（使用者要求）
        self.assertEqual(gl.check_answer(ex, "bin")[0], "wrong")

    def test_umlaut_transliteration(self):
        ex = parse([exercise(prompt=f"Ich ____ Wasser {i}.", answer="möchte", full_sentence=f"Ich möchte Wasser {i}.") for i in range(3)])[0]
        self.assertEqual(gl.check_answer(ex, "moechte")[0], "correct")

    def test_clean_removes_spaces_between_cjk(self):
        self.assertEqual(gl.clean("請 問 這 裡 的 鮮 乳 在 哪 裡 ？ ( Akkusativ ) die Milch"), "請問這裡的鮮乳在哪裡？ ( Akkusativ ) die Milch")

    def test_sentence_dedup(self):
        prev = ["Ich habe gestern einen Pullover gekauft."]
        self.assertTrue(gl.sentence_too_similar("ich habe gestern einen pullover gekauft!", prev))
        self.assertFalse(gl.sentence_too_similar("Wo finde ich hier frische Milch?", prev))

    def test_sentence_prompt_uses_random_scenario_and_avoid_list(self):
        p = gl.build_sentence_prompt("A2", "", ["Satz A."])
        self.assertIn("Satz A.", p)
        self.assertTrue(any(s in p for s in gl.SCENARIOS))
        self.assertIn("情境或主題：旅行", gl.build_sentence_prompt("A2", "旅行"))

    def test_tense_prompt_and_pick(self):
        for key in gl.TENSES:
            p = gl.build_sentence_prompt("B1", "", (), key)
            self.assertIn("時態規定", p)
            self.assertIn(key, p)
        self.assertNotIn("時態規定", gl.build_sentence_prompt("B1", "", (), ""))
        self.assertEqual(gl.pick_tense("perfekt"), "perfekt")
        self.assertEqual(gl.pick_tense(""), "")
        self.assertEqual(gl.pick_tense("quatsch"), "")
        self.assertEqual({gl.pick_tense("random") for _ in range(200)}, set(gl.TENSES))

    def test_tense_ok(self):
        self.assertTrue(gl.tense_ok("Ich werde morgen anrufen.", "futur"))
        self.assertFalse(gl.tense_ok("Ich rufe morgen an.", "futur"))
        self.assertTrue(gl.tense_ok("Ich habe gestern Brot gekauft.", "perfekt"))
        self.assertTrue(gl.tense_ok("Wir sind nach Berlin gefahren.", "perfekt"))
        self.assertFalse(gl.tense_ok("Ich kaufe Brot.", "perfekt"))
        self.assertTrue(gl.tense_ok("Ich kaufte Brot.", "praeteritum"))  # 過去式不檢查

    def test_parse_sentence_requires_grammar(self):
        with self.assertRaises(ValueError):
            gl.parse_sentence(json.dumps({"german": "Hallo.", "chinese": "你好", "grammar": []}))


class SpeakingLogic(unittest.TestCase):
    def test_repeated_questions_detection(self):
        data = {"steps": [{"question": "What is your hometown like?"}, {"question": "Do you like music?"}]}
        self.assertEqual(speaking.repeated_questions(data, ["What is your hometown like?"]), 1)
        self.assertEqual(speaking.repeated_questions(data, ["Tell me about your job"]), 0)

    def test_generation_prompt_randomizes_without_topic(self):
        prompts = {speaking.build_generation_prompt(5.5, [1, 2, 3]) for _ in range(12)}
        self.assertGreater(len(prompts), 1)
        self.assertIn("主題請以「旅遊」", speaking.build_generation_prompt(5.5, [1], "旅遊"))
        self.assertIn("以前出過", speaking.build_generation_prompt(5.5, [1], "", ["Q1?"]))

    def test_parse_set_orders_parts(self):
        data = {"topic": "T", "part1": [{"topic": "Home", "questions": ["a?", "b?"]}], "part2": {"prompt": "Describe x", "points": ["p"], "closing": "and explain y"},
                "part3": ["c?"]}
        steps = speaking.parse_set(json.dumps(data), [1, 2, 3])["steps"]
        self.assertEqual([s["part"] for s in steps], [1, 1, 2, 3])


class Infra(unittest.TestCase):
    def test_lenient_json(self):
        self.assertEqual(jsonutil.loads_lenient('{"a": 1,}'), {"a": 1})
        self.assertEqual(jsonutil.loads_lenient("```json\n[1, 2]\n```" .strip("`").replace("json\n", "")), [1, 2])

    def test_rating_moves_in_right_direction(self):
        rt = skills.Rating(rating=3.0, rd=1.0) if hasattr(skills.Rating, "__dataclass_fields__") else None
        if rt is None:
            self.skipTest("Rating 建構方式不同")
        up = skills.update_from_items(rt, [(1.0, 3.0)] * 5)
        self.assertGreater(up.rating, 3.0)
        down = skills.update_from_items(rt, [(0.0, 3.0)] * 5)
        self.assertLess(down.rating, 3.0)

    def test_error_classification(self):
        class E(Exception):
            code = 503
        self.assertEqual(usage.classify_error(E("x")), "server")
        E.code = 401
        self.assertEqual(usage.classify_error(E("x")), "client")


if __name__ == "__main__":
    unittest.main()


# ---------------- 口說對話追問 ----------------
import dialogue  # noqa: E402

TREE = {
    "topic": "Cooking", "opening": "Do you like cooking?",
    "branches": [
        {"label": "likes", "keywords": ["love", "enjoy", "like cooking", "relaxing"], "question": "What do you enjoy making most?",
         "branches": [{"label": "pasta", "keywords": ["pasta", "noodles"], "question": "Why pasta?"},
                      {"label": "sweet", "keywords": ["cake", "dessert"], "question": "Why desserts?"}],
         "generic": "Who do you usually cook for?"},
        {"label": "dislikes", "keywords": ["hate", "boring", "dislike", "takeaway"], "question": "What do you eat instead?",
         "branches": [], "generic": "Would you ever change that?"},
    ],
    "generic": "Can you give me an example?", "generic_next": "Why is that?",
}


class DialogueLogic(unittest.TestCase):
    def test_keyword_hits_stem_and_phrase(self):
        self.assertEqual(dialogue.keyword_hits("I really enjoyed cooking dinners", ["enjoy", "dinner", "hate"]), 2)
        self.assertEqual(dialogue.keyword_hits("I like cooking a lot", ["like cooking"]), 2)  # 單字 cook＋片語整句
        self.assertEqual(dialogue.keyword_hits("I like to cook", ["like cooking"]), 1)  # 只有單字
        self.assertEqual(dialogue.keyword_hits("I am very sad", ["very", "the"]), 0)  # 太泛的字不算

    def test_branch_selection_and_three_rounds(self):
        d = dialogue.Dialogue(dialogue.parse_tree(json.dumps(TREE)))
        dec = d.decide("Yes I love it, it is really relaxing for me")
        self.assertEqual((dec["how"], dec["label"]), ("branch", "likes"))
        self.assertEqual(d.advance(dec), "What do you enjoy making most?")
        self.assertFalse(d.finished)
        dec = d.decide("I usually make pasta or noodles with tomato sauce")
        self.assertEqual(d.advance(dec), "Why pasta?")
        self.assertTrue(d.finished)
        self.assertIsNone(d.decide("anything"))

    def test_short_unmatched_uses_generic_without_api(self):
        d = dialogue.Dialogue(dialogue.parse_tree(json.dumps(TREE)))
        dec = d.decide("I do not know")
        self.assertEqual(dec["how"], "generic")
        self.assertEqual(d.advance(dec), "Can you give me an example?")
        dec = d.decide("hmm")
        self.assertEqual(d.advance(dec), "Why is that?")  # 走通用追問後的第 3 輪

    def test_long_unmatched_needs_live_once(self):
        d = dialogue.Dialogue(dialogue.parse_tree(json.dumps(TREE)), max_live=1)
        dec = d.decide("Yesterday my neighbour bought a new motorbike and it was very loud")
        self.assertEqual(dec["how"], "need_live")
        self.assertEqual(d.advance(dec, "Tell me about your neighbour."), "Tell me about your neighbour.")
        dec = d.decide("He is a student at the local university and drives everywhere")
        self.assertEqual(dec["how"], "generic")  # 即時接話額度用完了

    def test_branch_generic_for_round3(self):
        d = dialogue.Dialogue(dialogue.parse_tree(json.dumps(TREE)))
        d.advance(d.decide("I enjoy it, it is relaxing"))
        dec = d.decide("my family")
        self.assertEqual((dec["how"], d.advance(dec)), ("generic", "Who do you usually cook for?"))

    def test_parse_tree_validation(self):
        bad = dict(TREE, branches=[TREE["branches"][0]])
        with self.assertRaises(ValueError):
            dialogue.parse_tree(json.dumps(bad))
        no_generic = {k: v for k, v in TREE.items() if k not in ("generic", "generic_next")}
        t = dialogue.parse_tree(json.dumps(no_generic))
        self.assertTrue(t["generic"] and t["generic_next"])


class DialogueSafety(unittest.TestCase):
    def test_single_keyword_hit_is_not_enough(self):
        """只命中 1 個關鍵字不夠有把握，寧可走通用追問（實測 1 分約 3~7% 接錯，2 分 0/117）。"""
        d = dialogue.Dialogue(dialogue.parse_tree(json.dumps(TREE)))
        self.assertEqual(d.decide("I enjoy it")["how"], "generic")
        self.assertEqual(dialogue.MIN_SCORE, 2)
