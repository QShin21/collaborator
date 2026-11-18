"""The repository as a whole: every source compiles and keeps to the house rules,
the app has no way of typing into another application, and the papers that go out
with the code say the same thing as the code."""
import re
import sys
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

import banxing

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "banxing").rglob("*.py"))


class SourceTests(unittest.TestCase):
    def test_every_source_file_compiles(self):
        """Includes the Windows-only module, which cannot be imported here."""
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

    def test_the_app_has_no_way_of_typing_into_another_application(self):
        """It only reads. Nothing in the package synthesises keys or mouse input, drives
        another window's controls, or posts messages to it. The one thing that leaves
        towards another application is text on the clipboard, when the user clicks."""
        banned = (r"SendInput", r"keybd_event", r"mouse_event", r"PostMessage", r"SendMessage", r"\bVK_[A-Z]+",
                  r"UIAutomation", r"comtypes", r"pyautogui", r"pynput", r"SetValue\(", r"WM_PASTE", r"WM_CHAR")
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for pattern in banned:
                self.assertNotRegex(text, pattern, f"{path.name}: {pattern}")
        writers = [path.name for path in SOURCES if "clipboard_append" in path.read_text(encoding="utf-8")]
        self.assertEqual(writers, ["app.py"])

    def test_only_the_shell_knows_about_windows_and_only_the_executor_about_threads(self):
        if sys.platform != "win32":
            with self.assertRaises(ImportError):
                import banxing.winshell  # noqa: F401
        pure = ("watch/transcript.py", "watch/ledger.py", "watch/nudge.py", "watch/session.py", "lint.py",
                "textutil.py", "geometry.py", "ui/viewmodel.py")
        for name in pure:
            text = (ROOT / "banxing" / name).read_text(encoding="utf-8")
            for needle in ("import threading", "import time", "import os\n", "open(", "import subprocess", "import socket",
                           "urllib.request", "tkinter"):
                self.assertNotIn(needle, text, f"{name} is supposed to be pure: {needle}")

    def test_nothing_of_the_archived_app_is_imported(self):
        gone = ("follow", "suggest", "levels", "calibration", "hosts", "aboutme", "controller", "evidence", "drafts")
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for name in gone:
                self.assertNotRegex(text, rf"(?m)^\s*(?:from|import) \.*(?:banxing\.)?{name}\b", f"{path.name}: {name}")
        self.assertFalse((ROOT / "banxing" / "hosts").exists())


class DocumentTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_the_ledger_has_a_row_for_every_work_package_and_claims_nothing_beyond_e1(self):
        ledger = self.read("docs/verification.md")
        rows = re.findall(r"(?m)^\| (W-\d\d) \|.*$", ledger)
        self.assertEqual(rows, [f"W-{n:02d}" for n in range(1, 19)])
        records = list((ROOT / "docs" / "evidence").glob("EV-*.md")) if (ROOT / "docs" / "evidence").is_dir() else []
        if records:
            self.skipTest("evidence has been recorded; review the wording by hand")
        for line in ledger.splitlines():
            if line.startswith("| W-"):
                level = line.split("|")[4]
                self.assertNotRegex(level, r"\bE[2-5]\b(?!.*E0)", line[:50])        # nothing above E1 without a record
        for relative in ("README.md", "docs/verification.md", "docs/design.md", "packaging/windows/README.txt"):
            text = self.read(relative)
            self.assertNotRegex(text, r"(?:真机|真实模型|Windows)[^。\n]{0,12}通过（E[2-5]", relative)

    def test_the_papers_say_which_version_this_is_and_that_it_is_a_preview(self):
        self.assertRegex(banxing.__version__, r"^\d+\.\d+\.\d+(a|b|rc)\d+$")       # a pre-release, and named as one
        for relative in ("README.md", "docs/verification.md", "docs/design.md", "packaging/windows/README.txt"):
            text = self.read(relative)
            self.assertIn("预览版", text, relative)
        for relative in ("README.md", "docs/verification.md", "docs/design.md"):
            self.assertIn(banxing.__version__, self.read(relative), relative)

    def test_the_test_count_in_the_ledger_is_the_real_one(self):
        loader = unittest.TestLoader()
        count = loader.discover(str(ROOT / "tests")).countTestCases()
        match = re.search(r"Linux 上 (\d+) 项通过", self.read("docs/verification.md"))
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), count)

    def test_the_founders_first_vision_is_still_there_word_for_word(self):
        readme = self.read("README.md")
        for line in ("> 制作一个帮助人们使用ai解决真实世界问题的的ai-collaborator，**problem-solving of authentic challenges。**",
                     "> 学生行动：学生主动描述问题、提出问题、提出目标、提出即使是模糊的想法，即结构良好问题或者结构不良问题，作为agent的大前提；"
                     "学生探索问题的具体任务、解决问题等。",
                     "> 改造前后的对照：用不同的agent，前后测与横评。"):
            self.assertIn(line, readme)
        archived = (ROOT / "legacy" / "v0.3" / "README.v0.3.md").read_text(encoding="utf-8")
        start, end = "项目发起人最初的设想（原话）：", "> 改造前后的对照：用不同的agent，前后测与横评。"
        self.assertEqual(readme[readme.index(start):readme.index(end)], archived[archived.index(start):archived.index(end)])

    def test_the_archive_is_complete_and_the_root_does_not_run_it(self):
        archive = ROOT / "legacy" / "v0.3"
        for relative in ("banxing/follow.py", "banxing/hosts/uia.py", "tests/test_security.py", "docs/verification.md",
                         "docs/banxing-generation2.md", "docs/evidence/EV-2026-011.md",
                         "README.md", "README.v0.3.md", "CLAUDE.md"):
            self.assertTrue((archive / relative).is_file(), relative)
        self.assertTrue((archive / "CLAUDE.md").read_text(encoding="utf-8").startswith("> **ARCHIVE.**"))
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            self.assertNotIn("legacy", text, workflow.name)
            self.assertNotIn("comtypes", text, workflow.name)
            self.assertNotIn("test_windows", text, workflow.name)
            self.assertIn("NOT YET RUN", text, workflow.name)                       # until somebody has run it

    def test_the_readme_names_only_commands_and_files_that_exist(self):
        readme = self.read("README.md")
        from banxing import cli
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for option in set(re.findall(r"python run\.py (--[a-z\-]+)", readme)):
            self.assertIn(f'"{option}"', source, option)
        for relative in set(re.findall(r"\]\(((?:docs|legacy)/[\w./\-]+)\)", readme)):
            self.assertTrue((ROOT / relative).exists(), relative)


class PackagingTests(unittest.TestCase):
    """The installer and the workflows cannot be run here (no Windows, no Inno Setup, no
    PowerShell). These only check that the files agree with each other and with the code."""
    APP_GUID = "53BFD128-3CC7-4A13-8915-94F7446099F3"

    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def workflows(self):
        return {path.name: path.read_text(encoding="utf-8") for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))}

    def test_the_installer_keeps_its_identity_asks_for_no_rights_and_leaves_user_data_alone(self):
        raw = (ROOT / "packaging" / "windows" / "banxing.iss").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                           # or the Chinese names are mangled
        script = self.read("packaging/windows/banxing.iss")
        self.assertIn("AppId={{" + self.APP_GUID + "}", script)                    # never changes: upgrades find it
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("{#AppVersion}", script)
        from banxing import APP_ID
        self.assertIn(f"AppMutex={APP_ID}-Banxing", script)                        # the name the running app holds
        self.assertIn('"Local\\\\" + APP_ID + "-Banxing"', (ROOT / "banxing" / "cli.py").read_text(encoding="utf-8"))
        active = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith(";"))
        for section in ("[UninstallDelete]", "[InstallDelete]", "[Registry]", "[Code]", "[UninstallRun]"):
            self.assertNotIn(section, active)                                      # it removes only what it installed
        places = set(re.findall(r"\{(?!\{)(#?[a-z]+)", active, re.I))                # every folder constant it uses
        self.assertEqual(places, {"#AppVersion", "#NumericVersion", "#SourceDir", "#OutputDir", "#IconFile",
                                  "app", "autopf", "autoprograms", "autodesktop", "cm"})   # none is the user's data
        self.assertEqual(re.findall(r"(?m)^\[(\w+)\]", active), ["Setup", "Languages", "Tasks", "Files", "Icons", "Run"])
        check = self.read("packaging/windows/verify-installer.ps1")
        self.assertIn("{" + self.APP_GUID + "}_is1", check)
        for path in sorted((ROOT / "packaging" / "windows").glob("*.ps1")):
            path.read_bytes().decode("ascii")                                      # read alike by every PowerShell
        # WScript.Shell cannot open a file whose name is outside the ANSI code page: the
        # shortcut's Chinese name is checked by code point, its target read from a copy.
        self.assertIn("[char]0x4F34) + ([string][char]0x884C", check)
        self.assertIn("$shell.CreateShortcut($copy)", check)
        self.assertNotIn("CreateShortcut($found", check)
        self.assertIn("Name: \"{autoprograms}\\\u4f34\u884c Banxing\"", script)
        for needle in ("/VERYSILENT", "unins000.exe", "settings.json", "the user's data folder was left alone"):
            self.assertIn(needle, check)

    def test_the_build_the_checks_and_the_workflows_name_the_same_files(self):
        script = self.read("packaging/windows/banxing.iss")
        build = self.read("packaging/windows/build.ps1")
        self.assertIn("OutputBaseFilename=Banxing-v{#AppVersion}-windows-x64-setup", script)
        self.assertIn('$name = "Banxing-v$Version-windows-x64"', build)
        self.assertIn('"$name-setup.exe"', build)
        for define in re.findall(r"\{#(\w+)\}", script):
            self.assertIn(f"/D{define}=", build, define)                           # everything the script needs is passed
        self.assertIn('--add-data "banxing/prompts;banxing/prompts"', build)
        self.assertIn("--selftest", build)
        workflows = self.workflows()
        self.assertIn(r"release\Banxing-v$version-windows-x64-setup.exe", workflows["ci.yml"])
        for name, text in workflows.items():
            self.assertIn("packaging/windows/build.ps1", text, name)               # one build, tested on every push
            self.assertIn("packaging/windows/verify-installer.ps1", text, name)
            self.assertLess(text.index("unittest discover"), text.index("build.ps1 -Version"), name)
            self.assertLess(text.index("build.ps1 -Version"), text.index("verify-installer.ps1 -Setup"), name)
        self.assertNotIn("softprops", workflows["ci.yml"])                         # CI publishes nothing

    def test_a_release_is_a_preview_with_its_notes_and_never_replaces_one(self):
        version = banxing.__version__
        notes = self.read(f"docs/release-notes/v{version}.md")
        for needle in (version, "预览版", f"Banxing-v{version}-windows-x64-setup.exe", f"Banxing-v{version}-windows-x64.zip",
                       "没有验证的", "没有代码签名", "升级和卸载都不动你的数据"):
            self.assertIn(needle, notes)
        self.assertNotRegex(notes, r"(?:真机|真实模型|真实桌面)[^。\n]{0,12}(?:已验证|通过)")
        publishing = {name: text for name, text in self.workflows().items() if "softprops/action-gh-release" in text}
        self.assertIn("release.yml", publishing)
        for name, text in publishing.items():
            publish = text[text.index("softprops/action-gh-release"):]
            self.assertIn("prerelease: true", publish, name)
            self.assertIn("fail_on_unmatched_files: true", publish, name)
            self.assertIn("body_path: docs/release-notes/", publish, name)
            self.assertIn("release/*-setup.exe", publish, name)
            before = text[:text.index("softprops/action-gh-release")]
            self.assertIn("never overwrites one", before, name)                    # checked before anything is published
            self.assertLess(before.index("verify-installer.ps1 -Setup"), len(before), name)

    def test_a_one_time_release_workflow_is_for_this_version_only(self):
        """Such a file starts a release by being pushed. One for an older version must not
        linger (a later edit of it would start that release again), and the one for this
        version can only build the tree it names."""
        once = {name: text for name, text in self.workflows().items() if name.startswith("release-once-")}
        version = banxing.__version__
        self.assertLessEqual(len(once), 1)
        for name, text in once.items():
            self.assertEqual(name, f"release-once-v{version}.yml")
            self.assertIn(f"VERSION: v{version}\n", text)
            self.assertRegex(text, r"TARGET_TREE: [0-9a-f]{40}\n")
            self.assertIn(f'paths: [".github/workflows/{name}"]', text)
            self.assertIn("branches: [main]", text)
            self.assertNotIn("workflow_dispatch", text)
            self.assertNotRegex(text, r"(?m)^\s+tags:")
            self.assertIn("target_commitish: ${{ env.TARGET_SHA }}", text)


if __name__ == "__main__":
    unittest.main()
