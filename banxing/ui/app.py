"""The Tk shell: the figure, the bubble, notices, the menu, three dialogs, the tray icon.

Thin on purpose — what to show is decided in viewmodel.py and watch/; this file only
draws it and reports clicks. Tk is used because it ships with Python (no extra
runtime to bundle).

The figure, the bubble and the notices are borderless tool windows that do not take
the keyboard (on Windows: WS_EX_NOACTIVATE, applied after the window has been mapped
once — Tk creates the native wrapper lazily and would drop the style otherwise), so
a bubble never interrupts typing in the chat the user is working in. Dialogs are
ordinary windows and do take focus; that is intended.

Threads. Tk belongs to ONE thread — the one that created the interpreter — for its
whole life, including its end. Three rules follow, and PetApp keeps them itself:

  * every Tk call is made on that thread; other threads (tray icon, workers) only
    put work into a queue that `_pump` empties;
  * the interpreter is torn down on that thread, in `destroy()`;
  * and it is also FREED there. Widgets, variables and callbacks refer to each other
    in cycles, so dropping the last name does not free them: they wait for Python's
    cycle collector, which runs on whichever thread happens to allocate next. If
    that is a worker, Tcl finds its interpreter being deleted by a stranger and
    aborts the whole process ("Tcl_AsyncDelete: async handler deleted by the wrong
    thread" — seen on the first Windows CI run of v0.3). `destroy()` therefore lets
    go of every Tk object and collects at once (`release_tk`).

STATUS: this file was rewritten for the supervising app and HAS NOT BEEN RUN ON
WINDOWS. It was built and looked at on Linux only. The overlay technique and the
thread rules are the ones v0.3 passed the Windows runner with (legacy/v0.3).
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

from .. import APP_NAME, __version__, geometry, llm
from ..geometry import Rect
from ..watch import sources
from ..watch.driver import View
from . import viewmodel
from .strings import t

log = logging.getLogger(__name__)
WIN = sys.platform == "win32"
if WIN:
    from .. import winshell

BG, BORDER, TEXT, MUTED = "#ffffff", "#d0d7de", "#1f2328", "#656d76"
ACCENT, HOVER, REASON, PANEL, ERROR = "#0969da", "#f3f4f6", "#9a6700", "#f6f8fa", "#d1242f"
KEY = "#fe01fd"                     # transparent colour of the figure's window
BODY = {"watching": "#4f8cff", "thinking": "#4f8cff", "paused": "#9aa4b2", "idle": "#9aa4b2", "problem": "#4f8cff"}


def release_tk():
    """Free Tk objects that are no longer referenced — now, on the calling thread.
    Call it on the Tk thread after windows were destroyed (see "Threads" above)."""
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
        self.watcher = None
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

        self.pet_rect = None
        self.pet_visible = False
        self.hidden = False                 # hidden to the tray by the user
        self.bubble = None                  # watch.session.Bubble on screen
        self.sources_open = False
        self._bubble_size = None
        self._ledger = {}                   # the open ledger window and how to refresh it
        self.dialogs = 0
        self.dialog_windows = []
        self._drag = None
        self._tick = 0
        self._look = "idle"
        self._toast_timer = None
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

    # ------------------------------------------------------------ plumbing
    def px(self, value):
        return int(round(value * self.scale))

    def attach(self, watcher, executor=None):
        self.watcher = watcher
        self.executor = executor
        self.changed()

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
            winshell.make_overlay(winshell.top_level(window.winfo_id()))
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
        if WIN and self.pet_visible:
            try:
                return winshell.work_area(winshell.top_level(self.pet.winfo_id()))
            except Exception:
                pass
        return Rect(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def owned(self):
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
        if self.watcher is not None:
            try:
                self.watcher.tick()
            except Exception:
                log.exception("looking at the watched conversation failed")
        self.root.after(40, self._pump)

    def _animate(self):
        if self._quit:
            return
        self._tick += 1
        if self._look == "thinking":
            self._draw_pet()
        if self._tick % 3 == 0 and self.watcher is not None:
            self._tray_state = (self.watcher.state(), self.pet_visible)
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
        # The watcher keeps working (and logging) without a view; it must not keep
        # this object's windows alive, nor call into them.
        watcher, self.watcher, self.executor = self.watcher, None, None
        if watcher is not None and getattr(watcher, "view", None) is self:
            watcher.view = View()
        for name, value in list(vars(self).items()):
            if _holds_tk(value):
                setattr(self, name, type(value)() if isinstance(value, (list, dict, set)) else None)
        del watcher, value
        release_tk()

    # ------------------------------------------------------------ the figure
    def _state_look(self):
        state = self.watcher.state() if self.watcher is not None else {}
        if not state.get("active"):
            return "idle"
        if state.get("problem") or state.get("state") in ("diverged", "consent") or state.get("error"):
            return "problem"
        if state.get("paused"):
            return "paused"
        return "thinking" if state.get("auditing") else "watching"

    def _draw_pet(self):
        canvas, size, look = self.canvas, self.size, self._look
        canvas.delete("all")
        pad = self.px(4)
        canvas.create_oval(pad, pad, size - pad, size - pad, fill=BODY.get(look, BODY["idle"]), outline="")
        eye_y, eye_r = int(size * 0.44), max(2, self.px(5))
        for centre in (int(size * 0.36), int(size * 0.64)):
            if look in ("paused", "idle"):
                canvas.create_line(centre - eye_r, eye_y, centre + eye_r, eye_y, fill="#ffffff", width=self.px(2))
            else:
                canvas.create_oval(centre - eye_r, eye_y - eye_r, centre + eye_r, eye_y + eye_r,
                                   fill="#ffffff", outline="")
                lift = -self.px(2) if look == "thinking" else 0
                dot = max(1, eye_r // 2)
                canvas.create_oval(centre - dot, eye_y - dot + lift, centre + dot, eye_y + dot + lift,
                                   fill="#1f2328", outline="")
        if look == "thinking":
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
        if look == "problem" or self.bubble is not None:
            r = self.px(9)
            cx, cy = size - pad - r, pad + r
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ERROR if self.bubble is not None else "#bf8700",
                               outline="#ffffff", width=self.px(1))
            canvas.create_text(cx, cy, text="!" if self.bubble is not None else "?", fill="#ffffff", font=self.bold)

    def _home_rect(self):
        saved = self.settings.get("pet_free")
        screen = Rect(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            rect = Rect.at(saved[0], saved[1], self.size, self.size)
        else:
            rect = Rect.at(screen.right - self.size - self.px(48), screen.bottom - self.size - self.px(120),
                           self.size, self.size)
        return geometry.clamp_into(rect, screen)

    def place(self):
        """Show the figure where it lives (or take it away when hidden to the tray —
        unless there is something to show)."""
        if self.hidden and self.bubble is None and not self.dialogs:
            if self.pet_visible:
                self.pet.withdraw()
                self.pet_visible = False
                self._hide_toast()
            return
        if not self.pet_visible:
            self.pet_rect = self.pet_rect or self._home_rect()
            self._show(self.pet, self.pet_rect)
            self.pet_visible = True

    def greet(self):
        self.place()
        ready = self.watcher is not None and self.watcher.state().get("model")
        self._say(t("notice_started" if ready else "notice_setup"), seconds=8.0)

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
        if drag["moved"]:
            self.settings.set(pet_free=[self.pet_rect.left, self.pet_rect.top])
        elif self.watcher is not None:
            # A click on the figure: look now — or, with nothing to look at, choose a conversation.
            if self.watcher.active:
                self.watcher.look_now()
            elif self.watcher.state().get("model"):
                self.open_watch()
            else:
                self.open_model()

    # ------------------------------------------------------------ view contract
    def changed(self):
        self._look = self._state_look()
        self._draw_pet()
        refresh = self._ledger.get("refresh")
        if refresh is not None:
            refresh()

    def show_bubble(self, bubble):
        self.bubble, self.sources_open, self._bubble_size = bubble, False, None
        self._hide_toast()
        self.place()
        self._render_bubble()
        self._draw_pet()

    def hide_bubble(self, reason):
        self.bubble = None
        self.bubble_window.withdraw()
        self._draw_pet()
        self.place()

    def notice(self, kind, detail=""):
        text = viewmodel.notice_text(kind, detail)
        if text:
            self.place()
            self._say(text, seconds=7.0 if kind in ("watch_branch", "watch_consent") else 4.5)

    # ------------------------------------------------------------ small parts
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

    def _row(self, window, text):
        self._label(window, text, self.small, MUTED, anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))

    # ------------------------------------------------------------ the bubble
    def _render_bubble(self):
        window = self.bubble_window
        for child in window.winfo_children():
            child.destroy()
        view = viewmodel.nudge_view(self.bubble)
        watcher = self.watcher
        width, pad = self.px(430), self.px(10)
        wrap = width - 2 * pad - self.px(8)
        outer = tk.Frame(window, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG, padx=pad, pady=self.px(8))
        inner.pack(fill="both", expand=True)
        head = tk.Frame(inner, bg=BG)
        head.pack(fill="x")
        close = self._label(head, "✕", self.small, MUTED, cursor="hand2", padx=self.px(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda event: watcher.dismiss())
        self._label(head, view.kind, self.small, REASON).pack(side="left")
        self._para(inner, view.title, wrap, self.bold).pack(fill="x", pady=(self.px(2), 0))
        for line in view.lines:
            self._para(inner, line, wrap).pack(fill="x", pady=(self.px(4), 0))
        box = tk.Frame(inner, bg=PANEL, padx=self.px(8), pady=self.px(6))
        box.pack(fill="x", pady=(self.px(8), 0))
        self._label(box, t("nudge_ask"), self.small, ACCENT, PANEL, anchor="w").pack(fill="x")
        self._para(box, view.ask, wrap - self.px(16), self.font, TEXT, PANEL).pack(fill="x", pady=(self.px(2), 0))
        if view.caution:
            self._para(inner, "⚠ " + view.caution, wrap, self.small, REASON).pack(fill="x", pady=(self.px(4), 0))
        if self.sources_open:
            for line in view.evidence:
                self._para(inner, line, wrap, self.small, MUTED).pack(fill="x", pady=(self.px(3), 0))
        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", pady=(self.px(8), 0))
        commands = {"sources": self._toggle_sources, "copy": self._copy_ask,
                    "intended": lambda: self._answered(watcher.intended), "wrong": lambda: self._answered(watcher.wrong)}
        labels = {"sources": "nudge_sources_hide" if self.sources_open else "nudge_sources", "copy": "nudge_copy",
                  "intended": "nudge_intended", "wrong": "nudge_wrong"}
        for name in view.buttons:
            self._button(row, t(labels[name]), commands[name], primary=name == "copy").pack(
                side="left", padx=(0, self.px(6)))
        self._para(inner, view.footer, wrap, self.small, MUTED).pack(fill="x", pady=(self.px(6), 0))
        window.update_idletasks()
        self._bubble_size = (max(width, window.winfo_reqwidth()), window.winfo_reqheight())
        self._place_bubble()

    def _place_bubble(self):
        if self.bubble is None or self.pet_rect is None or self._bubble_size is None:
            return
        rect, _ = geometry.place_bubble(self.pet_rect, self._bubble_size, self._screen(), [], gap=self.px(8))
        self._show(self.bubble_window, rect)

    def _toggle_sources(self):
        if self.bubble is not None:
            self.sources_open = not self.sources_open
            self._render_bubble()

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_ask(self):
        """The question goes to the clipboard — the user pastes it to the main model
        themselves. Nothing is typed into any application by this app."""
        if self.bubble is None:
            return
        self.copy(self.bubble.finding.ask)
        self.watcher.took_back()
        self._say(t("notice_copied"), seconds=6.0)

    def _answered(self, answer):
        """"Intended" / "does not hold": taken down at once (no dialog, the keyboard
        stays where it is); a line of the user's own can be added afterwards."""
        answer()
        self._say(t("nudge_note_hint"), action=(t("nudge_note_title"), self._ask_note), seconds=12.0)

    def _ask_note(self):
        self._hide_toast()
        window = self._dialog(t("nudge_note_title"))
        self._para(window, t("nudge_note_hint"), self.px(380), self.small, MUTED).pack(fill="x")
        text = tk.Text(window, width=46, height=3, font=self.font, wrap="word", relief="solid", bd=1)
        text.pack(pady=self.px(8))
        text.focus_set()

        def save():
            note = text.get("1.0", "end").strip()
            window.destroy()
            self.watcher.add_note(note)
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x")
        self._button(buttons, t("ok"), save, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))

    # ------------------------------------------------------------ notices
    def _say(self, text, action=None, seconds=5.0):
        """A short note next to the figure; `action` is (label, callback) for one button."""
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
        if action is not None:
            self._button(inner, action[0], action[1], primary=True).pack(anchor="w", pady=(self.px(6), 0))
        window.update_idletasks()
        size = (window.winfo_reqwidth(), window.winfo_reqheight())
        avoid = [Rect.at(w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())
                 for w in (self.bubble_window,) if w.winfo_viewable()]
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

    # ------------------------------------------------------------ menu
    def _popup(self, event):
        if self.watcher is None:
            return
        if self._menu is not None:              # the previous menu: gone now, not whenever it is collected
            try:
                self._menu.destroy()
            except tk.TclError:
                pass
        menu = self._menu = self._build_menu(viewmodel.menu_items(self.watcher.state(), has_tray=self._tray is not None))
        self._release_soon()
        if WIN:
            try:        # a popup menu only closes on an outside click if its owner is in front
                winshell.set_foreground(winshell.top_level(self.pet.winfo_id()))
            except Exception:
                pass
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_menu(self, items):
        menu = tk.Menu(self.root, tearoff=0, font=self.font)
        for item in items:
            if item.separator:
                menu.add_separator()
            else:
                menu.add_command(label=item.label, command=lambda i=item.id: self.command(i),
                                 state="normal" if item.enabled and item.id else "disabled")
        return menu

    def command(self, item_id):
        watcher = self.watcher
        if watcher is None or not item_id:
            return
        if item_id == "watch":
            self.open_watch()
        elif item_id == "look":
            watcher.look_now()
        elif item_id == "ledger":
            self.open_ledger()
        elif item_id == "paste":
            self._paste_again()
        elif item_id == "pause":
            watcher.pause()
        elif item_id == "resume":
            watcher.resume()
        elif item_id == "stop":
            watcher.stop()
        elif item_id == "model":
            self.open_model()
        elif item_id == "folder":
            self._open_folder()
        elif item_id == "clear":
            self.dialogs += 1
            try:
                sure = messagebox.askyesno(t("menu_clear"), t("clear_confirm"), parent=self.root)
            finally:
                self.dialogs = max(0, self.dialogs - 1)
            if sure:
                watcher.clear_data()
                self._say(t("notice_cleared"))
        elif item_id == "about":
            self.dialogs += 1
            try:
                messagebox.showinfo(t("menu_about"), t("about_text", name=t("app_name"), version=__version__,
                                                       folder=str(self.settings.folder)), parent=self.root)
            finally:
                self.dialogs = max(0, self.dialogs - 1)
        elif item_id == "hide":
            self.hidden = True
            self.place()
        elif item_id == "show":
            self.hidden = False
            self.place()
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

    def _clipboard(self):
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""

    def _paste_again(self):
        text = self._clipboard()
        problem = self.watcher.paste(text) if text.strip() else "clipboard"
        self._say(t("notice_pasted") if not problem
                  else t("watch_error_source", why=viewmodel.source_error_text(problem)))

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

    def open_model(self):
        """Which model reads alongside. Saving this sends nothing: a conversation is
        agreed to one at a time, in the dialog where it is chosen."""
        settings, watcher = self.settings, self.watcher
        window = self._dialog(t("model_title"))
        wide = self.px(430)
        locked = settings.env_locked
        current = settings.model_config()
        provider = tk.StringVar(value=settings.get("provider") or (current.provider if locked else "anthropic"))
        model = tk.StringVar(value=settings.get("model") or "")
        base_url = tk.StringVar(value=settings.get("base_url") or "")
        api_key = tk.StringVar(value="")
        effort = tk.StringVar(value=settings.get("effort") or "medium")
        state = "disabled" if locked else "normal"
        self._para(window, t("model_note"), wide, self.small, MUTED).pack(fill="x")
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
        where = self._label(window, "", self.small, TEXT, PANEL, justify="left", anchor="w",
                            padx=self.px(8), pady=self.px(6))
        where.pack(fill="x", pady=(self.px(12), self.px(4)))
        error = self._label(window, "", self.small, ERROR, anchor="w", justify="left")
        error.pack(fill="x")

        def describe(*_):
            config = current if locked else llm.ModelConfig(provider.get(), base_url=base_url.get().strip())
            text = t("model_where", host=config.origin or "?") if config.provider != "mock" else t("model_where_local")
            where.configure(text=self._wrapped(text, self.small, wide - self.px(16)))
        self._watch(window, provider, describe)
        self._watch(window, base_url, describe)
        describe()
        if locked:
            error.configure(text=self._wrapped(t("model_env_locked"), self.small, wide))

        def save():
            if locked:
                if effort.get() in ("low", "medium", "high", "none"):
                    settings.set(effort=effort.get())
                window.destroy()
                return
            try:
                watcher.save_model(provider=provider.get(), model=model.get(), base_url=base_url.get(),
                                   api_key=api_key.get(), effort=effort.get())
            except ValueError as problem:
                error.configure(text=self._wrapped(t("model_error_" + str(problem)), self.small, wide))
                return
            window.destroy()

        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", pady=(self.px(10), 0))
        self._button(buttons, t("model_save"), save, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))

    def open_watch(self):
        """Choose the conversation to watch and its range — and agree to exactly that.
        Nothing is read for sending before the button is pressed."""
        watcher, settings = self.watcher, self.settings
        window = self._dialog(t("watch_title"))
        wide = self.px(520)
        self._para(window, t("watch_intro"), wide, self.small, MUTED).pack(fill="x")
        self._row(window, t("watch_file"))
        line = tk.Frame(window, bg=BG)
        line.pack(fill="x")
        path = tk.Entry(line, font=self.font, width=52)
        path.pack(side="left", fill="x", expand=True)
        found = {"kind": "", "path": "", "listings": [], "text": ""}
        chooser = tk.Listbox(window, font=self.font, height=5, activestyle="none", exportselection=False,
                             relief="solid", bd=1)
        error = self._label(window, "", self.small, ERROR, anchor="w", justify="left")

        def fail(text):
            if error.winfo_exists():
                error.configure(text=self._wrapped(text, self.small, wide))

        def offer(kind, where, listings, problem="", text=""):
            if not window.winfo_exists():
                return
            found.update(kind=kind, path=where, listings=list(listings), text=text)
            chooser.delete(0, "end")
            for item in listings:
                title = item.title or t("watch_untitled")
                chooser.insert("end", t("watch_listing", title=title, turns=item.turns) if item.turns else title)
            if listings:
                chooser.selection_set(0)
            fail(t("watch_error_source", why=viewmodel.source_error_text(problem)) if problem else "")

        def load():
            where = path.get().strip().strip('"')
            if where:
                watcher.listing(where, lambda kind, listings, problem: offer(kind, where, listings, problem))

        def browse():
            chosen = filedialog.askopenfilename(parent=window)
            if chosen:
                path.delete(0, "end")
                path.insert(0, chosen)
                load()

        def clipboard():
            text = self._clipboard()
            if not text.strip():
                offer("", "", [], "clipboard")
                return
            try:
                transcript = sources.read_pasted(text)
            except sources.SourceError as problem:
                offer("", "", [], problem.kind)
                return
            offer(sources.PASTED, "", [sources.Listing(id="", title=t("watch_pasted"), turns=len(transcript.turns))],
                  text=text)

        self._button(line, t("watch_load"), load).pack(side="left", padx=(self.px(6), 0))
        self._button(line, t("watch_browse"), browse).pack(side="left", padx=(self.px(6), 0))
        more = tk.Frame(window, bg=BG)
        more.pack(fill="x", pady=(self.px(6), 0))
        self._button(more, t("watch_clipboard"), clipboard).pack(side="left")
        self._button(more, t("watch_sessions"), lambda: watcher.codex_sessions(
            lambda kind, listings: offer(kind, "", listings, "" if listings else "missing"))).pack(
                side="left", padx=(self.px(6), 0))
        self._row(window, t("watch_pick"))
        chooser.pack(fill="x")
        self._row(window, t("watch_first"))
        first = tk.Entry(window, font=self.font, width=6)
        first.insert(0, "1")
        first.pack(anchor="w")
        persist = tk.IntVar(value=0)
        tk.Checkbutton(window, text=t("watch_persist"), variable=persist, bg=BG, font=self.small, anchor="w",
                       activebackground=BG, highlightthickness=0).pack(fill="x", pady=(self.px(8), 0))
        disclosure = self._label(window, "", self.small, TEXT, PANEL, justify="left", anchor="w",
                                 padx=self.px(8), pady=self.px(6))
        disclosure.pack(fill="x", pady=(self.px(8), self.px(4)))
        error.pack(fill="x")
        shown = {"destination": None}

        def first_turn():
            try:
                return max(1, int(first.get().strip() or "1"))
            except ValueError:
                return 1

        def describe(event=None):
            """Name where the conversation would go, and remember that this was on screen."""
            config = settings.model_config()
            shown["destination"] = config.destination if config.ready else None
            if config.ready and config.destination:
                text = t("watch_disclosure", host=config.origin, model=config.model_name, first=first_turn())
            elif config.ready:
                text = t("watch_disclosure_local")
            else:
                text = t("watch_error_model")
            disclosure.configure(text=self._wrapped(text, self.small, wide - self.px(16)))
        first.bind("<KeyRelease>", describe)
        describe()

        def started(problem):
            if not problem:
                if window.winfo_exists():
                    window.destroy()
                self._say(t("notice_watch_started"))
                return
            if problem == "destination":
                describe()
            known = ("model", "destination", "first")
            fail(t("watch_error_" + problem) if problem in known
                 else t("watch_error_source", why=viewmodel.source_error_text(problem)))

        def agree():
            picked = chooser.curselection()
            if not found["listings"] or not picked:
                fail(t("watch_error_choose"))
                return
            item = found["listings"][picked[0]]
            kind = found["kind"]
            if kind in (sources.SESSION, sources.CLAUDE):
                ref = sources.SourceRef(kind, path=found["path"] or item.id)
            elif kind in (sources.APPSERVER, sources.PASTED):
                ref = sources.SourceRef(kind, conversation=item.id)
            else:
                ref = sources.SourceRef(kind, path=found["path"], conversation=item.id)
            watcher.begin(ref, shown["destination"], first_turn(), bool(persist.get()), started, text=found["text"])

        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", pady=(self.px(10), 0))
        self._button(buttons, t("watch_agree"), agree, primary=True).pack(side="right")
        self._button(buttons, t("cancel"), window.destroy).pack(side="right", padx=self.px(8))

    def _text_dialog(self, title, text):
        dialog = self._dialog(title)
        box = tk.Text(dialog, width=84, height=24, font=self.small, wrap="word", relief="solid", bd=1)
        box.pack()
        box.insert("1.0", text)
        box.configure(state="disabled")
        self._button(dialog, t("close"), dialog.destroy, primary=True).pack(anchor="e", pady=(self.px(8), 0))

    def open_ledger(self):
        """The ledger: what the watcher holds, where each entry comes from, what was
        sent — and the user's own controls over all of it."""
        old = self._ledger.get("window")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        watcher = self.watcher
        window = self._dialog(t("ledger_title"))
        wide = self.px(600)
        output = tk.Text(window, width=78, height=26, font=self.font, wrap="word", relief="solid", bd=1)
        output.pack()
        note = self._label(window, "", self.small, REASON, anchor="w", justify="left")
        note.pack(fill="x", pady=(self.px(6), 0))
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", pady=(self.px(8), 0))
        extra = tk.Frame(window, bg=BG)
        extra.pack(fill="x", pady=(self.px(6), 0))
        keep = tk.IntVar(value=1 if watcher.persist else 0)
        shown = {"destination": None}

        def correct():
            session = watcher.session
            if session is None:
                return
            dialog = self._dialog(t("ledger_correct"))
            self._para(dialog, t("ledger_correct_hint"), self.px(420), self.small, MUTED).pack(fill="x")
            text = tk.Text(dialog, width=52, height=4, font=self.font, wrap="word", relief="solid", bd=1)
            text.pack(pady=self.px(8))
            text.insert("1.0", session.ledger.goal.text)
            text.focus_set()

            def save():
                goal = text.get("1.0", "end").strip()
                dialog.destroy()
                watcher.correct_goal(goal)
            row = tk.Frame(dialog, bg=BG)
            row.pack(fill="x")
            self._button(row, t("ledger_correct_save"), save, primary=True).pack(side="right")
            self._button(row, t("cancel"), dialog.destroy).pack(side="right", padx=self.px(8))

        def last(name):
            audit = watcher.session.last if watcher.session is not None else None
            return getattr(audit, name, "") if audit is not None else ""

        def agree_again():
            if watcher.agree(shown["destination"]):
                refresh()                       # the real destination is shown now; ask again

        def stop():
            watcher.stop()
            if window.winfo_exists():
                window.destroy()

        def refresh():
            if not window.winfo_exists():
                return
            session, state = watcher.session, watcher.state()
            config = self.settings.model_config()
            lines = [t("status_none")] if session is None else (
                ([watcher.title, ""] if watcher.title else [])
                + viewmodel.ledger_lines(session.ledger, session.findings, session.usage, config.origin, session.last))
            output.configure(state="normal")
            output.delete("1.0", "end")
            output.insert("1.0", "\n".join(lines))
            output.configure(state="disabled")
            for frame in (buttons, extra):
                for child in frame.winfo_children():
                    child.destroy()
            status = viewmodel.status_text(state)
            if state.get("state") == "consent":
                shown["destination"] = config.destination if config.ready else None
                status += "\n" + (t("watch_disclosure", host=config.origin, model=config.model_name, first=session.first)
                                  if config.destination else t("watch_disclosure_local"))
            note.configure(text=self._wrapped(status, self.small, wide))
            if session is None:
                self._button(buttons, t("close"), window.destroy, primary=True).pack(side="right")
                return
            if state.get("state") == "diverged":
                self._button(buttons, t("ledger_accept_branch"), watcher.accept_branch, primary=True).pack(side="left")
            elif state.get("state") == "consent":
                self._button(buttons, t("watch_agree_again"), agree_again, primary=True).pack(side="left")
            else:
                self._button(buttons, t("menu_look"), watcher.look_now, primary=True).pack(side="left")
            self._button(buttons, t("ledger_correct"), correct).pack(side="left", padx=self.px(6))
            self._button(buttons, t("menu_resume" if session.paused else "menu_pause"),
                         watcher.resume if session.paused else watcher.pause).pack(side="left")
            self._button(buttons, t("close"), window.destroy).pack(side="right")
            self._button(buttons, t("menu_stop"), stop).pack(side="right", padx=self.px(6))
            keep.set(1 if watcher.persist else 0)
            tk.Checkbutton(extra, text=t("ledger_persist_on"), variable=keep, bg=BG, font=self.small,
                           activebackground=BG, highlightthickness=0,
                           command=lambda: watcher.set_persist(bool(keep.get()))).pack(side="left")
            if last("request"):
                self._button(extra, t("ledger_sent"), lambda: self._text_dialog(t("ledger_sent"), last("request"))).pack(
                    side="right")
            if last("independent"):
                self._button(extra, t("ledger_blind"),
                             lambda: self._text_dialog(t("ledger_blind"), last("independent"))).pack(
                                 side="right", padx=self.px(6))

        def closed(event=None):
            if event is None or event.widget is window:
                self._ledger = {}
        window.bind("<Destroy>", closed, add="+")
        self._ledger = {"window": window, "refresh": refresh}
        refresh()

    # ------------------------------------------------------------ tray
    def _start_tray(self):
        """A notification-area icon, so the app can be reached while the figure is
        hidden. Optional: without pystray/Pillow the figure simply stays visible."""
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
