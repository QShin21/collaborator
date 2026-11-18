"""What the shell shows, decided without any toolkit so it can be tested."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .strings import t

# ------------------------------------------------------------------ text layout
_TOKEN = re.compile(r"[\x21-\x7e]+|\s+|.", re.S)
_NOT_AT_LINE_START = set("，。、；：？！）》」』”’…,.;:?!)]}%")


def wrap_text(text, measure, width):
    """Break text into lines no wider than `width` (as judged by `measure`).

    Toolkits wrap at spaces only, which tears mixed Chinese/English text apart.
    Here Chinese may break between any two characters, a Latin word stays whole,
    and closing punctuation never starts a line. Existing line breaks are kept."""
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


# ------------------------------------------------------------------ the bubble
@dataclass(frozen=True)
class NudgeView:
    kind: str                           # the label of the kind of concern
    title: str
    lines: Tuple[str, ...]              # what is off, what it bears on, the cheapest check
    ask: str                            # the question to take back to the main model
    evidence: Tuple[str, ...]           # the passages, each with its turn and who said it
    footer: str
    caution: str = ""
    tone: str = "direction"             # "direction" (it bears on where this is going) or "note"
    weight: str = ""                    # the same, in words
    buttons: Tuple[str, ...] = ("sources", "copy", "intended", "wrong")


def _who(where):
    return t({"user": "ref_user", "model": "ref_model", "record": "ref_record"}.get(where, "ref_record"))


def ref_lines(refs):
    return tuple(t("ref_line", turn=ref.turn, who=_who(ref.where), quote=ref.quote) for ref in refs)


def _caution(codes):
    known = [t("caution_" + code) for code in codes if code in ("privilege", "destructive")]
    return "；".join(known) if known else (t("caution_other") if codes else "")


def nudge_view(bubble):
    """What a bubble shows. The finding was checked before it got here; this only
    lays it out."""
    finding = bubble.finding
    lines = [finding.claim, t("nudge_why", text=finding.why)]
    if finding.probe:
        lines.append(t("nudge_probe", text=finding.probe))
    evidence = ref_lines(finding.basis)
    fresh = tuple(ref for ref in finding.now if ref not in finding.basis)
    if fresh:
        evidence += (t("nudge_now") + "：",) + ref_lines(fresh)
    footer = t("nudge_footer", through=bubble.through,
               mode=t("nudge_mode_manual" if bubble.manual else "nudge_mode_auto"))
    if bubble.more > 0:
        footer += " · " + t("nudge_more", count=bubble.more)
    return NudgeView(kind=t("kind_" + finding.kind), title=finding.title, lines=tuple(lines), ask=finding.ask,
                     evidence=evidence, footer=footer, caution=_caution(finding.caution),
                     tone=finding.weight, weight=t("weight_" + finding.weight))


# ------------------------------------------------------------------ the ledger
def _turn_list(numbers):
    """1,2,3,7,8 → "1–3、7–8": which turns, compactly."""
    numbers, parts, start = sorted(set(numbers)), [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            parts.append(str(start) if start == number else f"{start}–{number}")
            start = None
    return "、".join(parts)


def _sources(refs):
    """ "（第 4、12 轮）" — where an entry of the ledger can be looked up."""
    return "（" + t("ledger_turns", turns=_turn_list(ref.turn for ref in refs)) + "）" if refs else ""


def usage_line(ledger, usage, origin):
    if usage is None or not usage.requests:
        return t("ledger_usage_none")
    line = t("ledger_usage", through=ledger.through, turns=t("ledger_turns", turns=_turn_list(usage.turns)),
             chars=usage.chars, requests=usage.requests, host=origin or t("ledger_usage_local"))
    return line + (" · " + t("ledger_usage_masked", count=usage.masked) if usage.masked else "")


def trace_line(trace):
    """What the supervisor went and looked at during the latest audit."""
    if trace is None or not (trace.looks or trace.independent):
        return t("ledger_trace_none")
    parts = []
    if trace.looks:
        parts.append(t("ledger_trace", looks=trace.looks, searches=trace.searches, found=trace.found,
                       turns=t("ledger_turns", turns=_turn_list(trace.turns) or "—")))
    if trace.independent:
        parts.append(t("ledger_trace_independent"))
    return "；".join(parts)


_ORDER = {"broken": 0, "unchecked": 1, "backed": 2, "retired": 3}


HEAD, PLAIN, MUTED_ROW, BACKED_ROW, UNCHECKED_ROW, BROKEN_ROW, FINDING_ROW, DETAIL = (
    "head", "plain", "muted", "backed", "unchecked", "broken", "finding", "detail")
_ROW_OF = {"backed": BACKED_ROW, "unchecked": UNCHECKED_ROW, "broken": BROKEN_ROW, "retired": MUTED_ROW}


def ledger_rows(ledger, findings=(), usage=None, origin="", last=None):
    """The ledger as (text, kind of row) pairs — what the ledger window colours by.
    What is shakiest comes first."""
    goal = ledger.goal
    if not ledger.through and not goal.text:
        return [(t("ledger_empty"), PLAIN), ("", PLAIN), (usage_line(ledger, usage, origin), MUTED_ROW)]
    state = "goal_user" if goal.by == "user" else "goal_" + goal.state
    rows = [(f"{t('ledger_goal')}（{t(state)}）", HEAD),
            ("  " + (goal.text or t("goal_none")) + _sources(goal.refs), PLAIN)]
    rows += [("  " + t("ledger_open", text=item), DETAIL) for item in goal.open]
    rows += [("", PLAIN), (t("ledger_anchors"), HEAD)]
    marks = {"backed": "✓", "unchecked": "？", "broken": "✗", "retired": "–"}
    for anchor in sorted(ledger.anchors, key=lambda a: _ORDER.get(a.status, 9)):
        rows.append((f"  {marks[anchor.status]} {t('status_' + anchor.status)} · {t('backing_' + anchor.backing)} · "
                     f"{t('anchor_' + anchor.kind)}  {anchor.text}" + _sources(anchor.refs), _ROW_OF[anchor.status]))
        if anchor.carries:
            rows.append(("      " + t("ledger_carries", text=anchor.carries), DETAIL))
    rows += [] if ledger.anchors else [("  " + t("ledger_none"), MUTED_ROW)]
    rows += [("", PLAIN), (t("ledger_findings"), HEAD)]
    for finding in findings:
        rows.append((f"  ! [{t('kind_' + finding.kind)} · {t('weight_' + finding.weight)}] {finding.title}"
                     + _sources(finding.basis + finding.now), FINDING_ROW))
        rows.append(("      " + finding.claim, PLAIN))
        rows.append(("      " + t("nudge_why", text=finding.why), PLAIN))
        rows.append(("      " + t("nudge_ask") + finding.ask, DETAIL))
    rows += [] if findings else [("  " + t("ledger_none"), MUTED_ROW)]
    if ledger.marks:
        rows += [("", PLAIN), (t("ledger_marks"), HEAD)]
        for mark in ledger.marks:
            kind = t("kind_" + mark.kind)
            rows.append(("  " + t("mark_line", turn=mark.turn, kind=kind, title=mark.title,
                                  state=t("mark_" + mark.state)) + _sources(mark.refs),
                         BACKED_ROW if mark.state == "resolved" else PLAIN))
            if mark.note:
                rows.append(("      " + mark.note, DETAIL))
    if ledger.gaps:
        rows += [("", PLAIN), (t("ledger_gaps"), HEAD)]
        rows += [("  - " + t("gap_" + gap.kind, turn=gap.turn, count=gap.count), DETAIL) for gap in ledger.gaps]
    if last is not None and last.ok:
        rows += [("", PLAIN), (t("ledger_last"), HEAD), ("  " + trace_line(last.trace), DETAIL)]
        if not findings:
            rows.append(("  " + t("ledger_sound") + (f"：{last.sound}" if last.sound else ""), BACKED_ROW))
    return rows + [("", PLAIN), (usage_line(ledger, usage, origin), MUTED_ROW)]


def ledger_lines(ledger, findings=(), usage=None, origin="", last=None):
    """The ledger as lines of text, for the command line."""
    return [text for text, _ in ledger_rows(ledger, findings, usage, origin, last)]


def ledger_summary(ledger, findings=()):
    """The line above the ledger: how the footing stands, at a glance.
    Returns ((label, count, kind of row), ...) — only what is there."""
    statuses = [anchor.status for anchor in ledger.anchors]
    parts = [(t("status_" + status), statuses.count(status), _ROW_OF[status])
             for status in ("broken", "unchecked", "backed") if statuses.count(status)]
    bearing = sum(1 for finding in findings if finding.weight == "direction")
    if bearing:
        parts.insert(0, (t("weight_direction"), bearing, FINDING_ROW))
    return tuple(parts)


# ------------------------------------------------------------------ status, notices, menu
def status_text(state):
    """One line about what the app is doing, from Watcher.state()."""
    if not state.get("model"):
        return t("status_setup")
    if not state.get("active"):
        return t("status_none")
    if state.get("problem"):
        return t("status_unreadable")
    if state.get("state") == "diverged":
        return t("status_diverged")
    if state.get("state") == "consent":
        return t("status_consent")
    if state.get("paused"):
        return t("status_paused")
    if state.get("auditing"):
        return t("status_auditing")
    line = t("status_on", through=state.get("through", 0))
    return line + (" · " + t("status_findings", count=state["findings"]) if state.get("findings") else "")


def hover_text(state):
    """What the figure says when the pointer rests on it: the status and what a click does."""
    if not state.get("model"):
        hint = "hover_model"
    elif not state.get("active"):
        hint = "hover_watch"
    else:
        hint = "hover_look"
    return status_text(state) + "\n" + t(hint)


def source_error_text(kind):
    key = f"source_error_{kind}"
    text = t(key)
    return t("source_error_unreadable") if text == key else text


def notice_text(kind, detail=""):
    """Text for a short notice next to the figure, or "" when nothing should be said."""
    if kind == "watch_clear":
        return t("notice_watch_clear", detail=detail or "").rstrip()
    if kind == "watch_error":
        return t("notice_watch_error", detail=detail or "?")
    if kind == "watch_unreadable":
        return t("notice_watch_unreadable", detail=source_error_text(detail))
    key = f"notice_{kind}"
    text = t(key)
    return "" if text == key else text


@dataclass(frozen=True)
class Item:
    id: str                         # what the shell calls back with; "" for separators / info lines
    label: str = ""
    enabled: bool = True
    checked: Optional[bool] = None

    @property
    def separator(self):
        return not self.id and not self.label


SEPARATOR = Item("")


def menu_items(state, has_tray=False) -> List[Item]:
    """The menu for a Watcher.state()."""
    items = [Item("", status_text(state), enabled=False), SEPARATOR]
    if state.get("active"):
        items += [Item("look", t("menu_look"), enabled=not state.get("auditing")),
                  Item("ledger", t("menu_ledger"))]
        if state.get("source") == "pasted_text":
            items.append(Item("paste", t("menu_paste")))
        items += [Item("resume" if state.get("paused") else "pause",
                       t("menu_resume" if state.get("paused") else "menu_pause")),
                  Item("stop", t("menu_stop"))]
    items += [Item("watch", t("menu_watch")), SEPARATOR, Item("model", t("menu_model")),
              Item("folder", t("menu_folder")), Item("clear", t("menu_clear")), Item("guide", t("menu_guide")),
              Item("about", t("menu_about")),
              SEPARATOR]
    if has_tray:
        items.append(Item("hide", t("menu_hide")))
    items.append(Item("quit", t("menu_quit")))
    return items


def tray_items(state, visible) -> List[Item]:
    items = [Item("show" if not visible else "hide", t("menu_show" if not visible else "menu_hide"))]
    if state.get("active"):
        items += [Item("look", t("menu_look")), Item("ledger", t("menu_ledger"))]
    return items + [Item("watch", t("menu_watch")), Item("model", t("menu_model")), Item("guide", t("menu_guide")),
                    SEPARATOR, Item("quit", t("menu_quit"))]
