"""Shared helpers for the snapshot and restore tests: a fake tmux per session name, a saved record,
and a clock that does not move unless a test moves it."""
from fakes import FakeTmux, pane
from kalmux import tmsnapshot


def fake_tmux(*names, colors=None, path="/Volumes/Dev/proj"):
    """A FakeTmux holding one pane per session name, in that order."""
    panes = [pane(name, pane_id=f"%{i}", window_id=f"@{i}", path=f"{path}/{name}") for i, name in enumerate(names)]
    return FakeTmux(panes=panes, colors=dict(colors or {}))


def saved(name, cwd="/tmp", color="", created=1000):
    return tmsnapshot.SavedSession(name=name, cwd=cwd, color=color, created=created)


class Clock:
    """A clock a test owns: a snapshot's age and its write-or-not decision must never depend on timing."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now
