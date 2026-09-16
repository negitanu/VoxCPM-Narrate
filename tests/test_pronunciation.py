import unittest

from voxcpm_narrate.pronunciation import apply_dictionary, find_unknown_words


class PronunciationTests(unittest.TestCase):
    def test_partial_registered_term_does_not_hide_an_unregistered_occurrence(self):
        found = find_unknown_words("東京支社と東京です。", {"東京支社": "とうきょうししゃ"})
        self.assertIn("東京", [item["term"] for item in found])

    def test_covered_part_of_compound_is_not_reported_again(self):
        found = find_unknown_words("東京支社です。", {"東京": "とうきょう"})
        self.assertEqual([item["term"] for item in found], ["支社"])

    def test_latin_boundaries_and_nonrecursive_replacements(self):
        self.assertEqual(apply_dictionary("RAILとAIです。", {"AI": "エーアイ", "エーアイ": "禁止"}),
                         "RAILとエーアイです。")
    def test_extracts_terms_and_respects_dictionary(self):
        found = find_unknown_words("VoxCPMの東京支社で、音声合成APIを使います。", {"VoxCPM": "ボックスシーピーエム"})
        terms = [item["term"] for item in found]
        self.assertIn("東京支社", terms)
        self.assertIn("音声合成", terms)
        self.assertNotIn("VoxCPM", terms)

    def test_does_not_report_common_words_or_nested_dictionary_terms(self):
        found = find_unknown_words("内容を確認します。", {"内容": "ないよう"})
        self.assertEqual(found, [])

    def test_candidate_never_invents_a_reading(self):
        item = find_unknown_words("珍しい固有名詞です。", {})[0]
        self.assertEqual(item["reading"], "")
        self.assertEqual(item["status"], "review")


if __name__ == "__main__":
    unittest.main()
