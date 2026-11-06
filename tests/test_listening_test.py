"""聽力模擬測驗的純邏輯測試（不連網）。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import listening_test as lt  # noqa: E402

FILLER = " ".join(["well"] * 320)


def make_data(questions, choices=(), segments=None):
    segments = segments or [{"speaker": "A", "text": "The customer name is B-R-O-W-N and the phone is 0 7 4 1 2. " + FILLER},
                            {"speaker": "B", "text": "We open at nine thirty and the fee is twelve pounds."}]
    return json.dumps({"title": "T", "context": "You will hear a conversation.", "segments": segments,
                       "choices": list(choices), "questions": questions})


def blank(answer, alts=()):
    return {"type": "blank", "question": "Name: ____", "answer": answer, "alternatives": list(alts), "evidence": "", "explanation": ""}


class ParseSection(unittest.TestCase):
    def test_keeps_blank_whose_answer_is_in_script_and_drops_invented_ones(self):
        qs = [blank("B-R-O-W-N")] * 5 + [blank("0 7 4 1 2")] * 4 + [blank("Smithers")]
        data = lt.parse_section(make_data(qs), 1)
        self.assertEqual(len(data["questions"]), 9)
        self.assertNotIn("Smithers", [q["answer"] for q in data["questions"]])

    def test_too_few_valid_questions_raises(self):
        with self.assertRaises(ValueError):
            lt.parse_section(make_data([blank("Nowhere")] * 10), 1)

    def test_short_script_raises(self):
        with self.assertRaises(ValueError):
            lt.parse_section(make_data([blank("brown")] * 10, segments=[{"speaker": "A", "text": "Too short."}]), 1)

    def test_match_needs_choices_and_valid_letter(self):
        qs = [blank("twelve")] * 6 + [{"type": "match", "question": "Room 1", "answer": "B"},
                                      {"type": "match", "question": "Room 2", "answer": "E"},
                                      {"type": "match", "question": "Room 3", "answer": "A"},
                                      {"type": "match", "question": "Room 4", "answer": "C"}]
        data = lt.parse_section(make_data(qs, choices=["a", "b", "c", "d"]), 2)
        self.assertEqual(len(data["questions"]), 9)  # E 超出 4 個選項
        with self.assertRaises(ValueError):
            lt.parse_section(make_data(qs, choices=[]), 2)

    def test_mcq_requires_three_or_four_options(self):
        good = {"type": "mcq", "question": "Q", "options": ["x", "y", "z"], "answer": "b"}
        bad = {"type": "mcq", "question": "Q", "options": ["x", "y"], "answer": "A"}
        data = lt.parse_section(make_data([good] * 8 + [bad] * 2), 3)
        self.assertEqual(len(data["questions"]), 8)
        self.assertEqual(data["questions"][0]["answer"], "B")


class Grading(unittest.TestCase):
    def test_blank_is_case_and_punctuation_insensitive_but_spelling_counts(self):
        q = {"type": "blank", "answer": "Brown", "alternatives": ["Browne"]}
        self.assertEqual(lt.check_answer(q, "brown."), "correct")
        self.assertEqual(lt.check_answer(q, "Browne"), "correct")
        self.assertEqual(lt.check_answer(q, "Braun"), "wrong")
        self.assertEqual(lt.check_answer(q, ""), "wrong")

    def test_numbers_and_articles(self):
        q = {"type": "blank", "answer": "12", "alternatives": []}
        self.assertEqual(lt.check_answer(q, "twelve"), "correct")
        q2 = {"type": "blank", "answer": "the library", "alternatives": []}
        self.assertEqual(lt.check_answer(q2, "library"), "correct")
        q3 = {"type": "blank", "answer": "0 7 4 1 2", "alternatives": []}
        self.assertEqual(lt.check_answer(q3, "07412"), "correct")  # 逐位與連寫視為同一個答案
        self.assertEqual(lt.check_answer(q3, "07413"), "wrong")

    def test_letters(self):
        q = {"type": "mcq", "answer": "C", "options": ["a", "b", "c"], "alternatives": []}
        for text in ("c", "C.", "(c)", "C smaller"):
            self.assertEqual(lt.check_answer(q, text), "correct", text)
        self.assertEqual(lt.check_answer(q, "A"), "wrong")


class ParseAnswers(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(lt.parse_answers("1 Brown\n2. B\nQ3: 12 pounds\nhello", 10), {1: "Brown", 2: "B", 3: "12 pounds"})

    def test_compact_letters(self):
        self.assertEqual(lt.parse_answers("1A 2b 3C", 10), {1: "A", 2: "B", 3: "C"})

    def test_out_of_range_and_garbage(self):
        self.assertEqual(lt.parse_answers("11 x\n0 y\nnothing here", 10), {})

    def test_several_answers_on_one_line(self):
        self.assertEqual(lt.parse_answers("1 word1 2 word2 3 word3", 10), {1: "word1", 2: "word2", 3: "word3"})
        self.assertEqual(lt.parse_answers("4 25 5 30", 10), {4: "25", 5: "30"})
        self.assertEqual(lt.parse_answers("1 2 3", 10), {1: "2 3"})  # 分不出來就當成第 1 題的答案
        self.assertEqual(lt.parse_answers("6 Green Street 7 Mr Lee\n8 14", 10), {6: "Green Street", 7: "Mr Lee", 8: "14"})

    def test_number_answer_not_mistaken_for_compact(self):
        self.assertEqual(lt.parse_answers("4 25", 10), {4: "25"})


class Speakers(unittest.TestCase):
    def test_speakers_a_to_d_kept_others_become_a(self):
        segs = [{"speaker": s, "text": "Hello there. " + FILLER} for s in ("A", "b", "C", "D", "E", "")]
        data = lt.parse_section(make_data([blank("hello")] * 10, segments=segs), 3)
        self.assertEqual([s["speaker"] for s in data["segments"]], ["A", "B", "C", "D", "A", "A"])

    def test_tts_has_four_distinct_voices_per_accent(self):
        import tts
        for accent, voices in tts.ACCENTS.items():
            self.assertEqual(set(voices), set("ABCDN"), accent)  # N = 旁白
            self.assertEqual(len(set(voices.values())), 5, accent)

    def test_speaker_legend_and_narration(self):
        segs = [{"speaker": s, "text": "Hello there. " + FILLER} for s in ("A", "B")]
        raw = json.loads(make_data([blank("hello")] * 10, segments=segs))
        raw["speakers"] = {"A": "Sarah, a customer", "B": "Tom, the receptionist", "C": "ignored"}
        data = lt.parse_section(json.dumps(raw), 1)
        self.assertEqual(data["speakers"], {"A": "Sarah, a customer", "B": "Tom, the receptionist"})
        self.assertIn("Sarah, a customer", lt.format_sheet(1, data))
        self.assertTrue(data["script"].startswith("👥"))
        n = lt.narration(1, data)
        self.assertEqual(n["speaker"], "N")
        self.assertIn("The speakers are Sarah, a customer; Tom, the receptionist.", n["text"])

    def test_single_speaker_has_no_legend(self):
        data = lt.parse_section(make_data([blank("hello")] * 10, segments=[{"speaker": "A", "text": "Hello there. " + FILLER}]), 4)
        self.assertNotIn("👥", lt.format_sheet(4, data))


class Evidence(unittest.TestCase):
    def test_list_evidence_is_flattened(self):
        q = {"type": "blank", "question": "N: ____", "answer": "Brown", "evidence": ["Sure, it is Brown.", "B-R-O-W-N."]}
        item = lt._clean_question(q, [])
        self.assertEqual(item["evidence"], "Sure, it is Brown. B-R-O-W-N.")


class Band(unittest.TestCase):
    def test_table(self):
        self.assertEqual(lt.raw_to_band(40, 40), 9.0)
        self.assertEqual(lt.raw_to_band(30, 40), 7.0)
        self.assertEqual(lt.raw_to_band(23, 40), 6.0)
        self.assertEqual(lt.raw_to_band(0, 40), 2.0)
        self.assertEqual(lt.raw_to_band(8, 10), lt.raw_to_band(32, 40))  # 單段按比例換算

    def test_difficulty_rises_with_section(self):
        self.assertLess(lt.difficulty(1, 6.0), lt.difficulty(4, 6.0))


class Display(unittest.TestCase):
    def test_sheet_and_results(self):
        qs = [blank("brown")] * 8
        data = lt.parse_section(make_data(qs), 1)
        data["questions"][0]["evidence"] = "x"
        sheet = lt.format_sheet(1, data)
        self.assertIn("NO MORE THAN", sheet)
        self.assertIn("**8.**", sheet)
        blocks = lt.result_blocks(data["questions"], {1: "brown", 2: "wrong"})
        self.assertIn("✅", blocks[0])
        self.assertIn("❌", blocks[1])
        self.assertIn("未作答", blocks[2])


if __name__ == "__main__":
    unittest.main()
