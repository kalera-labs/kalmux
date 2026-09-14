"""tmactions — operations shared by the `kalmux` CLI and the `kalmux ui` server.

Every action returns a Result (ok, message, data) and never raises for expected failures, so the
HTTP layer and the CLI only differ in how they print it.
"""
from __future__ import annotations

import base64
import concurrent.futures
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from tmconfig import SESSION_ID_RE, render_resume
from tmcore import (CLEAR_WORDS, CTRL_RE, HEX_RE, STATE_ORDER, It2, Tmux, alive_session_ids, attach_command,
                    cc_tab_command, load_registry, load_status, load_trails, merge, osc6_tab_color, parse_color,
                    tombstones, valid_session_name, write_tty)

# same colors as bin/cc-status-tmux (and iTerm2's own cc-status)
STATUS_COLORS = {"working": ("#ff9500", "#ff9500"), "waiting": ("#5f87ff", "#5f87ff"), "idle": ("#00d75f", "#888888")}
STATE_WORD_RE = re.compile(r"[^A-Za-z0-9_-]")
MAX_RESUME_SUFFIX = 1000
SEARCH_KEYS = ("name", "session", "project", "title")
# what bin/cc-status-tmux writes on SessionEnd: an empty status plus an empty SetUserVar payload
CLEAR_STATUS = ("\x1b]21337;status=;indicator=;status-color=;detail=\x1b\\"
                "\x1b]1337;SetUserVar=cc_state=\x07")
NAME_RULE = "name must start with a letter, digit or _ and use only letters, digits, _ and - (max 64)"
MAX_TEXT = 256


@dataclass
class Result:
    ok: bool
    message: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "data": self.data}


def _text(value, limit: int = MAX_TEXT) -> str:
    """Coerce an untrusted JSON/CLI value to a bounded, control-char-free string."""
    if not isinstance(value, str):
        return ""
    return CTRL_RE.sub("", value)[:limit].strip()


# ----------------------------------------------------------------------------- iTerm2 tab map
class TabMap:
    """tmux pane id -> iTerm2 tab {guid, window, tab}.

    iTerm2 exposes the tmux pane number of every control-mode tab as the session variable
    `tmuxWindowPane`; one `it2` call per new GUID, cached until the GUID disappears."""

    def __init__(self, it2: It2, ttl: float = 2.0, workers: int = 6, backoff: float = 30.0) -> None:
        self.it2 = it2
        self.ttl = ttl
        self.workers = workers
        self.backoff = backoff
        self._guid_pane: dict[str, str] = {}
        # stamp None = never built. NOT 0.0: time.monotonic() counts from boot on macOS, so right after a
        # reboot "now - 0.0 < ttl" is true and the first refresh would hand back the empty cache.
        self._cache: tuple[dict, float | None] = ({}, None)
        self._down_until = 0.0
        self._lock = threading.Lock()

    def is_down(self) -> bool:
        """True while the last `it2 session list` failed: the map is empty because iTerm2 did not answer,
        not because the sessions have no tabs."""
        return time.monotonic() < self._down_until

    def refresh(self, force: bool = False) -> dict[str, dict]:
        with self._lock:
            mapping, stamp = self._cache
            if not force and stamp is not None and time.monotonic() - stamp < self.ttl:
                return mapping
            if not force and self.is_down():
                return {}                                   # it2 is failing/hanging: keep /api/state fast
            mapping = self._build() if self.it2.available() else {}
            self._cache = (mapping, time.monotonic())
            return mapping

    def _build(self) -> dict[str, dict]:
        rows = self.it2.list_sessions()
        if not rows and getattr(self.it2, "last_rc", 0) != 0:
            self._down_until = time.monotonic() + self.backoff
            return {}
        self._down_until = 0.0
        live = {r["guid"] for r in rows}
        for guid in [g for g in self._guid_pane if g not in live]:
            del self._guid_pane[guid]
        candidates = [r for r in rows if r.get("is_tmux") is not False]
        unknown = [r["guid"] for r in candidates if r["guid"] not in self._guid_pane]
        if unknown:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as ex:
                for guid, value in zip(unknown, ex.map(lambda g: self.it2.get_var(g, "tmuxWindowPane"), unknown), strict=True):
                    if value.isdigit():
                        self._guid_pane[guid] = "%" + value      # failures are not cached: retried next refresh
        out: dict[str, dict] = {}
        for r in candidates:
            pane = self._guid_pane.get(r["guid"])
            if pane and pane not in out:
                out[pane] = {"guid": r["guid"], "window": r.get("window", ""), "tab": r.get("tab", "")}
        return out


# ----------------------------------------------------------------------------- snapshot
PANE_KEYS = ("pane", "window_id", "pane_id", "claude", "state", "source", "detail", "title", "age", "stale", "path",
             "active", "claude_session_id", "ctx_pct", "model", "cost_usd", "quota")
CONTEXT_KEYS = ("claude_session_id", "ctx_pct", "model", "cost_usd", "quota")
BLANK_CONTEXT = {"claude_session_id": "", "ctx_pct": None, "model": "", "cost_usd": None, "quota": None}


def _pane_rank(p: dict) -> tuple:
    return (STATE_ORDER.get(p["state"], 9), not p["active"])


def _session_entry(s: dict) -> dict:
    """One session row: the state of its most urgent pane, the context of its most urgent Claude pane."""
    best = min(s["panes"], key=_pane_rank)
    claude = [p for p in s["panes"] if p["claude_session_id"]]
    lead = min(claude, key=_pane_rank) if claude else BLANK_CONTEXT
    path = best["path"]
    return {**s, "state": best["state"], "age": best["age"], "stale": best["stale"], "source": best["source"],
            "detail": best["detail"], "title": best["title"], "path": path,
            "project": os.path.basename(path.rstrip("/")) or path,
            "in_iterm2": any(p["guid"] for p in s["panes"]),
            **{k: lead[k] for k in CONTEXT_KEYS}}


def _five_hour_pct(quota: dict) -> float:
    five = quota.get("five_hour") if isinstance(quota.get("five_hour"), dict) else {}
    pct = five.get("used_pct")
    return float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else -1.0


def top_quota(sessions: list[dict]) -> dict | None:
    """The tightest rate-limit window across live sessions (they all share one account)."""
    quotas = [s["quota"] for s in sessions if isinstance(s.get("quota"), dict)]
    return max(quotas, key=_five_hour_pct) if quotas else None


def _tombstones(registry: dict, trace_dir, now: int, keep_days: int, superseded: bool) -> list[dict]:
    if not trace_dir:
        return []
    rows = tombstones(load_trails(Path(trace_dir), now, keep_days), registry)
    return rows if superseded else [r for r in rows if r["ended"] != "superseded"]


def snapshot(tmux: Tmux, registry_dir: Path, tabmap: TabMap | None, now: int | None = None, status_dir=None,
             trace_dir=None, keep_days: int = 30, superseded: bool = False) -> dict:
    """Everything the UI needs in one JSON-able dict: sessions (sorted by urgency) with their panes."""
    now = int(time.time()) if now is None else now
    panes = tmux.list_panes()
    registry = load_registry(registry_dir)
    rows = merge(panes, registry, now, load_status(Path(status_dir)) if status_dir else None)
    tabs = tabmap.refresh() if tabmap else {}
    sessions: dict[str, dict] = {}
    for r in rows:
        tab = tabs.get(r["pane_id"], {})
        pane = {k: r[k] for k in PANE_KEYS}
        pane.update(guid=tab.get("guid", ""), iterm2_window=tab.get("window", ""), iterm2_tab=tab.get("tab", ""))
        s = sessions.setdefault(r["session"], {"name": r["session"], "color": r["color"],
                                               "attached": int(r["attached"]) if str(r["attached"]).isdigit() else 0, "panes": []})
        s["panes"].append(pane)
    out = [_session_entry(s) for s in sessions.values()]
    out.sort(key=lambda s: (STATE_ORDER.get(s["state"], 9), s["name"]))
    counts = {k: 0 for k in ("waiting", "working", "idle", "busy", "unknown", "stale", "gone")}
    for s in out:
        if s["state"] in counts:
            counts[s["state"]] += 1
    counts["total"] = len(out)
    return {"now": now, "counts": counts, "sessions": out, "quota": top_quota(out),
            "tombstones": _tombstones(registry, trace_dir, now, keep_days, superseded)}


def next_waiting(sessions: list[dict]) -> str | None:
    return next((s["name"] for s in sessions if s.get("state") == "waiting"), None)


def resolve_session(items: list[dict], query: str) -> Result:
    """Turn what the user typed into one session name: exact name, else a unique substring of name/project/title.

    Works on merged pane rows (key `session`) and on snapshot sessions (key `name`), so `kalmux go phase` finds
    the session whose Claude is working on "✳ Phase 2"."""
    query = _text(query, 128)
    if not query:
        return Result(False, "which session? give a name or part of a title")
    names: list[str] = []
    haystacks: dict[str, list[str]] = {}
    for item in items:
        name = item.get("name") or item.get("session") or ""
        if not name:
            continue
        if name not in haystacks:                           # every pane of a session feeds the same haystack:
            names.append(name)                              # the Claude title may sit on the second pane
            haystacks[name] = []
        haystacks[name].append(" ".join(str(item.get(k, "")) for k in SEARCH_KEYS).lower())
    if query in names:
        return Result(True, query, {"session": query})      # an exact name always wins over a substring
    needle = query.lower()
    hits = [n for n in names if any(needle in h for h in haystacks[n])]
    if len(hits) == 1:
        return Result(True, hits[0], {"session": hits[0]})
    if hits:
        return Result(False, "ambiguous: " + ", ".join(sorted(hits)))
    return Result(False, f"no session matches {query!r}")


# ----------------------------------------------------------------------------- status re-emit
def status_sequence(state: str, detail: str, tm_color: str) -> str:
    """OSC 21337 status + SetUserVar + OSC 6 tab color: what the hook would have sent, replayed.

    An empty state replays the hook's SessionEnd branch (an empty status), so a pane whose Claude is gone
    loses its stale indicator on re-attach instead of keeping it forever."""
    seq = "\x1b\\"          # a leading ST closes any string an earlier truncated write left open
    state = STATE_WORD_RE.sub("", _text(state, 32))     # one word: it is spliced into an OSC parameter list
    if state:
        dot, text = STATUS_COLORS.get(state, STATUS_COLORS["idle"])
        detail = _text(detail, 120).replace(";", ",")
        b64 = base64.b64encode(state.encode("utf-8")).decode("ascii")
        seq += (f"\x1b]21337;status={state};indicator={dot};status-color={text};detail={detail}\x1b\\"
                f"\x1b]1337;SetUserVar=cc_state={b64}\x07")
    else:
        seq += CLEAR_STATUS     # no known state: clear the indicator instead of leaving a dead session's dot
    if HEX_RE.match(tm_color or ""):
        seq += osc6_tab_color(tm_color)
    return seq


def reapply(tmux: Tmux, session: str | None = None) -> Result:
    """Re-send color + last known Claude status to every attached pane (tabs start blank after attach)."""
    n = 0
    for p in tmux.list_panes():
        if session and p["session"] != session:
            continue
        if p.get("attached", "0") in ("", "0") or not p.get("tty"):
            continue
        if write_tty(p["tty"], status_sequence(p.get("cc_state", ""), p.get("cc_detail", ""), p.get("tm_color", ""))):
            n += 1
    return Result(True, f"re-applied status/color on {n} pane(s)", {"panes": n})


def reapply_later(tmux: Tmux, session: str, delay: float = 2.0) -> threading.Thread:
    """After `open`, the new tab needs a moment before the pane pty is wired to iTerm2."""
    def run() -> None:
        time.sleep(delay)
        reapply(tmux, session)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ----------------------------------------------------------------------------- actions
def _session_panes(tmux: Tmux, session: str) -> list[dict]:
    return [p for p in tmux.list_panes() if p["session"] == session]


def action_go(tmux: Tmux, it2: It2, tabmap: TabMap, session: str, env=None) -> Result:
    """Focus the iTerm2 tab of a session; open one when the session has no tab yet."""
    session = _text(session, 128)
    panes = _session_panes(tmux, session)
    if not panes:
        return Result(False, f"no tmux session named {session!r}")
    if not it2.available():
        return Result(False, "not inside iTerm2 here; attach with: " + " ".join(attach_command(session, os.environ if env is None else env)))
    tabs = tabmap.refresh(force=True)
    if not tabs and tabmap.is_down():
        # an empty map because it2 failed is not "no tab": opening would stack a second -CC client on the session
        return Result(False, f"iTerm2 is not answering (it2 failed); not opening another tab for {session}")
    with_tab = [p for p in panes if p["pane_id"] in tabs]
    if not with_tab:
        return action_open(tmux, it2, session)
    best = next((p for p in with_tab if p.get("window_active") == "1" and p.get("pane_active") == "1"), with_tab[0])
    guid = tabs[best["pane_id"]]["guid"]
    if not it2.focus(guid):
        return Result(False, f"it2 could not focus tab {guid}")
    return Result(True, f"focused {session}", {"opened": False, "guid": guid})


def action_open(tmux: Tmux, it2: It2, session: str, window: str = "") -> Result:
    """Open a session as a new control-mode tab in the current (or given) iTerm2 window."""
    session = _text(session, 128)
    if not tmux.has_session(session):
        return Result(False, f"no tmux session named {session!r}")
    if not it2.available():
        return Result(False, "iTerm2 (it2) not available here")
    try:
        command = cc_tab_command(session)
    except ValueError as exc:
        return Result(False, str(exc))
    # iTerm2 reports no current window while it is in the background: fall back to the front-most window
    window = _text(window, 128) or it2.current_window() or next(iter(it2.list_windows()), "")
    if window:
        ok, out = it2.new_tab(command, window)
        where = "tab"
    else:
        ok, out = it2.new_window(command)
        where = "window"
    if not ok:
        return Result(False, f"it2 {where} new failed: {out or 'unknown error'}")
    return Result(True, f"opened {session} in a new {where}", {"opened": True, "window": window, "it2": out})


def action_color(tmux: Tmux, session: str, color: str) -> Result:
    session = _text(session, 128)
    panes = _session_panes(tmux, session)
    if not panes:
        return Result(False, f"no tmux session named {session!r}")
    color = _text(color, 32).lower()
    if color in CLEAR_WORDS:
        hex_color = None
    else:
        try:
            hex_color = parse_color(color)
        except ValueError as exc:
            return Result(False, str(exc))
    if not tmux.set_session_option(session, "@tm_color", hex_color):
        return Result(False, f"tmux refused to set @tm_color on {session!r}")
    seq = osc6_tab_color(hex_color)
    written = sum(1 for p in panes if p.get("tty") and write_tty(p["tty"], seq))
    return Result(True, f"{session}: color {hex_color or 'cleared'} on {len(panes)} pane(s)",
                  {"color": hex_color or "", "panes": len(panes), "written": written})


def action_new(tmux: Tmux, name: str, cwd: str = "", color: str = "", start_claude: bool = False,
               claude_cmd: str = "claude") -> Result:
    name = _text(name, 64)
    if not valid_session_name(name):
        return Result(False, "session " + NAME_RULE)
    if tmux.has_session(name):
        return Result(False, f"session {name!r} already exists")
    cwd = _text(cwd, 1024)
    directory = os.path.expanduser(cwd) if cwd else ""
    if directory and not os.path.isdir(directory):
        return Result(False, f"not a directory: {cwd}")
    hex_color = ""
    color = _text(color, 32).lower()
    if color and color not in CLEAR_WORDS:
        try:
            hex_color = parse_color(color)
        except ValueError as exc:
            return Result(False, str(exc))
    if not tmux.new_session(name, directory or None):
        return Result(False, f"tmux could not create session {name!r}")
    if hex_color:
        tmux.set_session_option(name, "@tm_color", hex_color)
    if start_claude:
        # "=name:" = exactly this session, its current window ("=name" alone is not a pane target)
        tmux.send_keys(f"={name}:", _text(claude_cmd, 512) or "claude")
    return Result(True, f"created tmux session {name!r}", {"name": name, "cwd": directory, "color": hex_color})


# ----------------------------------------------------------------------------- resume a gone session
def trails_named(trails: list[dict], query: str) -> list[dict]:
    """Every trail that lived in the tmux session `query`, newest first — how many share one name."""
    query = _text(query, 128)
    if not query:
        return []
    return sorted((t for t in trails if t.get("tmux_session") == query), key=lambda t: -(t.get("last_ts") or 0))


def find_trail(trails: list[dict], query: str) -> dict | None:
    """Locate a tombstone by full session id, by the tmux session it used to live in, or by an id prefix.

    A tmux name can be reused by many conversations: the NEWEST one wins, never whichever file happened to
    sort first. Callers report the ambiguity with `trails_named`."""
    query = _text(query, 128)
    if not query:
        return None
    exact = next((t for t in trails if t.get("session_id") == query), None)
    named = trails_named(trails, query)
    prefixed = [t for t in trails if len(query) >= 8 and str(t.get("session_id", "")).startswith(query)]
    return exact or (named[0] if named else None) or (prefixed[0] if len(prefixed) == 1 else None)


def _resume_name(tmux: Tmux, base: str) -> str:
    """The old session name if it is free, else the first free `<name>-2`, `-3`, … ("" when all are taken).

    A resume never steals the name of a session that is running."""
    base = _text(base, 60)
    if not valid_session_name(base):
        base = "claude"
    if not tmux.has_session(base):
        return base
    return next((f"{base}-{i}" for i in range(2, MAX_RESUME_SUFFIX) if not tmux.has_session(f"{base}-{i}")), "")


def action_resume(tmux: Tmux, cfg: dict, trail: dict, registry: dict, matches: int = 1) -> Result:
    """Recreate a dead Claude session: fresh tmux session, old cwd and color, resume command typed into its pane.

    `registry` is Claude Code's live registry. Resuming a conversation that is still running would put two
    Claude processes on one transcript, so it is refused here as well as filtered out by `resumable_trails`.
    `matches` is how many trails shared the name the user typed (see `trails_named`)."""
    session_id = _text(trail.get("session_id"), 64)
    if session_id in alive_session_ids(registry or {}):
        return Result(False, f"{session_id} is still running; resume it from its own session instead",
                      {"session_id": session_id, "alive": True})
    try:
        typed = render_resume(cfg, session_id)
    except ValueError as exc:
        return Result(False, str(exc))
    base = trail.get("tmux_session") or ""
    name = _resume_name(tmux, base)
    if not name:
        return Result(False, f"no free tmux name left for {base or 'claude'!r} (tried up to -{MAX_RESUME_SUFFIX - 1})")
    directory, note = _text(trail.get("cwd"), 1024), ""
    directory = os.path.expanduser(directory) if directory else ""
    if not os.path.isdir(directory):
        directory = os.path.expanduser("~")
        note = f"; its old directory is gone, so it starts in {directory}"
    pane_id = tmux.new_session_pane(name, directory or None)
    if not pane_id:
        return Result(False, f"tmux could not create session {name!r}", {"name": name})
    color = _text(trail.get("color"), 32).lower()
    if HEX_RE.match(color):
        tmux.set_session_option(name, "@tm_color", color)
    if not tmux.send_text(pane_id, typed):
        return Result(False, f"tmux could not type the resume command into {name!r}", {"name": name})
    ran = cfg["claude"]["resume_mode"] == "run"
    if ran:
        tmux.press_enter(pane_id)
    verb = "running" if ran else "typed (press Enter to run)"
    pick = f" (newest of {matches}; pass the session id to pick another)" if matches > 1 else ""
    return Result(True, f"{name}: {verb} {typed}{note}{pick}",
                  {"name": name, "pane_id": pane_id, "typed": typed, "ran": ran, "session_id": session_id,
                   "matches": matches})


def _drop_status(status_dir, session_id: str) -> bool:
    """Best effort: the status tap keeps one file per session, and forgetting must not leave it behind."""
    if not status_dir:
        return False
    try:
        (Path(status_dir) / f"{session_id}.json").unlink()
    except OSError:
        return False
    return True


def action_forget(trace_dir, session_id: str, status_dir=None) -> Result:
    """Delete one trail file (and the session's status leftover) so it stops showing up under Gone."""
    session_id = _text(session_id, 64)
    if not SESSION_ID_RE.match(session_id):
        return Result(False, f"not a Claude session id: {session_id!r}")
    status_gone = _drop_status(status_dir, session_id)      # the name is a validated uuid: no traversal
    path = Path(trace_dir) / f"{session_id}.jsonl"
    try:
        path.unlink()
    except FileNotFoundError:
        if status_gone:
            return Result(True, f"forgot {session_id} (no trail left, dropped its status file)",
                          {"session_id": session_id, "status": True})
        return Result(False, f"no trail for {session_id}")
    except OSError as exc:
        return Result(False, f"could not forget {session_id}: {exc}")
    return Result(True, f"forgot {session_id}", {"session_id": session_id, "status": status_gone})


def action_kill(tmux: Tmux, session: str) -> Result:
    session = _text(session, 128)
    if not tmux.has_session(session):
        return Result(False, f"no tmux session named {session!r}")
    if not tmux.kill_session(session):
        return Result(False, f"tmux could not kill {session!r}")
    return Result(True, f"killed {session}")


def action_rename(tmux: Tmux, session: str, new_name: str) -> Result:
    session, new_name = _text(session, 128), _text(new_name, 64)
    if not valid_session_name(new_name):
        return Result(False, "new " + NAME_RULE)
    if not tmux.has_session(session):
        return Result(False, f"no tmux session named {session!r}")
    if tmux.has_session(new_name):
        return Result(False, f"session {new_name!r} already exists")
    if not tmux.rename_session(session, new_name):
        return Result(False, f"tmux could not rename {session!r}")
    return Result(True, f"renamed {session} -> {new_name}", {"name": new_name})


def action_detach(tmux: Tmux, session: str) -> Result:
    session = _text(session, 128)
    if not tmux.has_session(session):
        return Result(False, f"no tmux session named {session!r}")
    if not tmux.detach_session(session):
        return Result(False, f"tmux could not detach clients of {session!r}")
    return Result(True, f"detached all clients of {session} (session keeps running)")
