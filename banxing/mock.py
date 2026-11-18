"""Offline stand-in for the model service. Plumbing, not evidence.

It lets the tests, the self-test of the packaged build and demos run without a
network. Its answers are keyword rules (watch/standin.py) — just enough to play
every outcome. Nothing about quality may be concluded from it, and the app never
falls back to it silently: it is used only when the provider is set to "mock".
"""
from __future__ import annotations


def complete(meta):
    from .watch import standin
    if meta.get("task") == "blind":
        return standin.blind(meta)
    return standin.audit(meta)
