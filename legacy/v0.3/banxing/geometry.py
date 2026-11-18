"""Screen geometry: where the pet sits and where the bubble may open. Pure functions.

All coordinates are physical screen pixels, the same space UI Automation reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    @property
    def empty(self):
        return self.width <= 0 or self.height <= 0

    def moved(self, dx, dy):
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def grown(self, margin):
        return Rect(self.left - margin, self.top - margin, self.right + margin, self.bottom + margin)

    def overlap(self, other):
        """Overlapping area in square pixels."""
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return w * h if w > 0 and h > 0 else 0

    def inside(self, outer):
        return (self.left >= outer.left and self.top >= outer.top
                and self.right <= outer.right and self.bottom <= outer.bottom)

    @staticmethod
    def at(x, y, width, height):
        return Rect(int(x), int(y), int(x) + int(width), int(y) + int(height))


def clamp_into(rect, outer):
    """Move `rect` the shortest distance so it lies inside `outer` (if it fits)."""
    dx = dy = 0
    if rect.right > outer.right:
        dx = outer.right - rect.right
    if rect.left + dx < outer.left:
        dx = outer.left - rect.left
    if rect.bottom > outer.bottom:
        dy = outer.bottom - rect.bottom
    if rect.top + dy < outer.top:
        dy = outer.top - rect.top
    return rect.moved(dx, dy)


def pet_rect(window, screen, size, offset=None, box=None, margin=12):
    """The pet's place for a Codex window.

    `offset` is the user's own choice (drag), stored relative to the window's
    bottom-right corner so it follows the window. Without one the pet sits on the
    right edge, just above the input box, where it does not cover the send button.
    """
    w, h = size
    if offset is not None:
        rect = Rect.at(window.right + offset[0], window.bottom + offset[1], w, h)
        return clamp_into(rect, screen)
    anchor_bottom = (box.top - margin) if box is not None and not box.empty else (window.bottom - 4 * margin - h)
    outside = Rect.at(window.right + margin, anchor_bottom - h, w, h)
    if outside.inside(screen):
        return outside
    inside = Rect.at(window.right - margin - w, anchor_bottom - h, w, h)
    return clamp_into(inside, screen)


def offset_from(rect, window):
    """Inverse of `pet_rect` for a dragged pet."""
    return (rect.left - window.right, rect.top - window.bottom)


def place_bubble(pet, size, screen, avoid=(), gap=8):
    """Choose where the bubble opens next to the pet.

    Preference order: above the pet, to its left, to its right, below it — each
    tried with both alignments. A spot is taken only if it is on screen and does
    not cover any `avoid` rectangle (the input box, the send button). If no spot
    is clean, the one covering the least is used.
    Returns (Rect, side) where side names where the bubble sits relative to the pet.
    """
    w, h = size
    spots = [
        ("above", pet.right - w, pet.top - gap - h),
        ("above", pet.left, pet.top - gap - h),
        ("left", pet.left - gap - w, pet.bottom - h),
        ("left", pet.left - gap - w, pet.top),
        ("right", pet.right + gap, pet.bottom - h),
        ("right", pet.right + gap, pet.top),
        ("below", pet.right - w, pet.bottom + gap),
        ("below", pet.left, pet.bottom + gap),
    ]
    avoid = [a for a in avoid if a is not None and not a.empty]
    best = None
    for order, (side, x, y) in enumerate(spots):
        rect = Rect.at(x, y, w, h)
        fitted = clamp_into(rect, screen)
        moved = abs(fitted.left - rect.left) + abs(fitted.top - rect.top)
        covered = sum(fitted.overlap(a) for a in avoid) + fitted.overlap(pet)
        if moved == 0 and covered == 0:
            return fitted, side
        score = (covered, moved, order)
        if best is None or score < best[0]:
            best = (score, fitted, side)
    return best[1], best[2]


def union(rects: Iterable[Optional[Rect]]) -> Optional[Rect]:
    rects = [r for r in rects if r is not None and not r.empty]
    if not rects:
        return None
    return Rect(min(r.left for r in rects), min(r.top for r in rects),
                max(r.right for r in rects), max(r.bottom for r in rects))


Size = Tuple[int, int]
