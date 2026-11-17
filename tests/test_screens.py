"""Renders the shell's main states in real Tk windows and checks their layout.

Where Pillow can grab the screen, each state is also saved as a PNG for a human
to look at: into $BANXING_SHOTS if set, else <temp>/banxing-shots. On a headless
machine the whole module is skipped. This exercises Tk on whatever platform runs
the tests; it says nothing about focus behaviour next to a real Codex window.
"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import examples, levels as lv
from banxing.cli import build
from banxing.config import Settings
from banxing.context import ContextProvider
from banxing.controller import InlineExecutor
from banxing.geometry import Rect, union
from banxing.hosts.simulated import SimulatedCodex
from banxing.store import Store
from banxing.ui import strings

from test_shell import NO_DISPLAY

SHOTS = Path(os.environ.get("BANXING_SHOTS") or Path(tempfile.gettempdir()) / "banxing-shots")


def window_rect(window):
    return Rect.at(window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height())


@unittest.skipIf(NO_DISPLAY, f"Tk cannot open a window here ({NO_DISPLAY})")
class ScreenTests(unittest.TestCase):
    def setUp(self):
        from banxing.ui.app import PetApp
        strings.set_language("zh")
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-screens-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.settings = Settings(self.folder, environ={"BANXING_PROVIDER": "mock"})
        self.codex = SimulatedCodex()
        self.app = PetApp(self.settings, Store(self.folder), use_tray=False)
        self.addCleanup(self.app.destroy)
        screen = Rect(0, 0, self.app.root.winfo_screenwidth(), self.app.root.winfo_screenheight())
        # A Codex window that fits this screen, with the composer at its bottom.
        window = Rect(40, 30, min(screen.right - 140, 1100), min(screen.bottom - 60, 760))
        self.codex.window_rect = window
        self.codex.box_rect = Rect(window.left + 120, window.bottom - 110, window.right - 120, window.bottom - 50)
        self.codex.send_rect = Rect(window.left + 100, window.bottom - 130, window.right - 100, window.bottom - 30)
        empty = ContextProvider(sessions_dir=str(self.folder / "none"), use_appserver=False)
        self.controller, _, _ = build(self.settings, Store(self.folder), self.app, self.codex,
                                      lambda on_snapshot: InlineExecutor(), context=empty,
                                      capabilities=self.codex.capabilities())
        self.app.attach(self.controller, None)
        self.now = 50.0
        self.controller.clock = lambda: self.now

    def step(self, seconds=0.25, times=1):
        for _ in range(times):
            self.now += seconds
            self.controller.on_snapshot(self.codex.snapshot(self.now))
            self.app.root.update()

    def shot(self, name):
        """Save a picture of our own windows only — never the rest of the desktop."""
        try:
            from PIL import ImageGrab
            for _ in range(6):                    # let the display catch up with the last changes
                self.app.root.update()
                time.sleep(0.05)
            windows = [self.app.pet, self.app.bubble_window, self.app.toast] + list(self.app.dialog_windows)
            area = union(window_rect(w) for w in windows if w.winfo_viewable())
            if area is None:
                return
            SHOTS.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(max(0, area.left - 6), max(0, area.top - 6), area.right + 6, area.bottom + 6)).save(
                SHOTS / f"{name}.png")
        except Exception:
            pass                                  # screenshots are a convenience, not a requirement

    def test_bubble_sits_by_the_pet_and_clear_of_the_input_box(self):
        self.codex.type(examples.get("furniture")["draft"])
        self.step(times=9)
        app = self.app
        self.assertIsNotNone(app.bubble)
        pet, bubble = window_rect(app.pet), window_rect(app.bubble_window)
        self.assertEqual(bubble.overlap(self.codex.box_rect), 0)
        self.assertEqual(bubble.overlap(self.codex.send_rect), 0)
        self.assertEqual(bubble.overlap(pet), 0)
        self.assertEqual(pet.overlap(self.codex.box_rect), 0)
        self.assertGreater(bubble.width, 300)
        texts = self.labels(app.bubble_window)
        for expected in ("三条路线 · 选一条填入 Codex", "主线", "支线", "换一批", "先做纸样比一比", "拿到能打印的等大纸样", "先做单层练手",
                         "拿到单层的图纸和材料单", "买现成的来改", "拿到改装步骤和采购清单", "预览"):
            self.assertIn(expected, texts)
        self.shot("1-bubble")

        collapsed = bubble.height
        app._toggle_preview(0)
        app.root.update()
        opened = window_rect(app.bubble_window)
        self.assertGreater(opened.height, collapsed)
        self.assertEqual(opened.overlap(self.codex.box_rect), 0)
        texts = self.labels(app.bubble_window)
        self.assertTrue(any(text.startswith("适用：") for text in texts))
        self.assertTrue(any("拼起来就是花架的正面和侧面" in text.replace("\n", "") for text in texts))
        self.assertIn("填入 Codex", texts)
        self.assertIn("复制", texts)
        self.shot("2-preview")

        self.controller.pick(0)
        self.step()
        self.assertIsNone(app.bubble)
        self.assertFalse(app.bubble_window.winfo_viewable())
        self.assertTrue(self.codex.draft.startswith("想在阳台做一个三层的花架，我没做过木工。请先在这个项目里"))
        self.assertEqual(self.codex.sent, [])
        self.assertTrue(app.toast.winfo_viewable())            # "已填入 Codex…"
        self.shot("3-filled")

    def test_critical_reason_error_and_copy_offer_are_shown(self):
        app = self.app
        self.controller.set_level(lv.WALK)
        self.controller.example("software")
        self.step()
        self.assertIn("内置示例 · 不经过模型", self.labels(app.bubble_window))
        self.shot("4-example")
        self.controller.dismiss()
        self.step()

        app.status("error", "timeout")
        app.root.update()
        self.assertTrue(any("超时" in text for text in self.labels(app.toast)))
        self.shot("5-error")
        app.offer_copy("完整提示词", "failed")
        app.root.update()
        self.assertIn("复制提示词", self.labels(app.toast))
        pet, toast = window_rect(app.pet), window_rect(app.toast)
        self.assertEqual(toast.overlap(pet), 0)
        self.assertEqual(toast.grown(app.px(12)).overlap(pet) > 0, True,
                         f"the notice should sit next to the pet: pet {pet}, notice {toast}, "
                         f"pet_rect {app.pet_rect}, avoid {app._avoid()}")
        self.shot("6-offer-copy")

    def test_pet_hides_when_codex_is_not_in_front_unless_pinned(self):
        app = self.app
        self.step()
        self.assertTrue(app.pet.winfo_viewable())
        app.pinned = False
        self.codex.foreground = False
        self.step()
        self.assertFalse(app.pet.winfo_viewable())
        app.pinned = True
        self.step()
        self.assertTrue(app.pet.winfo_viewable())

    def test_greeting_shows_the_pet_before_codex_is_in_front(self):
        app = self.app
        app.pinned = False
        self.codex.foreground = False
        self.step()
        self.assertFalse(app.pet.winfo_viewable())
        app.greet(seconds=0.2)
        app.root.update()
        self.assertTrue(app.pet.winfo_viewable())
        self.assertTrue(any("伴行已启动" in text for text in self.labels(app.toast)))
        self.shot("8-greeting")
        time.sleep(0.3)
        app.root.update()
        self.step()
        self.assertFalse(app.pet.winfo_viewable())            # back to following: hidden until Codex is in front
        self.assertFalse(app.toast.winfo_viewable())

    def test_dragging_the_pet_is_remembered_relative_to_the_window(self):
        app = self.app
        self.step()
        start = app.pet_rect

        class Event:
            def __init__(self, x, y):
                self.x_root, self.y_root = x, y
        app._press(Event(start.left + 5, start.top + 5))
        app._motion(Event(start.left - 195, start.top - 95))
        app._release(Event(start.left - 195, start.top - 95))
        self.assertEqual(app.pet_rect, start.moved(-200, -100))
        app.root.update()
        self.assertEqual(window_rect(app.pet), app.pet_rect)       # the window really moved
        offset = self.settings.get("pet_offset")
        self.assertEqual(offset, [app.pet_rect.left - self.codex.window_rect.right,
                                  app.pet_rect.top - self.codex.window_rect.bottom])
        self.codex.window_rect = self.codex.window_rect.moved(30, 20)
        self.step()
        self.assertEqual(app.pet_rect, start.moved(-170, -80))
        self.step()
        self.assertEqual(window_rect(app.pet), app.pet_rect)       # and follows the Codex window

    def test_dialogs_open_and_the_model_dialog_enforces_the_rules(self):
        app = self.app
        self.settings._environ = {}                           # as on a fresh install
        self.controller.refresh_setup()
        app.open_model()
        app.open_paste()
        app.open_probe()
        app.root.update()
        self.assertEqual(app.dialogs, 3)
        self.shot("7-dialogs")
        for window in list(app.dialog_windows):
            window.destroy()
        app.root.update()
        self.assertEqual(app.dialogs, 0)

    def test_hints_bubble_adds_a_sentence_and_stays(self):
        app = self.app
        self.controller.set_level(lv.HINT)
        draft = "想做一个放在玄关的换鞋凳"
        self.codex.type(draft)
        self.step(times=9)
        self.assertEqual(app.bubble.kind, "hints")
        texts = self.labels(app.bubble_window)
        for expected in ("这稿还可以补一句 · 点一下加到末尾", "要三条完整路线"):
            self.assertIn(expected, texts)
        self.assertEqual(window_rect(app.bubble_window).overlap(self.codex.box_rect), 0)
        self.shot("9-hints")
        clause = app.bubble.hints[0].clause
        self.controller.apply_hint(0)
        self.step()
        self.assertEqual(self.codex.draft, draft + "。" + clause)
        self.assertEqual((len(app.bubble.hints), self.codex.sent), (2, []))

    def test_generation_two_dialogs_open(self):
        app = self.app
        for opener in (app.open_aboutme, app.open_calibration, app.open_evidence):
            opener()
            app.root.update()
        self.assertEqual(app.dialogs, 3)
        self.shot("10-dialogs-gen2")
        for window in list(app.dialog_windows):
            window.destroy()
        app.root.update()
        self.assertEqual(app.dialogs, 0)
        self.assertFalse(self.controller.calibrating)          # closing the wizard ends its special mode

    def test_a_caution_opens_the_preview_instead_of_filling(self):
        app = self.app
        self.codex.type(examples.get("furniture")["draft"])
        self.step(times=9)
        app.need_preview(1)
        app.root.update()
        self.assertTrue(any(text.startswith("适用：") for text in self.labels(app.bubble_window)))
        self.assertEqual(self.codex.draft, examples.get("furniture")["draft"])

    def test_the_question_about_speaking_less_has_two_answers(self):
        app = self.app
        self.step()
        app.ask("quieter")
        app.root.update()
        texts = self.labels(app.toast)
        self.assertTrue(app.toast.winfo_viewable())
        self.assertGreaterEqual(len(texts), 3, texts)
        self.shot("11-ask")

    def test_a_window_that_is_already_shown_really_moves(self):
        """Regression: a second placement before the first one's window event was
        handled used to be dropped, leaving the pet (and so the bubble) behind."""
        app = self.app
        first, second = Rect.at(500, 400, app.size, app.size), Rect.at(300, 200, app.size, app.size)
        app._place_pet(first)
        app.root.update_idletasks()
        app._place_pet(second)
        app.root.update()
        self.assertEqual(window_rect(app.pet), second)
        app._place_pet(first)
        app._place_pet(second)
        app._place_pet(first)
        app.root.update()
        self.assertEqual(window_rect(app.pet), first)

    def labels(self, window):
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "Label":
                    found.append(child.cget("text"))
                walk(child)
        walk(window)
        return found


if __name__ == "__main__":
    unittest.main()
