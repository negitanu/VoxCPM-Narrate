import unittest

from voxcpm_narrate.pronunciation import find_unknown_words


class PronunciationTests(unittest.TestCase):
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
