"""Core helpers: text comparison, draft gating, geometry, model configuration.
(Levels, routes, hints and the safety gate are in test_routes.py.)"""
import unittest

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import drafts, examples, geometry, llm
from banxing.geometry import Rect
from banxing.textutil import changed_chars, clean, fingerprint, keywords, loose, redact, strict

FURNITURE = examples.get("furniture")["draft"]
SOFTWARE = examples.get("software")["draft"]


class TextTests(unittest.TestCase):
    def test_clean_removes_what_editors_add(self):
        self.assertEqual(clean("  做个柜子​￼\r\n\r\n"), "做个柜子")
        self.assertEqual(clean(None), "")

    def test_strict_ignores_whitespace_only(self):
        self.assertEqual(strict("做个  柜子\n\n带抽屉 "), strict("做个 柜子 带抽屉"))
        self.assertNotEqual(strict("做个柜子。"), strict("做个柜子"))
        self.assertEqual(fingerprint("a  b"), fingerprint(" a b\n"))

    def test_loose_ignores_case_width_and_punctuation(self):
        self.assertEqual(loose("Ｄｉｙ 一个柜子！"), loose("diy一个柜子"))

    def test_changed_chars(self):
        self.assertEqual(changed_chars("abc", "abc"), 0)
        self.assertEqual(changed_chars("abc", "abd"), 2)
        self.assertEqual(changed_chars("abc", "abcde"), 2)

    def test_keywords_mix_latin_words_and_cjk_bigrams(self):
        terms = keywords("把 preview.html 的标题改成深蓝")
        self.assertIn("preview.html", terms)
        self.assertIn("标题", terms)
        self.assertIn("深蓝", terms)

    def test_redact_masks_credentials_and_reports_the_count(self):
        text, count = redact("用这个 sk-abcdefghijklmnopqrstuvwx 调接口，password: hunter2secret，其他照旧")
        self.assertEqual(count, 2)
        self.assertNotIn("abcdefghijkl", text)
        self.assertNotIn("hunter2secret", text)
        self.assertIn("其他照旧", text)
        self.assertEqual(redact(FURNITURE), (FURNITURE, 0))


class DraftTests(unittest.TestCase):
    def test_what_is_worth_analysing(self):
        self.assertEqual(drafts.analyzable(FURNITURE), (True, ""))
        self.assertEqual(drafts.analyzable("build a photo sorting tool"), (True, ""))
        self.assertEqual(drafts.analyzable("   "), (False, "empty"))
        self.assertEqual(drafts.analyzable("继续"), (False, "short"))
        self.assertEqual(drafts.analyzable("ok thanks"), (False, "short"))
        self.assertEqual(drafts.analyzable("/review 最近的改动有没有问题"), (False, "command"))
        self.assertEqual(drafts.analyzable("做" * 4000), (False, "long"))

    def test_tidying_is_not_a_new_draft_but_a_new_condition_is(self):
        self.assertFalse(drafts.substantial_change(FURNITURE, FURNITURE.replace("。", "！")))
        self.assertFalse(drafts.substantial_change(FURNITURE, FURNITURE.replace("花架", "花架子")))
        self.assertTrue(drafts.substantial_change(FURNITURE, FURNITURE + "家里没有电钻和锯子。"))
        self.assertTrue(drafts.substantial_change(FURNITURE, SOFTWARE))
        self.assertTrue(drafts.substantial_change("", FURNITURE))


class GeometryTests(unittest.TestCase):
    screen = Rect(0, 0, 1920, 1080)

    def test_pet_sits_outside_the_window_edge_when_there_is_room(self):
        window, box = Rect(200, 100, 1400, 900), Rect(300, 800, 1300, 860)
        pet = geometry.pet_rect(window, self.screen, (64, 64), box=box)
        self.assertGreaterEqual(pet.left, window.right)
        self.assertLessEqual(pet.bottom, box.top)
        self.assertTrue(pet.inside(self.screen))

    def test_pet_moves_inside_a_maximised_window_and_never_covers_the_box(self):
        box = Rect(300, 960, 1700, 1040)
        pet = geometry.pet_rect(self.screen, self.screen, (64, 64), box=box)
        self.assertTrue(pet.inside(self.screen))
        self.assertEqual(pet.overlap(box), 0)

    def test_dragged_position_follows_the_window(self):
        window = Rect(200, 100, 1400, 900)
        placed = Rect.at(1000, 500, 64, 64)
        offset = geometry.offset_from(placed, window)
        self.assertEqual(geometry.pet_rect(window, self.screen, (64, 64), offset), placed)
        moved = window.moved(100, 50)
        self.assertEqual(geometry.pet_rect(moved, self.screen, (64, 64), offset), placed.moved(100, 50))

    def test_bubble_opens_above_the_pet_and_avoids_the_input_box(self):
        box = Rect(300, 800, 1300, 860)
        send = Rect(1240, 810, 1290, 850)
        pet = Rect.at(1320, 720, 64, 64)
        bubble, side = geometry.place_bubble(pet, (420, 200), self.screen, [box, send])
        self.assertEqual(side, "above")
        self.assertEqual(bubble.overlap(box) + bubble.overlap(send) + bubble.overlap(pet), 0)
        self.assertTrue(bubble.inside(self.screen))

    def test_bubble_finds_another_side_when_above_is_off_screen(self):
        pet = Rect.at(1800, 10, 64, 64)
        box = Rect(300, 800, 1300, 860)
        bubble, side = geometry.place_bubble(pet, (420, 200), self.screen, [box])
        self.assertNotEqual(side, "above")
        self.assertTrue(bubble.inside(self.screen))
        self.assertEqual(bubble.overlap(pet), 0)

    def test_bubble_takes_the_least_bad_spot_when_nothing_is_clean(self):
        tiny = Rect(0, 0, 500, 300)
        pet = Rect.at(218, 118, 64, 64)
        bubble, _ = geometry.place_bubble(pet, (420, 200), tiny, [Rect(0, 250, 500, 300)])
        self.assertTrue(bubble.inside(tiny))


class ModelConfigTests(unittest.TestCase):
    def test_endpoint_rules(self):
        llm.check_base_url("https://api.example.com/v1")
        llm.check_base_url("http://127.0.0.1:11434/v1")
        for bad in ("http://api.example.com", "ftp://x", "https://user:pw@host", "not a url"):
            with self.assertRaises(ValueError):
                llm.check_base_url(bad)

    def test_readiness_and_host(self):
        self.assertTrue(llm.ModelConfig("mock").ready)
        self.assertFalse(llm.ModelConfig("anthropic").ready)
        config = llm.ModelConfig("openai", base_url="https://gateway.example.com/v1/", api_key="k")
        self.assertTrue(config.ready)
        self.assertEqual(config.host, "gateway.example.com")
        self.assertEqual(llm.ModelConfig("anthropic", api_key="k").host, "api.anthropic.com")
        self.assertEqual(llm.ModelConfig("anthropic", api_key="k").model_name, "claude-fable-5-2")

    def test_plain_http_to_a_remote_host_is_refused_before_any_request(self):
        config = llm.ModelConfig("openai", base_url="http://gateway.example.com/v1", api_key="k")
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(config, "s", "u")
        self.assertEqual(caught.exception.kind, "config")


if __name__ == "__main__":
    unittest.main()
