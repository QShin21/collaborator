"""The Tk shell: pet, bubble, notices, menu, dialogs, tray icon.

Thin on purpose — what to show is decided in viewmodel.py and follow.py; this file
only draws it and reports clicks. Tk is used because it ships with Python (no extra
runtime to bundle) and can host the two things the brief needs: a small always-on-
top pet and a bubble that does not take the keyboard away from Codex.

How "does not take the keyboard" is done on Windows: pet, bubble and notices are
borderless tool windows with WS_EX_NOACTIVATE (win32.make_overlay), applied after
the window has been mapped once — Tk creates the native wrapper lazily and would
drop the style otherwise. Dialogs (model settings, paste, self-check) are ordinary
windows and do take focus; that is intended.

Threads. Tk belongs to ONE thread — the one that created the interpreter — for its
whole life, including its end. Three rules follow, and PetApp keeps them itself:

  * every Tk call is made on that thread; other threads (tray icon, watcher, model
    calls) only put work into a queue that `_pump` empties;
  * the interpreter is torn down on that thread, in `destroy()`;
  * and it is also FREED there. Widgets, variables and callbacks refer to each other
    in cycles, so dropping the last name does not free them: they wait for Python's
    cycle collector, which runs on whichever thread happens to allocate next. If
    that is the watcher or a worker, Tcl finds its interpreter being deleted by a
    stranger and aborts the whole process ("Tcl_AsyncDelete: async handler deleted
    by the wrong thread" — seen in the first Windows CI run, when the self-test's
    UI Automation thread collected what the Tk check had left behind). `destroy()`
    therefore lets go of every Tk object and collects at once (`release_tk`).

STATUS: the first Windows CI run built these windows and passed the screen tests,
then died of the fault described above; see docs/verification.md for what has run
since. Focus behaviour next to a real Codex window is a manual check.
"""
from __future__ import annotations

import gc
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox

from .. import APP_NAME, __version__, aboutme, calibration, evidence, geometry, llm
from ..controller import View
from ..geometry import Rect
from . import viewmodel
from .strings import t

log = logging.getLogger(__name__)
WIN = sys.platform == "win32"
if WIN:
    from ..hosts import win32

BG, BORDER, TEXT, MUTED = "#ffffff", "#d0d7de", "#1f2328", "#656d76"
ACCENT, HOVER, REASON, PANEL = "#0969da", "#f3f4f6", "#9a6700", "#f6f8fa"
KEY = "#fe01fd"                     # transparent colour of the pet window
BODY = {"idle": "#4f8cff", "thinking": "#4f8cff", "error": "#4f8cff", "paused": "#9aa4b2",
        "blind": "#4f8cff", "setup": "#4f8cff", "away": "#9aa4b2"}
BADGE = {"error": ("#d1242f", "!"), "blind": ("#bf8700", "?"), "setup": ("#bf8700", "?")}


def release_tk():
    """Free Tk objects that are no longer referenced — now, on the calling thread.
    Call it on the Tk thread after windows were destroyed (see "Threads" above).
    Returns the number of objects the collector found."""
    return gc.collect()


def _holds_tk(value):
    if isinstance(value, (tk.Misc, tk.Variable, tk.Image, tkfont.Font)):
        return True
    if isinstance(value, (list, tuple, set)):
        return any(_holds_tk(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_tk(item) for item in value.values())
    return False


class WrongThread(RuntimeError):
    """A Tk operation was asked for on a thread that does not own the interpreter."""


class PetApp(View):
    def __init__(self, settings, store, use_tray=True):
        self.settings = settings
        self.store = store
        self.controller = None
        self.executor = None
        self._owner = threading.get_ident()     # the only thread that may touch Tk — or end it
        self._destroyed = False
        self._release_pending = False
        self._menu = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        family = "Microsoft YaHei UI" if WIN else "TkDefaultFont"
        self.font = (family, 10)
        self.bold = (family, 10, "bold")
        self.small = (family, 9)
        self.size = self.px(60)

        self.snap = None
        self.state = ("away", "")
        self.pet_rect = None
        self.pet_visible = False
        self.pinned = False                 # show the pet even without a Codex window
        self.bubble = None                  # follow.Bubble on screen
        self.bubble_view = None
        self.preview = None                 # index of the row whose preview is open
        self._bubble_size = None
        self.dialogs = 0                    # open dialogs, message boxes included
        self.dialog_windows = []
        self._drag = None
        self._tick = 0
        self._told = set()
        self._toast_timer = None
        self._menu_vars = []
        self._calls = queue.Queue()         # from the tray thread
        self._tray = None
        self._tray_state = None
        self._quit = False

        self.pet = self._overlay_window()
        self.canvas = tk.Canvas(self.pet, width=self.size, height=self.size, highlightthickness=0,
                                bg=KEY if WIN else BG, cursor="hand2")
        self.canvas.pack()
        if WIN:
            try:
                self.pet.wm_attributes("-transparentcolor", KEY)
            except tk.TclError:
                pass
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._popup)
        self.bubble_window = self._overlay_window()
        self.toast = self._overlay_window()
        for window in (self.pet, self.bubble_window, self.toast):
            self._prime(window)
        self._draw_pet()
        if use_tray:
            self._start_tray()
        self.pinned = self._tray is None    # without a tray icon the pet must stay reachable

    # ------------------------------------------------------------ plumbing
    def px(self, value):
        return int(round(value * self.scale))

    def attach(self, controller, executor=None):
        self.controller = controller
        self.executor = executor

    def _overlay_window(self):
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        try:
            window.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        return window

    def _native(self, window):
        """Make a mapped overlay window non-activating. Safe to call repeatedly."""
        if not WIN:
            return
        try:
            win32.make_overlay(win32.top_level(window.winfo_id()))
        except Exception:
            log.exception("could not set the overlay style")

    def _prime(self, window):
        """Map the window once, off screen, so that its native wrapper exists and
        can be given the no-activate style before it is ever shown for real."""
        window.geometry("+-20000+-20000")
        window.deiconify()
        window.update_idletasks()
        self._native(window)
        window.withdraw()

    def _move(self, window, geometry_spec):
        """Apply a geometry request right away. If window events are processed
        between the request and Tk acting on it, a stale "you are at the old
        place" event can overwrite the request (seen on X11) and the window stays."""
        window.geometry(geometry_spec)
        window.update_idletasks()

    def _show(self, window, rect):
        self._move(window, f"{rect.width}x{rect.height}+{rect.left}+{rect.top}")
        window.deiconify()
        window.lift()
        self._native(window)

    def _screen(self):
        snap = self.snap
        if WIN and snap is not None and snap.window_id:
            try:
                return win32.work_area(int(snap.window_id))
            except Exception:
                pass
        return Rect(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def owned(self):
        """Is the calling thread the one that created (and so owns) the interpreter?"""
        return threading.get_ident() == self._owner

    def _own(self, what):
        if not self.owned():
            raise WrongThread(f"{what} must run on the thread that created the windows")

    def _release_soon(self):
        """A dialog or a menu was closed: free what it left behind on this thread,
        shortly, instead of leaving it for the collector to find from another one."""
        if self._release_pending or self._quit:
            return
        self._release_pending = True

        def release():
            self._release_pending = False
            if not self._quit:
                release_tk()
        self.root.after(250, release)

    def run(self):
        self._own("run()")
        self.root.after(40, self._pump)
        self.root.after(350, self._animate)
        self.root.mainloop()

    def _pump(self):
        if self._quit:
            return
        while True:
            try:
                call = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                call()
            except Exception:
                log.exception("tray command failed")
        if self.executor is not None:
            self.executor.pump()
        if self.controller is not None:
            try:
                self.controller.watchdog()
            except Exception:
                log.exception("watchdog failed")
        self.root.after(40, self._pump)

    def _animate(self):
        if self._quit:
            return
        self._tick += 1
        if self.state[0] == "thinking":
            self._draw_pet()
        if self._tick % 3 == 0 and self.controller is not None:
            self._tray_state = (self.controller.menu(), self.pet_visible)
        self.root.after(350, self._animate)

    def _stop_tray(self):
        tray, self._tray = self._tray, None
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass

    def quit(self):
        """Leave the main loop. The caller of run() then calls destroy()."""
        if self._destroyed:
            return
        self._own("quit()")
        self._quit = True
        self._hide_toast()
        self._stop_tray()
        self.root.quit()

    def destroy(self):
        """End the interpreter and free it — here, on the thread that owns it. After
        this returns nothing of Tk is left for the cycle collector to stumble over on
        another thread. Safe to call twice; refuses any other thread."""
        if self._destroyed:
            return
        self._own("destroy()")
        self._destroyed = True
        self._quit = True
        self._stop_tray()
        try:
            if self._toast_timer is not None:
                self.root.after_cancel(self._toast_timer)
        except tk.TclError:
            pass
        self._toast_timer = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass                                # already gone (the window was closed by the system)
        # The controller keeps working (and logging) without a view; it must not keep
        # this object's windows alive, nor call into them.
        controller, self.controller, self.executor = self.controller, None, None
        if controller is not None and getattr(controller, "view", None) is self:
            controller.view = View()
        for name, value in list(vars(self).items()):
            if _holds_tk(value):
                setattr(self, name, type(value)() if isinstance(value, (list, dict, set)) else None)
        del controller, value
        release_tk()

    # ------------------------------------------------------------ pet
    def _draw_pet(self):
        canvas, size, state = self.canvas, self.size, self.state[0]
        canvas.delete("all")
        pad = self.px(4)
        canvas.create_oval(pad, pad, size - pad, size - pad, fill=BODY.get(state, BODY["idle"]), outline="")
        eye_y, eye_r = int(size * 0.44), max(2, self.px(5))
        for centre in (int(size * 0.36), int(size * 0.64)):
            if state in ("paused", "away"):
                canvas.create_line(centre - eye_r, eye_y, centre + eye_r, eye_y, fill="#ffffff", width=self.px(2))
            else:
                canvas.create_oval(centre - eye_r, eye_y - eye_r, centre + eye_r, eye_y + eye_r,
                                   fill="#ffffff", outline="")
                look = -self.px(2) if state == "thinking" else 0
                dot = max(1, eye_r // 2)
                canvas.create_oval(centre - dot, eye_y - dot + look, centre + dot, eye_y + dot + look,
                                   fill="#1f2328", outline="")
        if state == "thinking":
            for index in range(3):
                x = int(size * (0.36 + 0.14 * index))
                lift = self.px(3) if self._tick % 3 == index else 0
                r = max(2, self.px(3))
                canvas.create_oval(x - r, int(size * 0.70) - r - lift, x + r, int(size * 0.70) + r - lift,
                                   fill="#ffffff", outline="")
        else:
            y = int(size * 0.66)
            canvas.create_arc(int(size * 0.38), y - self.px(8), int(size * 0.62), y + self.px(4),
                              start=200, extent=140, style="arc", outline="#ffffff", width=self.px(2))
        badge = BADGE.get(state)
        if badge:
            r = self.px(9)
            cx, cy = size - pad - r, pad + r
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=badge[0], outline="#ffffff", width=self.px(1))
            canvas.create_text(cx, cy, text=badge[1], fill="#ffffff", font=self.bold)

    def _free_rect(self):
        saved = self.settings.get("pet_free")
        screen = Rect(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            rect = Rect.at(saved[0], saved[1], self.size, self.size)
        else:
            rect = Rect.at(screen.right - self.size - self.px(48), screen.bottom - self.size - self.px(120),
                           self.size, self.size)
        return geometry.clamp_into(rect, screen)

    def follow(self, snap):
        self.snap = snap
        if self._drag is not None:
            return
        rect = None
        if snap.foreground and snap.window_rect is not None:
            offset = self.settings.get("pet_offset")
            offset = tuple(offset) if isinstance(offset, (list, tuple)) and len(offset) == 2 else None
            rect = geometry.pet_rect(snap.window_rect, self._screen(), (self.size, self.size), offset, snap.box_rect)
        elif self.pinned or self.bubble is not None or self.dialogs or snap.problem == "error":
            rect = self._free_rect()        # nothing to follow, but something to show or to fix
        self._place_pet(rect)

    def greet(self, seconds=8.0):
        """On start: show the pet for a moment even without Codex in front, so the
        user sees that it is running and where it will appear."""
        was_pinned, self.pinned = self.pinned, True
        if not self.pet_visible:
            self._place_pet(self._free_rect())
        calibrated = self.controller is None or self.controller.menu().get("calibrated")
        self._say(t("notice_started" if calibrated else "notice_started_uncalibrated"), seconds=seconds)

        def settle():
            self.pinned = was_pinned
            if self.snap is not None and not self._quit:
                self.follow(self.snap)
        self.root.after(int(seconds * 1000), settle)

    def _place_pet(self, rect):
        if rect is None:
            if self.pet_visible:
                self.pet.withdraw()
                self.pet_visible = False
                self._hide_toast()
            return
        if rect != self.pet_rect or not self.pet_visible:
            self.pet_rect = rect
            self._show(self.pet, rect)
            self.pet_visible = True
            if self.bubble is not None:
                self._place_bubble()

    def _press(self, event):
        self._drag = {"x": event.x_root, "y": event.y_root, "rect": self.pet_rect, "moved": False}

    def _motion(self, event):
        drag = self._drag
        if drag is None or drag["rect"] is None:
            return
        dx, dy = event.x_root - drag["x"], event.y_root - drag["y"]
        if not drag["moved"] and abs(dx) + abs(dy) < self.px(5):
            return
        drag["moved"] = True
        self.pet_rect = drag["rect"].moved(dx, dy)
        self._move(self.pet, f"+{self.pet_rect.left}+{self.pet_rect.top}")
        if self.bubble is not None:
            self._place_bubble()

    def _release(self, event):
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if not drag["moved"]:
            if self.controller is not None:
                self.controller.pet_clicked()
            return
        snap = self.snap
        if snap is not None and snap.foreground and snap.window_rect is not None:
            self.settings.set(pet_offset=list(geometry.offset_from(self.pet_rect, snap.window_rect)))
        else:
            self.settings.set(pet_free=[self.pet_rect.left, self.pet_rect.top])

    def status(self, state, detail):
        changed = (state, detail) != self.state
        self.state = (state, detail)
        self._draw_pet()
        if not changed:
            return
        if state == "error":
            self._say(viewmodel.status_text(state, detail))
        elif state in ("blind", "setup") and state not in self._told:
            self._told.add(state)
            self._say(viewmodel.status_text(state, detail))

    # ------------------------------------------------------------ bubble
    def show_bubble(self, bubble, snap):
        self.bubble, self.preview, self._bubble_size = bubble, None, None
        self._hide_toast()
        if not self.pet_visible:
            self._place_pet(self._free_rect())
        self._render_bubble()

    def hide_bubble(self, reason):
        self.bubble = self.bubble_view = None
        self.bubble_window.withdraw()

    def _can_fill(self):
        state = self.controller.menu() if self.controller is not None else {}
        return bool(state.get("can_fill") and state.get("readable"))

    def _label(self, parent, text, font=None, fg=TEXT, bg=BG, **options):
        return tk.Label(parent, text=text, font=font or self.font, fg=fg, bg=bg, bd=0, **options)

    def _wrapped(self, text, font, width):
        return viewmodel.wrap_text(text, tkfont.Font(root=self.root, font=font).measure, width)

    def _para(self, parent, text, width, font=None, fg=TEXT, bg=BG, **options):
        """A left-aligned paragraph, wrapped by us (Tk only wraps at spaces)."""
        font = font or self.font
        return self._label(parent, self._wrapped(text, font, width), font, fg, bg, justify="left", anchor="w",
                           **options)

    def _button(self, parent, text, command, primary=False):
        label = tk.Label(parent, text=text, font=self.small, fg="#ffffff" if primary else TEXT,
                         bg=ACCENT if primary else HOVER, padx=self.px(10), pady=self.px(4), cursor="hand2")
        label.bind("<Button-1>", lambda event: (command(), "break")[1])
        return label

    def _render_bubble(self):
        window = self.bubble_window
        for child in window.winfo_children():
            child.destroy()
        view = self.bubble_view = viewmodel.bubble_view(self.bubble, self._can_fill())
        width, pad = self.px(430), self.px(10)
        wrap = width - 2 * pad - self.px(8)
        outer = tk.Frame(window, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG, padx=pad, pady=self.px(8))
        inner.pack(fill="both", expand=True)

        head = tk.Frame(inner, bg=BG)
        head.pack(fill="x")
        self._label(head, view.title, self.small, MUTED).pack(side="left")
        close = self._label(head, "✕", self.small, MUTED, cursor="hand2", padx=self.px(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda event: self.controller.dismiss())
        if view.reason:
            self._para(inner, view.reason, wrap, self.small, REASON).pack(fill="x", pady=(self.px(2), 0))

        # One shared width for the bold column, so the deliverables line up.
        measure = tkfont.Font(root=self.root, font=self.bold).measure
        first_column = max([measure(row.approach) for row in view.rows] or [0])
        for index, row in enumerate(view.rows):
            self._render_row(inner, index, row, wrap, first_column)
            if self.preview == index:
                self._render_preview(inner, index, row, wrap, view.can_fill)
        for index, hint in enumerate(view.hints):
            self._render_hint(inner, index, hint, wrap)
        if view.kind == "hints":
            link = self._label(inner, t("hints_routes"), self.small, ACCENT, cursor="hand2", anchor="w")
            link.pack(fill="x", pady=(self.px(6), 0))
            link.bind("<Button-1>", lambda event: self.controller.want_routes())

        foot = tk.Frame(inner, bg=BG)
        foot.pack(fill="x", pady=(self.px(6), 0))
        if view.more:
            more = self._label(foot, t("bubble_more"), self.small, ACCENT, cursor="hand2")
            more.pack(side="right")
            more.bind("<Button-1>", lambda event: self.controller.more())
        if view.footer:
            basis = self._para(foot, view.footer, wrap - self.px(70), self.small, MUTED,
                               cursor="hand2" if self.bubble.request else "")
            basis.pack(side="left")
            if self.bubble.request:
                basis.bind("<Button-1>", lambda event: self._dialog_message(t("basis_sent"), self.bubble.request))

        window.update_idletasks()
        self._bubble_size = (max(width, window.winfo_reqwidth()), window.winfo_reqheight())
        self._place_bubble()

    def _render_row(self, parent, index, row, wrap, first_column=0):
        frame = tk.Frame(parent, bg=BG, cursor="hand2", pady=self.px(5), padx=self.px(4))
        frame.pack(fill="x", pady=(self.px(4) if index == 0 else 0, 0))
        tag = self._label(frame, row.tag, self.small, ACCENT if row.why else MUTED, cursor="hand2", anchor="w")
        tag.grid(row=0, column=0, sticky="w", padx=(0, self.px(8)))
        approach = self._label(frame, row.approach, self.bold, cursor="hand2", anchor="w")
        approach.grid(row=0, column=1, sticky="w")
        frame.columnconfigure(1, minsize=first_column)
        get = self._label(frame, row.get, self.font, MUTED, cursor="hand2", anchor="w", justify="left",
                          wraplength=max(self.px(110), wrap - first_column - self.px(120)))
        get.grid(row=0, column=2, sticky="w", padx=(self.px(12), self.px(8)))
        preview = self._label(frame, t("bubble_collapse" if self.preview == index else "bubble_preview"),
                              self.small, ACCENT, cursor="hand2")
        preview.grid(row=0, column=3, sticky="e")
        frame.columnconfigure(2, weight=1)
        parts = [frame, tag, approach, get, preview]
        for line, text, colour in ((1, row.why, MUTED), (2, ("⚠ " + row.caution) if row.caution else "", REASON)):
            if text:
                note = self._label(frame, self._wrapped(text, self.small, wrap - self.px(60)), self.small, colour,
                                   cursor="hand2", anchor="w", justify="left")
                note.grid(row=line, column=1, columnspan=3, sticky="w")
                parts.append(note)

        def shade(colour):
            for part in parts:
                try:
                    part.configure(bg=colour)
                except tk.TclError:
                    pass
        for part in parts:
            if part is not preview:
                part.bind("<Button-1>", lambda event, i=index: self.controller.pick(i))
        preview.bind("<Button-1>", lambda event, i=index: (self._toggle_preview(i), "break")[1])
        for part in parts:
            part.bind("<Enter>", lambda event: shade(HOVER))
            part.bind("<Leave>", lambda event: shade(BG))

    def _render_hint(self, parent, index, hint, wrap):
        frame = tk.Frame(parent, bg=BG, cursor="hand2", pady=self.px(5), padx=self.px(4))
        frame.pack(fill="x", pady=(self.px(4) if index == 0 else 0, 0))
        mark = self._label(frame, "？" if hint.is_question else "＋", self.bold, ACCENT, cursor="hand2")
        mark.grid(row=0, column=0, sticky="nw", padx=(0, self.px(8)))
        label = self._label(frame, hint.label, self.bold, cursor="hand2", anchor="w")
        label.grid(row=0, column=1, sticky="w")
        text = self._label(frame, self._wrapped("「" + hint.text + "」", self.font, wrap - self.px(40)), self.font,
                           MUTED, cursor="hand2", anchor="w", justify="left")
        text.grid(row=1, column=1, sticky="w")
        parts = (frame, mark, label, text)
        for part in parts:
            part.bind("<Button-1>", lambda event, i=index: self.controller.apply_hint(i))
            part.bind("<Enter>", lambda event: [p.configure(bg=HOVER) for p in parts])
            part.bind("<Leave>", lambda event: [p.configure(bg=BG) for p in parts])

    def _render_preview(self, parent, index, row, wrap, can_fill):
        box = tk.Frame(parent, bg=PANEL, padx=self.px(8), pady=self.px(6))
        box.pack(fill="x", pady=(0, self.px(4)))
        if row.fits:
            self._para(box, row.fits, wrap - self.px(16), self.small, MUTED, PANEL).pack(fill="x")
        # The prompt in its parts, so that what makes it work can be seen: your own
        # words first, then what to do now, what to hand back, how to check.
        for name, text in row.parts:
            self._label(box, name, self.small, ACCENT, PANEL, anchor="w").pack(fill="x", pady=(self.px(4), 0))
            self._para(box, text, wrap - self.px(16), self.font, TEXT, PANEL).pack(fill="x")
        if row.caution:
            self._para(box, "⚠ " + row.caution, wrap - self.px(16), self.small, REASON, PANEL).pack(
                fill="x", pady=(self.px(6), 0))
        actions = tk.Frame(box, bg=PANEL)
        actions.pack(fill="x", pady=(self.px(6), 0))
        if can_fill:
            self._button(actions, t("caution_confirm" if row.caution else "preview_fill"),
                         lambda: self.controller.pick(index, confirmed=True), primary=True).pack(side="left")
        self._button(actions, t("preview_copy"), lambda: self.controller.copy(index),
                     primary=not can_fill).pack(side="left", padx=(self.px(6) if can_fill else 0, 0))

    def need_preview(self, index):
        if self.bubble is not None and self.preview != index:
            self.preview = index
            self._render_bubble()

    def ask(self, kind):
        if kind == "quieter":
            self._say(t("ask_quieter"), seconds=30.0, actions=(
                (t("ask_yes"), lambda: (self._hide_toast(), self.controller.answer_offer(True))),
                (t("ask_no"), lambda: (self._hide_toast(), self.controller.answer_offer(False)))))

    def _toggle_preview(self, index):
        self.preview = None if self.preview == index else index
        self._render_bubble()

    def _avoid(self):
        snap = self.snap
        if snap is None or not snap.foreground:
            return []
        return [snap.box_rect, snap.send_rect]

    def _place_bubble(self):
        if self.bubble is None or self.pet_rect is None or self._bubble_size is None:
            return
        rect, _ = geometry.place_bubble(self.pet_rect, self._bubble_size, self._screen(), self._avoid(),
                                        gap=self.px(8))
        self._show(self.bubble_window, rect)

    # ------------------------------------------------------------ notices
    def _say(self, text, action=None, seconds=5.0, actions=()):
        """A short note next to the pet; `action` is (label, callback) for one button,
        `actions` several."""
        actions = tuple(actions) or ((action,) if action is not None else ())
        if not text or self.pet_rect is None or not self.pet_visible:
            return
        window = self.toast
        for child in window.winfo_children():
            child.destroy()
        outer = tk.Frame(window, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG, padx=self.px(10), pady=self.px(8))
        inner.pack(fill="both", expand=True)
        self._para(inner, text, self.px(300), self.small).pack(fill="x")
        if actions:
            row = tk.Frame(inner, bg=BG)
            row.pack(anchor="w", pady=(self.px(6), 0))
            for number, (label, callback) in enumerate(actions):
                self._button(row, label, callback, primary=number == 0).pack(side="left", padx=(0, self.px(6)))
        window.update_idletasks()
        size = (window.winfo_reqwidth(), window.winfo_reqheight())
        avoid = self._avoid()
        if self.bubble is not None and self.bubble_window.winfo_viewable():
            avoid = avoid + [Rect.at(self.bubble_window.winfo_x(), self.bubble_window.winfo_y(),
                                     self.bubble_window.winfo_width(), self.bubble_window.winfo_height())]
        rect, _ = geometry.place_bubble(self.pet_rect, size, self._screen(), avoid, gap=self.px(8))
        self._show(window, rect)
        if self._toast_timer is not None:
            self.root.after_cancel(self._toast_timer)
        self._toast_timer = self.root.after(int(seconds * 1000), self._hide_toast)

    def _hide_toast(self):
        if self._toast_timer is not None:
            self.root.after_cancel(self._toast_timer)
            self._toast_timer = None
        self.toast.withdraw()

    def notice(self, kind, detail=""):
        text = viewmodel.notice_text(kind, detail)
        if kind == "stepped_down":
            self._say(text, seconds=20.0, action=(
                t("step_undo"), lambda: (self._hide_toast(), self.controller.undo_step())))
        elif text:
            self._say(text, seconds=2.0 if kind == "ready" else 4.5)
        if kind == "setup" and self.dialogs == 0:
            self.open_model()

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def offer_copy(self, text, reason):
        def take():
            self.copy(text)
            self._hide_toast()
            self.controller.offer_copied()
            self._say(t("notice_copied"))
        self._say(viewmodel.offer_text(reason), action=(t("offer_copy"), take), seconds=20.0)

    def ask_paste(self):
        self.open_paste()

    # ------------------------------------------------------------ menu
    def _popup(self, event):
        if self.controller is None:
            return
        if self._menu is not None:              # the previous menu: gone now, not whenever it is collected
            try:
                self._menu.destroy()
            except tk.TclError:
                pass
        menu = self._menu = self._build_menu(
            viewmodel.menu_items(self.controller.menu(), has_tray=self._tray is not None))
        self._release_soon()
        if WIN:
            try:        # a popup menu only closes on an outside click if its owner is in front
                win32.set_foreground(win32.top_level(self.pet.winfo_id()))
            except Exception:
                pass
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        self.root.after(200, self._return_focus)

    def _return_focus(self):
        """After the menu: hand the keyboard back to Codex unless a dialog opened."""
        snap = self.snap
        if WIN and self.dialogs == 0 and snap is not None and snap.foreground and snap.window_id:
            try:
                win32.set_foreground(int(snap.window_id))
            except Exception:
                pass

    def _build_menu(self, items, parent=None):
        menu = tk.Menu(parent or self.root, tearoff=0, font=self.font)
        if parent is None:
            self._menu_vars = []
        for item in items:
            if item.separator:
                menu.add_separator()
            elif item.children:
                menu.add_cascade(label=item.label, menu=self._build_menu(item.children, menu))
            elif item.checked is not None:
                variable = tk.IntVar(value=1 if item.checked else 0)
                self._menu_vars.append(variable)
                menu.add_checkbutton(label=item.label, variable=variable, onvalue=1, offvalue=0,
                                     command=lambda i=item.id: self.command(i),
                                     state="normal" if item.enabled else "disabled")
            else:
                menu.add_command(label=item.label, command=lambda i=item.id: self.command(i),
                                 state="normal" if item.enabled and item.id else "disabled")
        return menu

    def command(self, item_id):
        controller = self.controller
        if controller is None or not item_id:
            return
        if item_id == "pause":
            controller.set_paused(True)
        elif item_id == "resume":
            controller.set_paused(False)
        elif item_id.startswith("level:"):
            controller.set_level(item_id.split(":", 1)[1])
        elif item_id == "undo_step":
            controller.undo_step()
        elif item_id == "aboutme":
            self.open_aboutme()
        elif item_id == "calibrate":
            self.open_calibration()
        elif item_id == "evidence":
            self.open_evidence()
        elif item_id == "clear":
            self.dialogs += 1
            try:
                sure = messagebox.askyesno(t("menu_clear"), t("clear_confirm"), parent=self.root)
            finally:
                self.dialogs = max(0, self.dialogs - 1)
            if sure:
                controller.clear_data()
                self._say(t("notice_cleared"))
        elif item_id == "context":
            controller.set_use_context(not controller.menu()["use_context"])
        elif item_id == "now":
            controller.pet_clicked()
        elif item_id == "retry":
            controller.retry()
        elif item_id == "undo":
            controller.undo()
        elif item_id == "paste":
            self.open_paste()
        elif item_id.startswith("example:"):
            controller.example(item_id.split(":", 1)[1])
        elif item_id == "model":
            self.open_model()
        elif item_id == "probe":
            self.open_probe()
        elif item_id == "folder":
            self._open_folder()
        elif item_id == "about":
            self._dialog_message(t("menu_about"), t("about_text", name=t("app_name"), version=__version__,
                                                    folder=str(self.settings.folder)))
        elif item_id == "hide":
            self.pinned = False
            if self.snap is not None:
                self.follow(self.snap)
        elif item_id == "show":
            self.pinned = True
            if self.snap is not None:
                self.follow(self.snap)
        elif item_id == "quit":
            self.quit()

    def _open_folder(self):
        folder = self.settings.folder
        folder.mkdir(parents=True, exist_ok=True)
        if WIN:
            try:
                os.startfile(str(folder))       # noqa: S606 - opening the user's own data folder
            except OSError:
                log.exception("cannot open the data folder")

    # ------------------------------------------------------------ dialogs
    def _dialog(self, title):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=BG, padx=self.px(16), pady=self.px(14))
        window.resizable(False, False)
        try:
            window.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self.dialogs += 1
        self.dialog_windows.append(window)

        def closed(event=None):
            if event is None or event.widget is window:
                window.unbind("<Destroy>")
                self.dialogs = max(0, self.dialogs - 1)
                if window in self.dialog_windows:
                    self.dialog_windows.remove(window)
                self._release_soon()
        window.bind("<Destroy>", closed)
        window.after(50, lambda: window.winfo_exists() and window.focus_force())
        return window

    def _watch(self, window, variable, callback):
        """Call `callback` when `variable` is written — until `window` closes.

        Never use `variable.trace_add` on its own here. A trace is a command inside the
        Tcl interpreter that holds the Python callback; the callback holds the dialog's
        widgets; the widgets hold the interpreter. Python's collector cannot see through
        the interpreter, so that ring is never freed — it must be cut by hand, while the
        interpreter is alive, which is what the <Destroy> handler does."""
        name = variable.trace_add("write", callback)

        def unwatch(event=None):
            if event is None or event.widget is window:
                try:
                    variable.trace_remove("write", name)
                except (tk.TclError, ValueError):
                    pass
        window.bind("<Destroy>", unwatch, add="+")

    def _dialog_message(self, title, text):
        self.dialogs += 1
        try:
            messagebox.showinfo(title, text, parent=self.root)
        finally:
            self.dialogs = max(0, self.dialogs - 1)

    def _row(self, window, text):
        self._label(window, text, self.small, MUTED, anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))

    def open_model(self):
        settings, controller = self.settings, self.controller
        window = self._dialog(t("model_title"))
        locked = settings.env_locked
        current = settings.model_config()
        provider = tk.StringVar(value=settings.get("provider") or (current.provider if locked else "anthropic"))
        model = tk.StringVar(value=settings.get("model") or "")
        base_url = tk.StringVar(value=settings.get("base_url") or "")
        api_key = tk.StringVar(value="")
        effort = tk.StringVar(value=settings.get("effort") or "low")
        use_context = tk.IntVar(value=1 if settings.get("use_context") else 0)
        state = "disabled" if locked else "normal"

        self._row(window, t("model_provider"))
        for name in llm.PROVIDERS:
            tk.Radiobutton(window, text=t("provider_" + name), value=name, variable=provider, bg=BG, font=self.font,
                           anchor="w", state=state, activebackground=BG, highlightthickness=0).pack(fill="x")
        self._row(window, t("model_name"))
        tk.Entry(window, textvariable=model, font=self.font, width=46, state=state).pack(fill="x")
        self._row(window, t("model_url"))
        tk.Entry(window, textvariable=base_url, font=self.font, state=state).pack(fill="x")
        has_key = bool(current.api_key) and not locked
        self._row(window, t("model_key") + (" · " + t("model_key_saved") if has_key else ""))
        tk.Entry(window, textvariable=api_key, font=self.font, show="•", state=state).pack(fill="x")
        self._row(window, t("model_effort"))
        tk.OptionMenu(window, effort, "low", "medium", "high", "none").pack(anchor="w")

        disclosure = self._label(window, "", self.small, TEXT, PANEL, justify="left",
                                 anchor="w", padx=self.px(8), pady=self.px(6))
        disclosure.pack(fill="x", pady=(self.px(12), self.px(4)))
        tk.Checkbutton(window, text=t("model_use_context"), variable=use_context, bg=BG, font=self.font,
                       anchor="w", activebackground=BG, highlightthickness=0).pack(fill="x")
        error = self._label(window, "", self.small, "#d1242f", anchor="w", justify="left")
        error.pack(fill="x")

        shown = {"destination": ""}         # what the disclosure names right now

        def show(config):
            """Name the destination `config` sends to, and remember that it was shown."""
            shown["destination"] = config.destination
            if config.destination:
                text = t("model_disclosure", host=config.origin)
            elif config.provider == "mock":
                text = t("model_disclosure_local")
            else:
                text = t("model_disclosure", host="?")
            disclosure.configure(text=self._wrapped(text, self.small, self.px(410)))

        def describe(*_):
            show(current if locked else llm.ModelConfig(provider.get(), base_url=base_url.get().strip()))
        self._watch(window, provider, describe)
        self._watch(window, base_url, describe)
        describe()
        if locked:
            error.configure(text=self._wrapped(t("model_env_locked"), self.small, self.px(410)))

        def save():
            fields = dict(provider=provider.get(), model=model.get(), base_url=base_url.get(),
                          api_key=api_key.get(), effort=effort.get())
            problem = controller.confirm_model(shown["destination"], bool(use_context.get()), fields)
            if problem:
                if problem == "destination":
                    # Requests would go somewhere other than what was on screen (an
                    # environment override): show the real destination and ask again.
                    show(settings.model_config())
                error.configure(text=self._wrapped(t("model_error_" + problem), self.small, self.px(410)))
                return
            if controller.menu()["paused"]:
                controller.set_paused(False)
            window.destroy()

        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", pady=(self.px(10), 0))
        self._button(buttons, t("model_save"), save, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))

    def open_paste(self):
        window = self._dialog(t("paste_title"))
        self._label(window, t("paste_hint"), self.font, anchor="w").pack(fill="x")
        text = tk.Text(window, width=54, height=6, font=self.font, wrap="word", relief="solid", bd=1)
        text.pack(pady=self.px(8))
        text.focus_set()

        def go():
            draft = text.get("1.0", "end").strip()
            window.destroy()
            if draft:
                self.controller.paste(draft)
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x")
        self._button(buttons, t("paste_go"), go, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))

    def open_aboutme(self):
        controller = self.controller
        window = self._dialog(t("aboutme_title"))
        self._para(window, t("aboutme_hint"), self.px(460), self.small, MUTED).pack(fill="x")
        text = tk.Text(window, width=56, height=7, font=self.font, wrap="word", relief="solid", bd=1)
        text.pack(pady=self.px(8))
        text.insert("1.0", "\n".join(controller.about.facts()))
        self._para(window, t("aboutme_kinds"), self.px(460), self.small, MUTED).pack(fill="x")
        habits = self._label(window, "", self.small, anchor="w", justify="left")
        habits.pack(fill="x", pady=(self.px(2), self.px(6)))

        def show_habits():
            lines = [f"{t('kind_' + kind)}：{t('band_' + aboutme.band(value))}"
                     for kind, value in controller.weights.items() if kind != "other"]
            habits.configure(text="   ".join(lines[:3]) + "\n" + "   ".join(lines[3:]))
        show_habits()

        problem = self._label(window, "", self.small, fg="#d1242f", anchor="w", justify="left")
        problem.pack(fill="x")

        def save():
            if controller.save_facts(text.get("1.0", "end").split("\n")) is None:
                # Windows could not encrypt it: nothing was written. Say so, keep the text.
                problem.configure(text=self._wrapped(t("aboutme_error_encrypt"), self.small, self.px(460)))
                return
            window.destroy()
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x")
        self._button(buttons, t("aboutme_save"), save, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))
        self._button(buttons, t("aboutme_reset"), lambda: (controller.reset_weights(), show_habits())).pack(side="left")

    def open_calibration(self):
        """The calibration wizard: one step at a time. The step logic is calibration.py;
        this only shows the instruction and the result."""
        controller = self.controller
        window = self._dialog(t("cal_title"))
        controller.calibrating = True
        results, memo, position = {}, {}, {"index": -1, "busy": False}
        self._para(window, t("cal_intro"), self.px(480), self.small, MUTED).pack(fill="x")
        title = self._label(window, "", self.bold, anchor="w")
        title.pack(fill="x", pady=(self.px(10), self.px(2)))
        body = self._label(window, "", self.font, anchor="w", justify="left", height=6)
        body.pack(fill="x")
        note = self._label(window, "", self.small, REASON, anchor="w", justify="left")
        note.pack(fill="x", pady=(self.px(4), 0))
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", pady=(self.px(10), 0))

        def closed(event=None):
            if event is None or event.widget is window:
                controller.calibrating = False
        window.bind("<Destroy>", closed, add="+")

        def finish():
            for step in calibration.STEP_NAMES:
                results.setdefault(step, {"ok": False, "skipped": True})
            entry = controller.finish_calibration(results, memo)
            controller.calibrating = False
            if not window.winfo_exists():
                return
            title.configure(text=t("cal_title"))
            body.configure(text=self._wrapped("\n".join(viewmodel.calibration_lines(entry)), self.font, self.px(480)),
                           height=len(calibration.STEPS) + 5)
            note.configure(text="")
            for child in buttons.winfo_children():
                child.destroy()
            self._button(buttons, t("close"), window.destroy, primary=True).pack(side="right")

        def advance():
            position["index"] += 1
            if position["index"] >= len(calibration.STEPS):
                finish()
                return
            step, _, _ = calibration.STEPS[position["index"]]
            title.configure(text=f"{position['index'] + 1}/{len(calibration.STEPS)}  {t('cal_step_' + step)}")
            body.configure(text=self._wrapped(t("cal_do_" + step, phrase=calibration.PHRASE) if step == "read"
                                              else t("cal_do_" + step), self.font, self.px(480)))
            note.configure(text="")
            controller.calibration_step(step, memo, lambda result: None, prepare=True)

        def checked(step, required, result):
            position["busy"] = False
            if not window.winfo_exists():
                return
            results[step] = result
            if result.get("ok"):
                advance()
            elif result.get("fatal"):
                finish()
            else:
                note.configure(text=self._wrapped(t("cal_failed_required" if required else "cal_failed"),
                                                  self.small, self.px(480)))

        def check():
            if position["busy"] or not 0 <= position["index"] < len(calibration.STEPS):
                return
            step, _, optional = calibration.STEPS[position["index"]]
            position["busy"] = True
            note.configure(text=t("cal_working"))
            controller.calibration_step(step, memo, lambda result: checked(step, not optional, result))

        def skip():
            if position["busy"] or not 0 <= position["index"] < len(calibration.STEPS):
                return
            step, _, optional = calibration.STEPS[position["index"]]
            if not optional:
                finish()                    # nothing after a required step can be trusted
                return
            results[step] = {"ok": False, "skipped": True}
            advance()

        self._button(buttons, t("cal_check"), check, primary=True).pack(side="right")
        self._button(buttons, t("cal_skip"), skip).pack(side="right", padx=self.px(8))
        self._button(buttons, t("cal_finish"), finish).pack(side="left")
        advance()

    def open_evidence(self):
        window = self._dialog(t("evidence_title"))
        self._para(window, t("evidence_intro"), self.px(560), self.small, MUTED).pack(fill="x")
        output = tk.Text(window, width=80, height=20, font=("Consolas" if WIN else "TkFixedFont", 9),
                         wrap="none", relief="solid", bd=1)
        output.pack(pady=self.px(8))
        output.insert("1.0", t("probe_running"))
        note = self._label(window, "", self.small, MUTED, anchor="w")
        note.pack(fill="x")
        holder = {}

        def show(bundle):
            holder["bundle"] = bundle
            if window.winfo_exists():
                output.delete("1.0", "end")
                output.insert("1.0", evidence.as_text(bundle))

        def save():
            if "bundle" not in holder:
                return
            path = filedialog.asksaveasfilename(parent=window, defaultextension=".zip",
                                                initialfile="banxing-evidence.zip", filetypes=[("zip", "*.zip")])
            if path:
                evidence.write(path, holder["bundle"])
                note.configure(text=t("evidence_saved"))
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x")
        self._button(buttons, t("evidence_save"), save, primary=True).pack(side="left")
        self._button(buttons, t("close"), window.destroy).pack(side="right")
        self.controller.collect_evidence(show)

    def open_probe(self):
        window = self._dialog(t("probe_title"))
        self._para(window, t("probe_intro"), self.px(540)).pack(fill="x")
        try_fill = tk.IntVar(value=0)
        tk.Checkbutton(window, text=t("probe_fill"), variable=try_fill, bg=BG, font=self.small, anchor="w",
                       activebackground=BG, highlightthickness=0).pack(fill="x", pady=(self.px(6), 0))
        output = tk.Text(window, width=78, height=18, font=("Consolas" if WIN else "TkFixedFont", 9),
                         wrap="word", relief="solid", bd=1)
        output.pack(pady=self.px(8))
        note = self._label(window, "", self.small, MUTED, anchor="w")
        note.pack(fill="x")

        def show(text):
            if not window.winfo_exists():
                return
            output.delete("1.0", "end")
            output.insert("1.0", text)

        def run():
            show(t("probe_running"))
            self.controller.probe(bool(try_fill.get()), lambda report, context: show(
                viewmodel.probe_text(report, context, __version__)))

        def learn():
            self.controller.learn_placeholder(lambda ok: note.winfo_exists() and note.configure(
                text=t("probe_learned" if ok else "probe_nothing_to_learn")))

        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x")
        self._button(buttons, t("probe_run"), run, primary=True).pack(side="left")
        self._button(buttons, t("probe_copy"), lambda: self.copy(output.get("1.0", "end").strip())).pack(
            side="left", padx=self.px(8))
        self._button(buttons, t("probe_learn"), learn).pack(side="left")
        self._button(buttons, t("close"), window.destroy).pack(side="right")

    # ------------------------------------------------------------ tray
    def _start_tray(self):
        """A notification-area icon, so the pet can be reached while it is hidden.
        Optional: without pystray/Pillow the pet simply stays visible."""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        try:
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((4, 4, 60, 60), fill=(79, 140, 255, 255))
            for x in (23, 41):
                draw.ellipse((x - 6, 22, x + 6, 34), fill=(255, 255, 255, 255))
                draw.ellipse((x - 3, 25, x + 3, 31), fill=(31, 35, 40, 255))

            def entries():
                state = self._tray_state
                if state is None:
                    return (pystray.MenuItem(t("menu_quit"), lambda: self._calls.put(self.quit)),)
                out = []
                for item in viewmodel.tray_items(*state):
                    if item.separator:
                        out.append(pystray.Menu.SEPARATOR)
                    else:
                        out.append(pystray.MenuItem(item.label, action(item.id)))
                return tuple(out)

            def action(item_id):
                # pystray looks at the number of parameters: exactly (icon, item).
                return lambda icon, entry: self._calls.put(lambda: self.command(item_id))

            self._tray = pystray.Icon("banxing", image, t("app_name"), pystray.Menu(entries))
            self._tray.run_detached()
        except Exception:
            log.exception("no tray icon")
            self._tray = None
