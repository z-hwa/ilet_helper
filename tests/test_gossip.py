"""gossip 閒聊的純邏輯：解析、即時點評排版、報告。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gossip  # noqa: E402


def turn_json(**kw):
    data = {"transcript": "Ich habe gestern ins Kino gegangen.", "transcript_en": "I went to the cinema yesterday.",
            "status": "errors", "verdict": "很接近了！",
            "errors": [{"said": "habe gestern ins Kino gegangen", "fix": "bin gestern ins Kino gegangen", "why": "gehen 的完成式用 sein"}],
            "better": "Ich bin gestern ins Kino gegangen.", "pronunciation_note": "",
            "reply": "Oh, welchen Film hast du gesehen?", "reply_en": "Oh, which film did you see?"}
    data.update(kw)
    return json.dumps(data)


class Parse(unittest.TestCase):
    def test_opening(self):
        o = gossip.parse_opening(json.dumps({"topic": "Büro", "german": "Hallo!", "english": "Hi!"}))
        self.assertEqual(o["german"], "Hallo!")
        with self.assertRaises(ValueError):
            gossip.parse_opening(json.dumps({"topic": "x"}))

    def test_turn_with_errors(self):
        t = gossip.parse_turn(turn_json())
        self.assertEqual(t["status"], "errors")
        self.assertEqual(t["errors"][0]["fix"], "bin gestern ins Kino gegangen")

    def test_errors_status_without_errors_becomes_ok(self):
        self.assertEqual(gossip.parse_turn(turn_json(errors=[]))["status"], "ok")

    def test_identical_said_and_fix_is_not_an_error(self):
        errors = [{"said": "ins Kino gegangen", "fix": "ins Kino gegangen", "why": "x"},
                  {"said": "habe gestern ins Kino gegangen", "fix": "bin gestern ins Kino gegangen", "why": "sein"}]
        t = gossip.parse_turn(turn_json(errors=errors))
        self.assertEqual([e["said"] for e in t["errors"]], ["habe gestern ins Kino gegangen"])
        self.assertEqual(gossip.parse_turn(turn_json(errors=errors[:1]))["status"], "ok")

    def test_error_must_come_from_this_turns_transcript(self):
        other = {"said": "ich bin müde", "fix": "ich war müde", "why": "x"}  # 不在這一輪的逐字稿裡
        t = gossip.parse_turn(turn_json(errors=[other]))
        self.assertEqual(t["errors"], [])
        self.assertEqual(t["status"], "ok")
        self.assertEqual(len(gossip.parse_turn(turn_json())["errors"]), 1)

    def test_error_quote_with_ellipsis_is_accepted(self):
        e = {"said": "habe [...] gegangen", "fix": "bin [...] gegangen", "why": "sein"}
        self.assertEqual(len(gossip.parse_turn(turn_json(errors=[e]))["errors"]), 1)
        e = {"said": "habe … gegangen", "fix": "bin … gegangen", "why": "sein"}
        self.assertEqual(len(gossip.parse_turn(turn_json(errors=[e]))["errors"]), 1)

    def test_perfect_drops_errors(self):
        t = gossip.parse_turn(turn_json(status="perfect"))
        self.assertEqual(t["errors"], [])

    def test_unclear_gets_default_reply(self):
        t = gossip.parse_turn(json.dumps({"status": "unclear", "transcript": "", "reply": "", "reply_en": ""}))
        self.assertEqual(t["status"], "unclear")
        self.assertIn("noch einmal", t["reply"])

    def test_empty_transcript_is_unclear(self):
        self.assertEqual(gossip.parse_turn(turn_json(transcript=""))["status"], "unclear")

    def test_bad_status_or_missing_reply_raises(self):
        with self.assertRaises(ValueError):
            gossip.parse_turn(turn_json(status="great"))
        with self.assertRaises(ValueError):
            gossip.parse_turn(turn_json(reply=""))


class Format(unittest.TestCase):
    def test_feedback_hides_transcript_and_lists_errors(self):
        t = gossip.parse_turn(turn_json())
        text = gossip.format_turn(t, 2, 6)
        self.assertIn("第 2/6 回合", text)
        self.assertIn("||Ich habe gestern ins Kino gegangen.||", text)
        self.assertIn("「habe gestern ins Kino gegangen」→「**bin gestern ins Kino gegangen**」", text)
        self.assertIn("更自然的說法", text)
        self.assertNotIn("Oh, welchen Film", text)  # 老師的話在另一段，而且要遮擋

    def test_perfect_has_no_error_lines(self):
        text = gossip.format_turn(gossip.parse_turn(turn_json(status="perfect")), 1, 3)
        self.assertIn("文法正確", text)
        self.assertNotIn("更自然的說法", text)

    def test_teacher_text_is_spoilered_with_translation(self):
        text = gossip.format_teacher("Hallo || du", "Hi")
        self.assertIn("||Hallo  du||", text)
        self.assertIn("🌐 ||Hi||", text)

    def test_turn_text_differs_per_attempt(self):
        o = {"german": "Hallo!", "english": "Hi"}
        self.assertNotEqual(gossip.build_turn_text(o, [], 1), gossip.build_turn_text(o, [], 2))

    def test_system_mentions_last_round(self):
        self.assertIn("最後一回合", gossip.turn_system("A2", "x", 3, 3))
        self.assertNotIn("最後一回合", gossip.turn_system("A2", "x", 1, 3))


class Report(unittest.TestCase):
    def test_report_has_scores_suggestions_and_spoiler_transcripts(self):
        t = gossip.parse_turn(turn_json())
        t.update(seq=1, teacher_de=t.pop("reply"), teacher_en=t.pop("reply_en"))
        opening = {"seq": 0, "teacher_de": "Hallo!", "teacher_en": "Hi!", "transcript": "", "transcript_en": ""}
        data = {"level_estimate": "A2+", "summary": "不錯", "criteria": {"pronunciation": 3, "vocabulary": 3, "grammar": 2.5, "fluency": 3},
                "criteria_notes": {"grammar": "完成式助動詞"}, "priorities": ["sein/haben"],
                "suggestions": [{"seq": 1, "original": "habe gestern ins Kino gegangen", "better": "bin gestern ins Kino gegangen", "why": "sein"}]}
        built = gossip.build_report(data, [opening, t])
        self.assertIn("A2+", built["messages"][0])
        self.assertIn("文法｜2.5", built["messages"][0])
        self.assertTrue(any("第 1 回合" in m and "bin gestern ins Kino gegangen" in m for m in built["messages"]))
        self.assertEqual([i["seq"] for i in built["items"]], [0, 1])
        joined = "\n".join(built["items"][1]["texts"])
        self.assertIn("你：||Ich habe gestern ins Kino gegangen.||", joined)
        self.assertIn("🌐 ||I went to the cinema yesterday.||", joined)
        self.assertIn("Lena：||Oh, welchen Film hast du gesehen?||", joined)
        self.assertIn("逐字稿", "\n".join(built["items"][0]["texts"]))  # 標題在第一組
        self.assertNotIn("||", built["markdown"])  # 週報用文字不含劇透標記

    def test_final_text_lists_errors_and_notes(self):
        t = gossip.parse_turn(turn_json(pronunciation_note="ch 發成 k"))
        t.update(seq=1, teacher_de="x", teacher_en="y")
        text = gossip.build_final_text("A2", "Büro", [t])
        self.assertIn("「habe gestern ins Kino gegangen」→「bin gestern ins Kino gegangen」", text)
        self.assertIn("ch 發成 k", text)


if __name__ == "__main__":
    unittest.main()
