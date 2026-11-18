"""What the shell shows, decided without any toolkit so it can be tested."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .. import examples
from ..levels import LEAD, ORDER, WALK
from .strings import t


# ------------------------------------------------------------------ text layout
_TOKEN = re.compile(r"[\x21-\x7e]+|\s+|.", re.S)
_NOT_AT_LINE_START = set("，。、；：？！）》」』”’…,.;:?!)]}%")


def wrap_text(text, measure, width):
    """Break text into lines no wider than `width` (as judged by `measure`).

    Toolkits wrap at spaces only, which tears mixed Chinese/English text apart
    ("做个 HTML" / "网页来记账…"). Here Chinese may break between any two
    characters, a Latin word stays whole, and closing punctuation never starts a
    line. Existing line breaks are kept.
    """
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for token in _TOKEN.findall(paragraph):
            if not line and token.isspace():
                continue
            if not line or measure(line + token) <= width or token in _NOT_AT_LINE_START:
                line += token
            else:
                lines.append(line.rstrip())
                line = "" if token.isspace() else token
        lines.append(line.rstrip())
    return "\n".join(lines)


# ------------------------------------------------------------------ bubble
@dataclass(frozen=True)
class Row:
    approach: str
    get: str            # "拿到带公式的记账表"
    fits: str           # shown in the preview
    prompt: str
    tag: str = ""       # 主线 / 支线
    why: str = ""       # under the main line: "因为：…"
    caution: str = ""   # a warning from the safety gate; such a row needs the preview first
    # The prompt in its parts, each with a small label, for the preview:
    parts: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HintRow:
    label: str          # what is missing
    text: str           # the sentence (or the stub to complete) that a click adds
    is_question: bool


@dataclass(frozen=True)
class BubbleView:
    kind: str           # routes | hints
    title: str
    reason: str         # one line under the title; "" if none
    rows: Tuple[Row, ...]
    can_fill: bool
    hints: Tuple[HintRow, ...] = ()
    footer: str = ""    # what this was based on
    more: bool = False  # "换一批" is available


def _caution(codes):
    known = [t("caution_" + code) for code in codes if code in ("privilege", "destructive")]
    return "；".join(known) if known else (t("caution_other") if codes else "")


def basis_text(bubble):
    """One line saying what the suggestion was based on."""
    if bubble.source == "example":
        return t("basis_example")
    used, turns, masked = bubble.basis
    text = t("basis_chat", turns=turns) if used and turns else t("basis_draft")
    if masked:
        text += " · " + t("basis_masked", count=masked)
    if bubble.assumes:
        text += " · " + t("basis_assumes", what=bubble.assumes)
    return text


def bubble_view(bubble, can_fill):
    reason = bubble.reason
    if bubble.source == "example":
        reason = t("bubble_example_note")
    if bubble.kind == "hints":
        hints = tuple(HintRow(h.label, h.addition, bool(h.ask)) for h in bubble.hints)
        return BubbleView(kind="hints", title=t("hints_title_fill" if can_fill else "hints_title_copy"),
                          reason=reason, rows=(), can_fill=can_fill, hints=hints, footer=basis_text(bubble))
    rows = []
    for route in bubble.routes:
        parts = ((t("part_yours"), bubble.draft), (t("part_do"), route.do),
                 (t("part_deliver"), route.deliver), (t("part_check"), route.check))
        rows.append(Row(
            approach=route.approach, get=t("bubble_get", deliverable=route.deliverable),
            fits=t("preview_fits", fits=route.fits) if route.fits else "", prompt=route.prompt,
            tag=t("tag_main" if route.main else "tag_side"),
            why=t("row_why", why=route.why) if route.main and route.why else "",
            caution=_caution(route.caution), parts=tuple(p for p in parts if p[1])))
    if bubble.source == "spare":
        title = t("bubble_title_spare")
    else:
        title = t("bubble_title_fill" if can_fill else "bubble_title_copy")
    return BubbleView(kind="routes", title=title, reason=reason, rows=tuple(rows), can_fill=can_fill,
                      footer=basis_text(bubble), more=bubble.more_left)


# ------------------------------------------------------------------ status
def status_text(state, detail="", level=LEAD):
    if state == "idle":
        return f"{t('status_idle')} · {t('level_' + level)}"
    if state == "setup":
        return t("status_setup_consent" if detail == "consent" else "status_setup_model")
    if state == "blind" and detail in ("uncalibrated", "stalled"):
        return t("status_" + detail)
    if state == "error":
        if detail == "consent":
            return t("status_setup_consent")
        key = f"status_error_{detail}"
        known = ("timeout", "invalid", "auth", "network", "redirect", "effort", "unsafe", "halted")
        return t(key if detail in known else "status_error_other")
    return t(f"status_{state}")


def notice_text(kind, detail=""):
    """Text for a short notice next to the pet, or "" when nothing should be said."""
    if kind == "copied":
        return t("notice_copied")
    if kind == "setup":
        return t("notice_setup_consent" if detail == "consent" else "notice_setup_model")
    if kind == "error":
        return t("notice_error", detail=detail or "?")
    if kind == "nothing":
        return detail or t("notice_nothing")
    if kind == "stepped_down":
        return t("notice_stepped_down", level=t("level_" + (detail or WALK)))
    key = f"notice_{kind}"
    text = t(key)
    return "" if text == key else text


def offer_text(reason):
    key = f"offer_{reason}"
    text = t(key)
    return t("offer_failed") if text == key else text


# ------------------------------------------------------------------ menu
@dataclass(frozen=True)
class Item:
    id: str                         # what the shell calls back with; "" for separators / info lines
    label: str = ""
    enabled: bool = True
    checked: Optional[bool] = None  # None = plain command; True/False = check or radio mark
    children: Tuple["Item", ...] = ()

    @property
    def separator(self):
        return not self.id and not self.label


SEPARATOR = Item("")


def menu_items(state, has_tray=False) -> List[Item]:
    """The pet menu for a controller.menu() state."""
    setup = state.get("setup") or ""
    following = not state["paused"] and not setup
    info = status_text(state["state"], setup if state["state"] == "setup" else "", state["level"])
    if state["state"] == "away" and setup:
        info = status_text("setup", setup)
    elif state["state"] == "away" and not state.get("calibrated", True):
        info = t("status_uncalibrated")
    level_items = [Item("level:" + level, t("level_" + level) + " — " + t("level_" + level + "_about"),
                        checked=state["level"] == level) for level in ORDER]
    if state.get("stepped_from"):
        level_items += [SEPARATOR, Item("undo_step", t("menu_undo_step", level=t("level_" + state["stepped_from"])))]
    level_items += [SEPARATOR,
                    Item("", t("menu_recent", adopts=state["adopts"], ignores=state["ignores"]), enabled=False)]
    items = [
        Item("", info, enabled=False),
        SEPARATOR,
        Item("levels", t("menu_levels"), children=tuple(level_items)),
        Item("resume" if state["paused"] else "pause", t("menu_resume" if state["paused"] else "menu_pause")),
        Item("context", t("menu_context"), checked=bool(state["use_context"])),
        SEPARATOR,
        Item("now", t("menu_spare" if state.get("spare") and not state["readable"] else "menu_now"),
             enabled=not state["has_bubble"] and ((following and state["readable"]) or bool(state.get("spare")))),
    ]
    if state["can_retry"]:
        items.append(Item("retry", t("menu_retry")))
    items += [
        Item("undo", t("menu_undo"), enabled=state["can_undo"]),
        SEPARATOR,
        Item("aboutme", t("menu_aboutme")),
        Item("paste", t("menu_paste")),
        Item("examples", t("menu_examples"), children=tuple(
            Item("example:" + name, t("example_" + name)) for name in examples.names())),
        SEPARATOR,
        Item("model", t("menu_model")),
        Item("calibrate", t("menu_calibrate" if state.get("calibrated") else "menu_calibrate_first")),
        Item("probe", t("menu_probe")),
        Item("evidence", t("menu_evidence")),
        Item("folder", t("menu_folder")),
        Item("clear", t("menu_clear")),
        Item("about", t("menu_about")),
        SEPARATOR,
    ]
    if has_tray:
        items.append(Item("hide", t("menu_hide")))
    items.append(Item("quit", t("menu_quit")))
    return items


def tray_items(state, pet_visible) -> List[Item]:
    return [
        Item("show" if not pet_visible else "hide", t("menu_show" if not pet_visible else "menu_hide")),
        Item("resume" if state["paused"] else "pause", t("menu_resume" if state["paused"] else "menu_pause")),
        SEPARATOR,
        Item("paste", t("menu_paste")),
        Item("model", t("menu_model")),
        Item("calibrate", t("menu_calibrate" if state.get("calibrated") else "menu_calibrate_first")),
        SEPARATOR,
        Item("quit", t("menu_quit")),
    ]


# ------------------------------------------------------------------ calibration
def calibration_lines(entry):
    """The result page of the wizard: one line per step, then what is switched on."""
    results = entry.get("results") or {}
    lines = []
    for step in results:
        result = results[step]
        mark = "✓" if result.get("ok") else ("–" if result.get("skipped") else "✗")
        lines.append(f"{mark}  {t('cal_step_' + step)}")
    caps = entry.get("capabilities") or []
    lines.append("")
    lines.append(t("cal_on", what="、".join(t("cap_" + cap) for cap in caps)) if caps else t("cal_none"))
    if "read" in caps and "fill" not in caps:
        lines.append(t("cal_no_fill"))
    if results.get("focus", {}).get("ok") is False and not results.get("focus", {}).get("skipped"):
        lines.append(t("cal_focus_warning"))
    if results.get("fill", {}).get("marker_left"):
        lines.append(t("cal_marker_left"))
    if results.get("speed", {}).get("ok") is False and not results.get("speed", {}).get("skipped"):
        lines.append(t("cal_slow_warning"))
    return lines


# ------------------------------------------------------------------ self-check report
def _yes(value):
    return "✓" if value else "✗"


def probe_text(report, context=None, version=""):
    """A plain-text compatibility report. By construction it holds no draft or chat text."""
    lines = [f"Banxing {version} · compatibility report", f"client: {report.get('client')}"]
    if report.get("reason"):
        lines.append(f"reason: {report['reason']}")
    if "process_names" in report:
        lines.append(f"looking for process: {', '.join(report['process_names'])}")
    if report.get("foreground_process"):
        lines.append(f"foreground process now: {report['foreground_process']}")
    if "window" in report:
        lines.append(f"[{_yes(report['window'])}] Codex window found")
    if "box" in report:
        lines.append(f"[{_yes(report['box'])}] input box found")
    info = report.get("box_info")
    if info:
        lines.append("    control type {control_type}, framework {framework!r}, value pattern {v}, text pattern {x}, "
                     "read-only {r}, multiline hint {m}, size {width}x{height}".format(
                         v=_yes(info.get("value_pattern")), x=_yes(info.get("text_pattern")),
                         r=_yes(info.get("read_only")), m=_yes(info.get("multiline_hint")),
                         control_type=info.get("control_type"), framework=info.get("framework"),
                         width=info.get("width"), height=info.get("height")))
    if "readable" in report:
        lines.append(f"[{_yes(report['readable'])}] draft readable ({report.get('draft_chars')} characters)")
    if "box_id_stable" in report:
        lines.append(f"[{_yes(report['box_id_stable'])}] input box identity stable"
                     + (" (using coarse id)" if report.get("coarse_box_id") else ""))
    address = report.get("address")
    if address:
        lines.append(f"[{_yes(address.get('found'))}] page address readable (scheme {address.get('scheme')!r}, "
                     f"thread id in address {_yes(address.get('has_thread_id'))})")
    if "selected_items" in report:
        lines.append(f"    selected items: {report['selected_items']}, text nodes: {report.get('text_nodes')}")
    if "ime_windows_now" in report:
        lines.append(f"    input-method windows showing now: {report['ime_windows_now'] or 'none'}")
        lines.append(f"    input-method-like windows: {report.get('ime_like_windows') or 'none'}")
    fill = report.get("fill")
    if fill:
        for method in ("paste", "value"):
            entry = fill.get(method) or {}
            lines.append(f"[{_yes(entry.get('write'))}] fill by {method}"
                         + (f" ({entry.get('reason')})" if entry.get("reason") else "")
                         + (f", restore {_yes(entry['restore'])}" if "restore" in entry else ""))
        lines.append(f"[{_yes(fill.get('draft_intact'))}] draft intact after the fill test")
    if context:
        lines.append(f"[{_yes(context.get('appserver'))}] codex command found for the app server")
        lines.append(f"[{_yes(context.get('sessions'))}] session folder found")
        if "identified" in context:
            lines.append(f"[{_yes(context['identified'])}] current chat recognised "
                         f"(candidates {context.get('candidates')}, source {context.get('source') or '-'}, "
                         f"finished turns {context.get('turns')})")
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    return "\n".join(lines)
