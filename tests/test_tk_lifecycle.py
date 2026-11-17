"""Who owns the Tk interpreter, and where it dies.

The first Windows CI run (commit ca3ec234) ended with

    Tcl_AsyncDelete: async handler deleted by the wrong thread

during the self-test. The windows built by the self-test's Tk check (and by the
screen tests before it) had been destroyed but not freed: widgets, variables and
callbacks hold each other in cycles and wait for Python's cycle collector. The next
check started a thread for UI Automation; importing comtypes there made the
collector run — on that thread — and Tcl aborted the process when its interpreter
was deleted by a thread that did not create it.

The rule these tests hold: when `PetApp.destroy()` (or the self-test's Tk check)
returns, nothing of Tk is left in memory, so no other thread can ever be the one to
free it. They run with the automatic collector switched OFF, so that "nothing is
left" is the app's own doing and not an accident of timing.

These build real windows and are skipped without a display. On Linux they show the
rule holds (E1); the fatal consequence itself exists only where Tcl is built with
threads, as on Windows — there the same tests are the proof that it no longer
happens (E2, once the CI run is green).
"""
import gc
import shutil
import tempfile
import threading
import tkinter
import unittest
import weakref
from pathlib import Path
from tkinter import font as tkfont

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import cli, examples
from banxing.config import Settings
from banxing.context import ContextProvider
from banxing.controller import InlineExecutor, View
from banxing.hosts.simulated import SimulatedCodex
from banxing.store import Store
from banxing.ui import strings

from test_shell import NO_DISPLAY


def tk_objects():
    """Every Tk object the interpreter still knows of — without collecting first."""
    kinds = (tkinter.Misc, tkinter.Variable, tkinter.Image, tkfont.Font)
    return [type(thing).__name__ for thing in gc.get_objects() if isinstance(thing, kinds)]


def collect_on_another_thread():
    """What the UI Automation thread did by accident: run the cycle collector. Callers
    first assert that no Tk object is left; this then shows the collection is survived
    (where Tcl is threaded, a Tk interpreter freed here would end the process)."""
    found = []
    thread = threading.Thread(target=lambda: found.append(gc.collect()), name="stranger")
    thread.start()
    thread.join(30)
    return found


@unittest.skipIf(NO_DISPLAY, f"Tk cannot open a window here ({NO_DISPLAY})")
class TkLifecycle(unittest.TestCase):
    def setUp(self):
        strings.set_language("zh")
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-tk-life-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        gc.collect()                            # start clean, whatever ran before
        self.assertEqual(tk_objects(), [])
        gc.disable()                            # from here on, only explicit collections happen
        self.addCleanup(gc.enable)

    def build(self):
        from banxing.ui.app import PetApp
        settings = Settings(self.folder, environ={"BANXING_PROVIDER": "mock"})
        codex = SimulatedCodex()
        app = PetApp(settings, Store(self.folder), use_tray=False)
        empty = ContextProvider(sessions_dir=str(self.folder / "none"), use_appserver=False)
        controller, _, _ = cli.build(settings, Store(self.folder), app, codex,
                                     lambda on_snapshot: InlineExecutor(), context=empty,
                                     capabilities=codex.capabilities())
        app.attach(controller, None)
        return app, controller, codex

    def use(self, app, controller, codex):
        """Everything that creates Tk objects: bubble, preview, menu, all six dialogs."""
        from banxing.ui import viewmodel
        codex.type(examples.get("furniture")["draft"])
        now = 50.0
        for _ in range(10):
            now += 0.25
            controller.clock = lambda now=now: now
            controller.on_snapshot(codex.snapshot(now))
            app.root.update()
        self.assertIsNotNone(app.bubble)
        app._toggle_preview(0)
        app._build_menu(viewmodel.menu_items(controller.menu()))
        for opener in (app.open_model, app.open_paste, app.open_probe, app.open_aboutme,
                       app.open_evidence, app.open_calibration):
            opener()
            app.root.update()
        app.status("error", "timeout")
        app.offer_copy("x", "failed")
        app.root.update()

    # ------------------------------------------------------------ the rule
    def test_destroy_frees_the_interpreter_at_once_on_its_own_thread(self):
        app, controller, codex = self.build()
        self.use(app, controller, codex)
        root = weakref.ref(app.root)
        dialog = weakref.ref(app.dialog_windows[0])
        self.assertGreater(len(tk_objects()), 20)

        app.destroy()

        self.assertIsNone(root())
        self.assertIsNone(dialog())
        self.assertEqual(tk_objects(), [])      # nothing waits for the collector
        self.assertEqual(len(collect_on_another_thread()), 1)

    def test_dialogs_closed_one_by_one_first_make_no_difference(self):
        app, controller, codex = self.build()
        self.use(app, controller, codex)
        for window in list(app.dialog_windows):
            window.destroy()
            app.root.update()
        window = None
        self.assertEqual(app.dialogs, 0)
        app.destroy()
        self.assertEqual(tk_objects(), [])

    def test_the_self_tests_tk_check_leaves_nothing_for_the_checks_that_follow(self):
        """The crash, step by step: the Tk check, then a collection on another thread."""
        result = cli._check_tk(self.folder)
        self.assertGreaterEqual(min(result["bubble"]), 40)
        self.assertEqual(tk_objects(), [])
        self.assertEqual(len(collect_on_another_thread()), 1)

    def test_a_failing_tk_check_leaves_nothing_either(self):
        from banxing.ui import app as app_module
        real = app_module.PetApp.open_evidence

        def broken(self):
            raise RuntimeError("a dialog that cannot be built")
        app_module.PetApp.open_evidence = broken
        try:
            with self.assertRaises(RuntimeError):
                cli._check_tk(self.folder)
        finally:
            app_module.PetApp.open_evidence = real
        self.assertEqual(tk_objects(), [])
        self.assertEqual(len(collect_on_another_thread()), 1)

    def test_the_whole_self_test_ends_without_tk_in_memory(self):
        lines = []
        emit, cli.emit = cli.emit, lines.append
        self.addCleanup(setattr, cli, "emit", emit)
        self.assertEqual(cli.selftest(str(self.folder / "report.json")), 0, lines)
        self.assertEqual(tk_objects(), [])
        self.assertEqual(len(collect_on_another_thread()), 1)

    # ------------------------------------------------------------ ownership
    def test_only_the_owning_thread_may_end_or_run_it(self):
        from banxing.ui.app import WrongThread
        app, controller, codex = self.build()
        caught = []

        def stranger():
            for action in (app.destroy, app.quit, app.run):
                try:
                    action()
                except WrongThread as error:
                    caught.append(str(error))
        thread = threading.Thread(target=stranger)
        thread.start()
        thread.join(30)
        self.assertEqual(len(caught), 3, caught)
        self.assertFalse(app.owned.__self__._destroyed)
        self.assertTrue(app.owned())
        app.root.update()                       # still alive and usable
        self.assertTrue(app.pet.winfo_exists())
        app.destroy()
        self.assertEqual(tk_objects(), [])

    def test_destroy_twice_and_quit_afterwards_are_harmless(self):
        app, controller, codex = self.build()
        app.destroy()
        app.destroy()
        app.quit()
        self.assertEqual(tk_objects(), [])

    def test_the_controller_outlives_the_windows_without_touching_them(self):
        app, controller, codex = self.build()
        self.use(app, controller, codex)
        app.destroy()
        self.assertIsNot(controller.view, app)
        self.assertIs(type(controller.view), View)
        self.assertIsNone(app.controller)
        codex.type("想给猫做一个爬架，家里没有电钻")
        for step in range(12):                  # snapshots, a model answer, a bubble effect: no Tk involved
            controller.clock = lambda step=step: 100.0 + step * 0.25
            controller.on_snapshot(codex.snapshot(100.0 + step * 0.25))
        controller.watchdog()
        self.assertEqual(tk_objects(), [])

    def test_a_window_closed_by_the_system_first_is_still_cleaned_up(self):
        app, controller, codex = self.build()
        app.root.destroy()                      # e.g. the session is ending
        app.destroy()
        self.assertEqual(tk_objects(), [])

    def test_quit_from_the_tray_runs_on_the_owning_thread(self):
        """The tray icon lives on its own thread; all it may do is queue the request."""
        app, controller, codex = self.build()
        threading.Thread(target=lambda: app._calls.put(app.quit)).start()
        app.root.after(40, app._pump)
        app.root.after(3000, app.root.quit)     # safety net: never hang the suite
        app.root.mainloop()
        self.assertTrue(app._quit)
        app.destroy()
        self.assertEqual(tk_objects(), [])

    def test_closing_a_dialog_schedules_a_collection_on_the_tk_thread(self):
        app, controller, codex = self.build()
        app.open_paste()
        app.root.update()
        window = app.dialog_windows[0]
        ref = weakref.ref(window)
        window.destroy()
        window = None
        self.assertTrue(app._release_pending)
        deadline = 0
        while app._release_pending and deadline < 200:
            app.root.update()
            deadline += 1
            threading.Event().wait(0.01)
        self.assertFalse(app._release_pending)
        self.assertIsNone(ref())                # freed while the app is still running
        app.destroy()
        self.assertEqual(tk_objects(), [])


if __name__ == "__main__":
    unittest.main()
