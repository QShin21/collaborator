"""The shell around the core: profile rules, view models, strings, threads, entry points.

The Tk tests run only where a display exists (they do on the Windows CI runner and
are skipped on a headless Linux box). Nothing here touches a real Codex window."""
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import cli, examples, follow, levels as lv, suggest
from banxing.controller import ThreadedExecutor
from banxing.geometry import Rect
from banxing.hosts import NullClient, create_client
from banxing.hosts.profile import BUTTON, DOCUMENT, EDIT, GROUP, BoxCandidate, Profile
from banxing.hosts.simulated import SimulatedCodex
from banxing.ui import strings, viewmodel

from rig import RigCase

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "banxing").rglob("*.py"))


class SourceTests(unittest.TestCase):
    def test_every_source_file_compiles(self):
        """Includes the Windows-only modules, which cannot be imported here."""
        for path in SOURCES + sorted((ROOT / "tests").glob("*.py")) + [ROOT / "run.py"]:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_python_39_syntax_only(self):
        """No match statements, no `X | Y` annotations evaluated at runtime."""
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*match .+:\s*$", path.name)
            if re.search(r"(?m)^(?:\s*def .*(?:: |-> )[\w\[\], ]+ \| |\s+\w+: [\w\[\], ]+ \| )", text):
                self.assertIn("from __future__ import annotations", text, path.name)

    def test_file_io_names_its_encoding(self):
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"(?:(?<![.\w])(?<!def )open\(|\.read_text\(|\.write_text\()", text):
                call = text[match.start(): match.start() + 300].split("\n\n")[0]
                if '"rb"' in call or '"wb"' in call:
                    continue
                self.assertIn("encoding=", call, f"{path.name}: {call[:80]}")

    def test_windows_modules_are_optional_elsewhere(self):
        if sys.platform == "win32":
            self.skipTest("on Windows these modules load for real")
        with self.assertRaises(ImportError):
            import banxing.hosts.win32  # noqa: F401
        client = create_client()
        self.assertIsInstance(client, NullClient)
        snap = client.snapshot(1.0)
        self.assertFalse(snap.foreground)
        self.assertFalse(client.fill(("w", "c", "b"), "a", "b").ok)

    def test_the_only_keys_ever_synthesised_cannot_send_a_message(self):
        """A guard on the one file that presses keys: no Enter, anywhere."""
        text = (ROOT / "banxing" / "hosts" / "win32.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"VK_RETURN|0x0D\b|VK_ENTER")
        self.assertIn("allowed = ((VK_CONTROL, VK_A), (VK_CONTROL, VK_V), (VK_DELETE,))", text)
        uia = (ROOT / "banxing" / "hosts" / "uia.py").read_text(encoding="utf-8")
        self.assertNotIn("win32.send_keys(", uia)
        self.assertEqual(uia.count("win32.send_shortcuts("), 2)


class DocumentTests(unittest.TestCase):
    """The papers that go out with the code say the same thing as the code."""

    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_the_ledger_has_a_row_for_every_work_package_of_the_first_release(self):
        ledger = self.read("docs/verification.md")
        plan = self.read("docs/banxing-generation2.md")
        packages = sorted(set(re.findall(r"(?m)^\| (M[012]-\d\d) \|", plan)))
        self.assertEqual(len(packages), 29)                           # 9 + 7 + 13, section 8.2
        for package in packages:
            self.assertRegex(ledger, rf"(?m)^\| {package} \|", package)

    def test_published_evidence_distinguishes_windows_runners_from_real_codex(self):
        """Runner E2 needs a linked record; it must never turn into a real-Codex claim."""
        # A record of a FAILED run is evidence too, and changes nothing here. Only a record
        # that itself claims a real-device level ends this check.
        records = [p.name for p in (ROOT / "docs" / "evidence").glob("EV-*.md")
                   if re.search(r"(?m)^证据等级：\s*E[3-5]", p.read_text(encoding="utf-8"))]
        if records:
            self.skipTest("real-device evidence has been recorded; review the wording by hand")
        for relative in ("README.md", "docs/verification.md", "packaging/windows/README.txt",
                         ".github/workflows/release.yml"):
            text = self.read(relative)
            self.assertNotRegex(text, r"真机通过（E3，Codex 版本 \d", relative)
            # English claims are only ever made in the negative.
            for claim in re.finditer(r"(?i)verified (?:on|against) a real codex", text):
                before = text[max(0, claim.start() - 40):claim.start()]
                self.assertRegex(before, r"(?i)\bnot\b|nobody|never", relative)
        windows_records = {p.name for p in (ROOT / "docs" / "evidence").glob("EV-*.md")
                           if re.search(r"(?m)^证据等级：\s*E2", p.read_text(encoding="utf-8"))}
        ledger = self.read("docs/verification.md")
        for line in ledger.splitlines():
            if line.startswith("| 真实"):
                self.assertNotRegex(line, r"\| E[2-5]\b", line[:40])
            if line.startswith("| M") or line.startswith("| Win32") or line.startswith("| 打包") or line.startswith("| Windows CI"):
                self.assertNotRegex(line, r"\| E[3-5]\b", line[:40])
                if "E2" in line:
                    linked = set(re.findall(r"evidence/(EV-\d{4}-\d+\.md)", line))
                    self.assertTrue(linked & windows_records, f"E2 needs a linked Windows record: {line[:60]}")
                    self.assertIn("运行器", line, "E2 must say it covers the runner, not real Codex")

    def test_the_old_names_are_gone_outside_the_history(self):
        old = ("desk" + "pet", "DESK" + "PET_", "AI-" + "Collaborator", "Desk" + "Pet")
        files = (SOURCES + sorted((ROOT / "tests").glob("*.py")) + sorted((ROOT / ".github").rglob("*.yml"))
                 + [ROOT / name for name in ("run.py", "start.bat", ".env.example", "README.md", "CLAUDE.md",
                                             "packaging/windows/README.txt", "docs/verification.md",
                                             "docs/acceptance.md")])
        for path in files:
            text = path.read_text(encoding="utf-8")
            for name in old:
                self.assertNotIn(name, text, f"{path.name}: {name}")

    def test_the_version_is_the_one_the_papers_talk_about(self):
        from banxing import __version__
        self.assertEqual(__version__, "0.3.0")
        self.assertIn(f"v{__version__}", self.read("README.md"))
        self.assertIn(f"v{__version__}", self.read("docs/verification.md"))


class ProfileTests(unittest.TestCase):
    profile = Profile.default()

    def box(self, **fields):
        base = dict(control_type=EDIT, framework="Chrome", has_value=True, has_text=True, width=700, height=60,
                    top=800)
        base.update(fields)
        return BoxCandidate(**base)

    def test_codex_is_recognised_by_its_process(self):
        self.assertTrue(self.profile.is_codex("Codex.exe", "Codex"))
        self.assertFalse(self.profile.is_codex("chrome.exe", "Codex – Chrome"))
        self.assertFalse(self.profile.is_codex("", ""))
        extra = Profile.default(extra_process_names=["Codex-Beta.EXE"])
        self.assertTrue(extra.is_codex("codex-beta.exe"))
        titled = Profile(process_names=("codex.exe",), title_pattern=r"^Codex")
        self.assertFalse(titled.is_codex("codex.exe", "Settings"))

    def test_environment_can_replace_the_process_list(self):
        old = os.environ.get("BANXING_PROCESS_NAMES")
        os.environ["BANXING_PROCESS_NAMES"] = "PowerShell.exe, pwsh.exe"
        try:
            self.assertEqual(Profile.default().process_names, ("powershell.exe", "pwsh.exe"))
        finally:
            if old is None:
                del os.environ["BANXING_PROCESS_NAMES"]
            else:
                os.environ["BANXING_PROCESS_NAMES"] = old

    def test_composer_is_accepted_and_small_or_single_line_fields_are_not(self):
        accepts = lambda **f: self.profile.accepts(self.box(**f), window_width=1200)    # noqa: E731
        self.assertTrue(accepts(aria="multiline=true;readonly=false", width=200))
        self.assertTrue(accepts())                                   # no hint, but wide
        self.assertFalse(accepts(aria="multiline=false"))            # search / rename <input>
        self.assertFalse(accepts(width=240))                         # narrow and unhinted
        self.assertFalse(accepts(read_only=True))
        self.assertFalse(accepts(has_value=False, has_text=False))
        self.assertFalse(accepts(control_type=BUTTON))
        self.assertTrue(accepts(control_type=GROUP, aria="multiline=true"))      # contenteditable container
        self.assertFalse(accepts(control_type=DOCUMENT, has_value=False))

    def test_pick_prefers_the_focused_then_the_lowest_box(self):
        search = self.box(aria="multiline=false", top=100, width=260)
        upper = self.box(top=300)
        lower = self.box(top=820)
        self.assertEqual(self.profile.pick([search, upper, lower], 1200), 2)
        self.assertEqual(self.profile.pick([search, self.box(top=300, focused=True), lower], 1200), 1)
        self.assertIsNone(self.profile.pick([search], 1200))
        self.assertIsNone(self.profile.pick([], 1200))

    def test_a_placeholder_is_not_a_draft(self):
        profile = Profile.default(placeholders=["Ask Codex anything"])
        self.assertEqual(profile.draft_from("Ask Codex anything\n"), "")
        self.assertEqual(profile.draft_from("描述一个任务", name="描述一个任务"), "")
        self.assertEqual(profile.draft_from("要求后续变更", help_text="要求后续变更"), "")
        self.assertEqual(profile.draft_from("做个柜子￼\n"), "做个柜子")
        self.assertEqual(profile.draft_from("  "), "")

    def test_only_a_real_address_counts_as_the_page_address(self):
        from banxing.hosts.profile import page_address
        for address in ("app://-/index.html#/thread/0199aaaa-1111-7222-8333-444455556666",
                        "file:///C:/Program%20Files/Codex/index.html", "https://example.com/c/abc"):
            self.assertEqual(page_address(address), address)
        for text in ("我想做个小工具", "stand-in draft 12345", "note: buy screws", "", None, "x:" + "y" * 3000):
            self.assertEqual(page_address(text), "")

    def test_rich_editors_are_pasted_into_and_native_boxes_get_a_value(self):
        self.assertEqual(self.profile.fill_order("Chrome"), ("paste", "value"))
        self.assertEqual(self.profile.fill_order("WinForm"), ("value", "paste"))
        self.assertEqual(self.profile.fill_order(""), ("value", "paste"))


class StringTests(unittest.TestCase):
    def test_every_string_exists_in_both_languages(self):
        for key, pair in strings.STRINGS.items():
            self.assertEqual(len(pair), 2, key)
            self.assertTrue(all(isinstance(text, str) and text for text in pair), key)
            self.assertEqual(set(re.findall(r"\{(\w+)\}", pair[0])), set(re.findall(r"\{(\w+)\}", pair[1])), key)

    def test_every_key_used_in_the_code_is_defined(self):
        used = set()
        for path in SOURCES:
            used.update(re.findall(r"""\bt\(\s*["']([a-z_]+)["']\s*[,)]""", path.read_text(encoding="utf-8")))
        self.assertGreater(len(used), 40)
        self.assertEqual(sorted(used - set(strings.STRINGS)), [])
        # Keys that are put together at run time.
        built = (["provider_" + name for name in ("anthropic", "openai", "mock")]
                 + ["example_" + name for name in examples.names()]
                 + ["model_error_" + code for code in ("provider", "base_url", "api_key", "api_key_again",
                                                       "env", "destination")]
                 + ["status_" + state for state in ("away", "idle", "thinking", "paused", "blind",
                                                    "uncalibrated", "stalled")]
                 + ["status_error_" + kind for kind in ("timeout", "invalid", "auth", "network", "redirect",
                                                        "effort", "unsafe", "halted", "other")]
                 + [key for level in lv.ORDER for key in ("level_" + level, "level_" + level + "_about")]
                 + ["kind_" + kind for kind in suggest.KINDS]
                 + ["cal_step_" + step for step, _, _ in __import__("banxing.calibration", fromlist=["x"]).STEPS]
                 + ["cap_" + cap for cap in ("read", "fill", "chat_key", "signals", "turn_events", "selection")]
                 + ["caution_" + code for code in ("privilege", "destructive", "other")]
                 + ["ask_" + what for what in ("quieter",)]
                 + ["notice_too_" + why for why in ("short", "long", "command", "empty")]
                 + ["offer_" + reason for reason in ("mismatch", "failed", "clipboard_busy", "keys_held")])
        self.assertEqual([key for key in built if key not in strings.STRINGS], [])

    def test_language_switch(self):
        self.addCleanup(strings.set_language, "zh")
        strings.set_language("en")
        self.assertEqual(strings.t("menu_quit"), "Quit")
        strings.set_language("zh")
        self.assertEqual(strings.t("menu_quit"), "退出")
        self.assertEqual(strings.t("bubble_get", deliverable="带公式的记账表"), "拿到带公式的记账表")
        self.assertEqual(strings.t("no_such_key"), "no_such_key")
        self.assertIn(strings.set_language("auto"), ("zh", "en"))


class ViewModelTests(RigCase):
    def setUp(self):
        strings.set_language("zh")

    def test_wrapping_keeps_words_whole_and_punctuation_off_line_starts(self):
        measure = lambda text: sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)     # noqa: E731
        text = "想做个 HTML 网页来记账，每天记几笔花销。月底要能看到按分类的汇总。"
        wrapped = viewmodel.wrap_text(text, measure, 20)
        lines = wrapped.split("\n")
        self.assertEqual(wrapped.replace("\n", "").replace(" ", ""), text.replace(" ", ""))
        self.assertTrue(lines[0].startswith("想做个 HTML 网页"))         # no break right after a space
        self.assertTrue(all(measure(line) <= 22 for line in lines), lines)
        self.assertFalse(any(line[:1] in "，。" for line in lines))
        self.assertTrue(all("HTM" not in line or "HTML" in line for line in lines))
        self.assertEqual(viewmodel.wrap_text("第一行\n\n第三行", measure, 40), "第一行\n\n第三行")
        self.assertEqual(viewmodel.wrap_text("", measure, 40), "")

    def test_bubble_shows_approach_and_first_deliverable_per_row(self):
        rig = self.rig()
        rig.pause_on(examples.get("furniture")["draft"])
        view = viewmodel.bubble_view(rig.view.bubble, can_fill=True)
        self.assertEqual((view.kind, view.title), ("routes", "三条路线 · 选一条填入 Codex"))
        self.assertEqual([(r.approach, r.get) for r in view.rows],
                         [("先做纸样比一比", "拿到能打印的等大纸样"), ("先做单层练手", "拿到单层的图纸和材料单"),
                          ("买现成的来改", "拿到改装步骤和采购清单")])
        self.assertEqual([r.tag for r in view.rows], ["主线", "支线", "支线"])        # exactly one main line
        self.assertTrue(view.rows[0].why.startswith("因为："))
        self.assertEqual((view.rows[1].why, view.rows[0].caution), ("", ""))
        self.assertTrue(view.rows[0].fits.startswith("适用："))
        self.assertTrue(view.rows[0].prompt.startswith("想在阳台做一个三层的花架"))
        # The preview shows which words are the user's own and which were added.
        self.assertEqual(view.rows[0].parts[0][1], examples.get("furniture")["draft"])
        self.assertEqual(len(view.rows[0].parts), 4)
        self.assertEqual((view.reason, view.more), ("", True))
        self.assertTrue(view.footer.startswith("依据：草稿"))
        self.assertEqual(viewmodel.bubble_view(rig.view.bubble, can_fill=False).title, "三条路线 · 选一条复制提示词")

    def test_hints_bubble_has_its_own_title_and_rows(self):
        rig = self.rig()
        rig.controller.set_level(lv.HINT)
        rig.pause_on("想做一个放在玄关的换鞋凳")
        view = viewmodel.bubble_view(rig.view.bubble, can_fill=True)
        self.assertEqual((view.kind, view.rows, len(view.hints)), ("hints", (), 3))
        self.assertEqual(view.title, "这稿还可以补一句 · 点一下加到末尾")
        self.assertEqual(viewmodel.bubble_view(rig.view.bubble, False).title, "这稿还可以补一句 · 点一下复制")
        self.assertTrue(all(h.label and h.text for h in view.hints))
        self.assertEqual([h.is_question for h in view.hints], [False, False, True])

    def test_cautions_and_what_it_was_based_on_are_spelled_out(self):
        routes = suggest.from_example("software").routes
        risky = (follow.replace(routes[0], caution=("destructive", "privilege")),) + tuple(routes[1:])
        bubble = follow.Bubble(1, "auto", None, "d", "routes", risky, reason="重复的记录还没决定留哪条。",
                               mode=lv.WALK, message_id="m", basis=(True, 3, 2), assumes="用浏览器打开")
        view = viewmodel.bubble_view(bubble, True)
        self.assertEqual(view.reason, "重复的记录还没决定留哪条。")
        self.assertIn("；", view.rows[0].caution)
        self.assertEqual(view.footer, "依据：草稿 + 这个聊天最近 3 轮 · 已隐去 2 处疑似密钥 · 假设：用浏览器打开")
        example = follow.Bubble(2, "example", None, "d", "routes", routes)
        shown = viewmodel.bubble_view(example, True)
        self.assertEqual((shown.reason, shown.footer, shown.more),
                         ("内置示例 · 不经过模型", "内置示例，没有发送任何内容", False))
        spare = follow.Bubble(3, "spare", None, "d", "routes", routes)
        self.assertEqual(viewmodel.bubble_view(spare, True).title, "刚才那句还可以这样问")

    def test_status_lines(self):
        self.assertEqual(viewmodel.status_text("idle", "", lv.LEAD), "跟随中 · 带路")
        self.assertEqual(viewmodel.status_text("idle", "", lv.HINT), "跟随中 · 提点")
        self.assertEqual(viewmodel.status_text("idle", "", lv.WALK), "跟随中 · 同行")
        self.assertIn("校准", viewmodel.status_text("blind", "uncalibrated"))
        self.assertIn("没有响应", viewmodel.status_text("blind", "stalled"))
        self.assertIn("安全检查", viewmodel.status_text("error", "unsafe"))
        self.assertIn("连续失败", viewmodel.status_text("error", "halted"))
        self.assertIn("超时", viewmodel.status_text("error", "timeout"))
        self.assertIn("重试", viewmodel.status_text("error", "http"))
        self.assertIn("跳转", viewmodel.status_text("error", "redirect"))
        self.assertIn("思考强度", viewmodel.status_text("error", "effort"))
        self.assertEqual(viewmodel.status_text("error", "consent"), viewmodel.status_text("setup", "consent"))
        self.assertIn("模型", viewmodel.status_text("setup", "model"))
        self.assertIn("发往", viewmodel.status_text("setup", "consent"))
        for state in ("away", "thinking", "paused", "blind"):
            self.assertNotEqual(viewmodel.status_text(state), f"status_{state}")

    def test_every_notice_the_machine_can_emit_has_words(self):
        source = (ROOT / "banxing" / "follow.py").read_text(encoding="utf-8")
        kinds = set(re.findall(r'Notice\("([a-z_]+)"', source)) | {"too_short", "too_long", "too_command", "too_empty"}
        kinds.discard("too_")
        self.assertGreaterEqual(len(kinds), 10)
        for kind in kinds:
            self.assertTrue(viewmodel.notice_text(kind, "timeout"), kind)
        self.assertEqual(viewmodel.notice_text("nothing", "图纸已确认。"), "图纸已确认。")
        for reason in ("mismatch", "failed", "clipboard_busy", "clipboard_restore", "keys_held", "focus", "error"):
            self.assertTrue(viewmodel.offer_text(reason))

    def test_menu_reflects_the_state(self):
        rig = self.rig()
        find = lambda items, item_id: next(i for i in items if i.id == item_id)      # noqa: E731
        items = viewmodel.menu_items(rig.controller.menu())
        self.assertEqual(items[0].label, "等待 Codex 窗口")
        self.assertFalse(find(items, "undo").enabled)
        self.assertFalse(find(items, "now").enabled)
        self.assertFalse(any(i.id == "retry" for i in items))
        self.assertFalse(any(i.id == "hide" for i in items))
        modes = find(items, "levels").children
        self.assertEqual([(m.id, m.checked) for m in modes[:3]],
                         [("level:lead", True), ("level:hint", False), ("level:walk", False)])
        self.assertFalse(any(m.id == "undo_step" for m in modes))
        self.assertEqual(find(items, "calibrate").label, viewmodel.t("menu_calibrate"))
        for wanted in ("aboutme", "evidence", "clear"):
            self.assertTrue(find(items, wanted).label)
        self.assertEqual([c.id for c in find(items, "examples").children], ["example:furniture", "example:software"])

        rig.pause_on(examples.get("furniture")["draft"])
        rig.controller.pick(0)
        rig.tick()
        items = viewmodel.menu_items(rig.controller.menu(), has_tray=True)
        self.assertEqual(items[0].label, "跟随中 · 带路")
        self.assertTrue(find(items, "undo").enabled)
        self.assertTrue(find(items, "now").enabled)
        self.assertTrue(any(i.id == "hide" for i in items))
        self.assertIn("采用 1", find(items, "levels").children[-1].label)

        rig.controller.set_paused(True)
        items = viewmodel.menu_items(rig.controller.menu())
        self.assertTrue(any(i.id == "resume" for i in items))
        self.assertFalse(find(items, "now").enabled)
        tray = viewmodel.tray_items(rig.controller.menu(), pet_visible=False)
        self.assertEqual([i.id for i in tray if i.id], ["show", "resume", "paste", "model", "calibrate", "quit"])

    def test_menu_offers_the_way_back_after_an_automatic_step_and_says_when_uncalibrated(self):
        rig = self.rig()
        find = lambda items, item_id: next(i for i in items if i.id == item_id)      # noqa: E731
        for number in range(4):
            rig.pause_on(f"想做第 {number + 1} 个放在玄关的换鞋凳")
            rig.controller.dismiss()
            rig.send()
        items = viewmodel.menu_items(rig.controller.menu())
        modes = find(items, "levels").children
        self.assertEqual([m.checked for m in modes[:3]], [False, True, False])
        self.assertEqual(find(modes, "undo_step").label, "改回「带路」")
        rig.controller.undo_step()
        self.assertEqual(rig.level, lv.LEAD)

        blank = self.rig(calibrated=False)
        items = viewmodel.menu_items(blank.controller.menu())
        self.assertIn("校准", items[0].label)
        self.assertEqual(find(items, "calibrate").label, viewmodel.t("menu_calibrate_first"))

    def test_calibration_result_lines(self):
        entry = {"results": {"window": {"ok": True}, "read": {"ok": True}, "fill": {"ok": False},
                             "focus": {"ok": False}, "chat": {"skipped": True, "ok": False}},
                 "capabilities": ["read"]}
        lines = viewmodel.calibration_lines(entry)
        self.assertEqual([line[:1] for line in lines[:5]], ["✓", "✓", "✗", "✗", "–"])
        text = "\n".join(lines)
        self.assertIn(viewmodel.t("cap_read"), text)
        self.assertIn(viewmodel.t("cal_no_fill"), text)
        self.assertIn(viewmodel.t("cal_focus_warning"), text)
        self.assertIn(viewmodel.t("cal_none"), "\n".join(viewmodel.calibration_lines({"results": {}})))

    def test_probe_report_is_readable_and_has_no_content(self):
        report = {"client": "uia", "process_names": ["codex.exe"], "foreground_process": "codex.exe",
                  "window": True, "box": True, "readable": True, "draft_chars": 23, "box_id_stable": True,
                  "box_info": {"control_type": 50004, "framework": "Chrome", "value_pattern": True,
                               "text_pattern": True, "read_only": False, "multiline_hint": True,
                               "width": 800, "height": 60},
                  "address": {"found": True, "scheme": "app", "has_thread_id": False}, "selected_items": 1,
                  "text_nodes": 240, "ime_windows_now": [], "ime_like_windows": ["MSCTFIME UI"],
                  "fill": {"paste": {"write": True, "reason": "", "restore": True},
                           "value": {"write": False, "reason": "failed"}, "draft_intact": True}}
        context = {"appserver": True, "sessions": True, "identified": True, "candidates": 4,
                   "source": "appserver", "turns": 3}
        text = viewmodel.probe_text(report, context, "0.2.0")
        for expected in ("[✓] Codex window found", "[✓] draft readable (23 characters)", "[✓] fill by paste",
                         "[✗] fill by value (failed)", "thread id in address ✗", "[✓] current chat recognised"):
            self.assertIn(expected, text)
        self.assertIn("reason: not_windows", viewmodel.probe_text({"client": "none", "reason": "not_windows"}))


class ThreadedExecutorTests(unittest.TestCase):
    def test_snapshots_jobs_and_results_all_arrive_on_the_pumping_thread(self):
        codex = SimulatedCodex()
        codex.type("线程测试草稿")
        seen, done = [], []
        executor = ThreadedExecutor(codex, seen.append, poll=0.02)
        logging.disable(logging.CRITICAL)          # the crashing job below is logged on purpose
        self.addCleanup(logging.disable, logging.NOTSET)
        executor.start()
        try:
            executor.analysis(lambda: "outcome", done.append)
            executor.client(lambda: codex.fill(codex._key(), "线程测试草稿", "新的内容"), done.append)
            executor.analysis(lambda: 1 / 0, done.append)             # a crashing job becomes an error outcome
            self.assertEqual(seen + done, [])                         # nothing happens until pumped
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (len(done) < 3 or len(seen) < 3):
                executor.pump()
                time.sleep(0.01)
            self.assertEqual(executor.call_client(lambda: codex.draft), "新的内容")
        finally:
            executor.stop()
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(all(isinstance(s, follow.Snapshot) for s in seen))
        self.assertIn("outcome", done)
        self.assertTrue(any(getattr(d, "ok", False) and d.method == "simulated" for d in done))
        self.assertTrue(any(getattr(d, "error", "") == "invalid" for d in done))


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-cli-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.saved = {name: os.environ.get(name) for name in ("BANXING_HOME", "BANXING_PROVIDER")}
        os.environ["BANXING_HOME"] = str(self.folder / "home")
        self.addCleanup(self.restore)
        self.lines = []
        self.emit, cli.emit = cli.emit, self.lines.append
        self.addCleanup(setattr, cli, "emit", self.emit)

    def restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_version(self):
        self.assertEqual(cli.main(["--version"]), 0)
        self.assertRegex(self.lines[0], r"^\d+\.\d+\.\d+$")

    def test_selftest_passes_its_required_checks_and_writes_a_report(self):
        report_path = self.folder / "out" / "selftest.json"
        self.assertEqual(cli.main(["--selftest", "--report", str(report_path)]), 0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        for name in ("core", "prompts", "context", "secretbox"):
            self.assertTrue(report["checks"][name]["ok"], report["checks"][name])
        self.assertIn("tk", report["checks"])
        self.assertIn("self-test passed", self.lines[-1])
        self.assertFalse((self.folder / "home").exists())             # the self-test leaves nothing behind

    def test_selftest_fails_when_a_required_check_cannot_pass(self):
        if sys.platform == "win32":
            self.skipTest("uia is expected to work on Windows")
        self.assertEqual(cli.main(["--selftest", "--require", "uia"]), 1)
        self.assertIn("FAILED", self.lines[-1])

    def test_demo_prints_three_suggestions_with_the_offline_stand_in(self):
        os.environ["BANXING_PROVIDER"] = "mock"
        self.assertEqual(cli.main(["--demo", examples.get("software")["draft"]]), 0)
        text = "\n".join(self.lines)
        for approach in ("先用表格搭一版", "先做能点的页面", "先想清要记什么"):
            self.assertIn(approach, text)

    def test_demo_at_the_hint_level_prints_what_is_missing(self):
        os.environ["BANXING_PROVIDER"] = "mock"
        self.assertEqual(cli.main(["--demo", "想做一个放在玄关的换鞋凳", "--mode", "hint"]), 0)
        self.assertNotIn("先用表格搭一版", "\n".join(self.lines))
        self.assertGreaterEqual(len(self.lines), 3)

    def test_eval_with_the_stand_in_is_marked_as_plumbing_only(self):
        os.environ["BANXING_PROVIDER"] = "mock"
        self.assertEqual(cli.main(["--eval", "--limit", "6"]), 0)
        text = "\n".join(self.lines)
        self.assertIn("plumbing", text)
        self.assertFalse((self.folder / "home" / "events.jsonl").exists())

    def test_demo_without_a_model_says_so(self):
        os.environ.pop("BANXING_PROVIDER", None)
        self.assertEqual(cli.main(["--demo", "随便写点什么想法看看"]), 2)

    def test_summary_of_an_empty_log(self):
        self.assertEqual(cli.main(["--summary"]), 0)
        self.assertEqual(json.loads(self.lines[0])["shown"], 0)


def _display_available():
    try:
        import gc
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        del root
        gc.collect()        # freed here, on the thread that made it (see ui/app.py, "Threads")
        return ""
    except Exception as error:      # no tkinter, or no display
        return f"{type(error).__name__}: {error}"


NO_DISPLAY = _display_available()


@unittest.skipIf(NO_DISPLAY, f"Tk cannot open a window here ({NO_DISPLAY})")
class TkShellTests(unittest.TestCase):
    """Builds the real windows. Runs on the Windows CI runner."""

    def test_shell_renders_a_bubble_and_fills_on_click(self):
        folder = Path(tempfile.mkdtemp(prefix="banxing-tk-"))
        self.addCleanup(shutil.rmtree, folder, True)
        result = cli._check_tk(folder)
        self.assertGreaterEqual(min(result["bubble"]), 40)


if __name__ == "__main__":
    unittest.main()
