<h1 align="center">Kalmux</h1>

<p align="center">
<b>Ten Claude Code agents in tmux, one place to watch them all.</b><br> A thin management layer that lives inside iTerm2 instead of replacing your terminal.
</p>

<p align="center">
<img alt="macOS" src="https://img.shields.io/badge/macOS-13%2B-111?logo=apple&logoColor=white">
<img alt="iTerm2" src="https://img.shields.io/badge/iTerm2-3.7.5%2B-1f6feb">
<img alt="tmux" src="https://img.shields.io/badge/tmux-3.x-1bb91f">
<img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white">
<img alt="License" src="https://img.shields.io/badge/License-MIT-black">
</p>

<p align="center"><img src="assets/toolbelt.gif" alt="The Kalmux toolbelt: an agent turns amber and jumps to the top of the list, one click goes to its tab, and the sessions that died are still there to resume" width="420"></p>

<p align="center"><sub>An agent stops for you, its card turns amber and jumps to the top; one click and you are in that tab. Recorded from the bundled mock server.</sub></p>

<p align="center"><a href="README.vi.md">Tiếng Việt</a></p>

---

## Why Kalera built this

A working day at [Kalera AI](https://www.kalera.ai) looks like four to ten tmux sessions, each running a Claude Code agent, each in a different repository. The agents are fast. The human is the bottleneck: one of them is waiting for an answer, one has been grinding for twenty minutes, one finished ten minutes ago and nobody noticed, and one is about to run out of context.

iTerm2 3.7 ships a Claude Code integration that answers exactly this question, one tab at a time. Its first releases broke under tmux: every pane inside a tmux server inherits the same `ITERM_SESSION_ID`, so every agent reported its state to one hidden gateway tab. iTerm2 3.7.5 fixed that routing, but a status per tab still leaves you clicking through ten tabs to find the one agent that needs you, and nothing outside iTerm2 (an SSH shell, the tmux status line) can see it.

Kalmux fixes the routing and then builds the missing dashboard on top of it. Nothing is replaced:

| Layer           | Stays what it is                                                            |
| --------------- | --------------------------------------------------------------------------- |
| **tmux**        | the server, so SSH and Tailscale keep working from any device               |
| **iTerm2**      | the desk client: control mode `tmux -CC`, native tabs, Cockpit, toolbelt    |
| **Claude Code** | untouched; Kalmux reads its hooks, its session registry and its status line |

Three small pieces do the work: a hook wrapper, the `kalmux` CLI, and a web UI served on `127.0.0.1:47321` that iTerm2 shows in its toolbelt.

## What you get

**One card per session, sorted by who needs you.** Waiting first, then working, idle, busy. Click a card to jump to its iTerm2 tab; if the session has no tab, one opens in the window you are in.

**A context gauge on every card.** The percentage comes straight from Claude Code's own status-line payload, so you can see which agent is about to compact before you hand it a long prompt. The card also carries the model and the session cost, and the header shows the five-hour quota window when your plan reports one (only from readings less than an hour old, and never a window that has already reset).

**A `Gone` section that brings sessions back.** On 14 September 2026 an agent tidying up its own sandbox ran `tmux kill-server`. The session it ran in was itself inside tmux, so `$TMUX` pointed at the real server: four sessions, every tab, and hours of work went away in one command. Kalmux now writes a small trail for every Claude session (its id, folder, tmux pane and identity color). When a session dies, its card moves to `Gone` with a **Resume** button that recreates the tmux session in the right folder with the right color and types `claude --resume <id>` at the prompt, waiting for your Enter. The repository also carries a `CLAUDE.md` that forbids agents from ever running `tmux kill-server` again.

**Your sessions survive a reboot, and a crash.** A restart kills the tmux server and every session in it, and so does an iTerm2 crash or a force quit: iTerm2 starts the Kalmux server, the Kalmux server starts tmux, and macOS takes the whole group down together. Kalmux records the live list every five seconds together with the identity of the tmux server it came from, so as soon as a different server answers (or none does) it recreates every session that was still open: same name, same directory, same color. They come back without a tab each: the toolbelt shows a banner with **Open all** and a `restored` badge on every card, so you open the ones you want, one click at a time or all at once. `claude --continue` in a restored session picks that directory's conversation up where it stopped.

**Identity colors that survive everything.** Twenty palette names or any hex value, stored in the tmux session itself and replayed into the iTerm2 tab on every attach, so a project keeps its color across detach, reboot and rebuild.

**Everything from the keyboard.** `Go to waiting` jumps to the first agent that needs you, `n` cycles through the rest, `1`-`9` open the nth card, `/` searches by session name, project or the title Claude gave the conversation.

**It works over SSH.** `ssh mac kalmux ls` prints the same table without iTerm2, and the tmux status line shows `[working]` / `[waiting]` per window for plain attaches.

A non-interactive SSH command gets a bare `PATH`: no `~/.local/bin`, no Homebrew, and a `python3` that is macOS's 3.9. If `ssh mac kalmux ls` answers `command not found`, call it by its path (`ssh mac '~/.local/bin/kalmux ls'`) or put `export PATH="$HOME/.local/bin:$PATH"` in `~/.zshenv`, which zsh reads for every invocation, interactive or not. A clone's `bin/kalmux` copes with the old `python3` on its own: it re-runs itself under a Python 3.11+ it finds in Homebrew, python.org, uv or `~/.local/bin`, or under the one `KALMUX_PYTHON=/path/to/python3.13` names, and prints a single line saying what to install when there is none.

## Install

Requires macOS with iTerm2 3.7.5+ (3.7.0 to 3.7.4 work, but their own Claude Code hooks report every tmux pane to one hidden tab, and `kalmux doctor` says so), tmux 3.x, Python 3.11+, `jq`, and `uv` for the toolbelt registration.

```bash
brew install kalera-labs/tap/kalmux
kalmux setup
```

Recent Homebrew asks you to vouch for a third-party tap before it will run its formula: `brew trust kalera-labs/tap`.

Or from a clone, which is also how you hack on it:

```bash
git clone https://github.com/kalera-labs/kalmux.git
cd kalmux
bin/kalmux setup
```

Either way `kalmux` ends up on your PATH: Homebrew puts the command there itself, and from a clone `setup` links `~/.local/bin/kalmux` to `bin/kalmux`.

`setup` is idempotent and reversible. It creates `~/.config/kalmux/config.toml`, adds Kalmux's own hook entries to `~/.claude/settings.json`, links the CLI, sets two iTerm2 preferences, adds a managed block to `~/.tmux.conf`, installs an iTerm2 AutoLaunch script that starts the UI server, routes Claude Code's status line through Kalmux, registers the toolbelt tool, and finishes by running `kalmux doctor`.

Then open the toolbelt: **View > Toolbelt** (⇧⌘B) and pick **Kalmux**. Reattach your sessions once (`kalmux open <name>` per session, from a single window) so the new tab preferences apply.

<details>
<summary>What setup touches, and why</summary>

- `~/.claude/settings.json` → `hooks`: one Kalmux entry per event the wrapper handles (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`, `SessionEnd`), each running `kalmux-hook` with a 5-second timeout. iTerm2's own `cc-status` entries stay exactly as they are and run in parallel. The command never contains the word `cc-status`, because iTerm2 treats every entry that does as its own (it rewrites and deduplicates them), and it points at a path that survives upgrades (`/opt/homebrew/opt/kalmux/...` under Homebrew, never the versioned Cellar directory). The first time the file changes it is copied to `settings.json.hooks.bak-kalmux` (mode 0600). When iTerm2 3.7.5 or later has its own hooks installed, the entries carry `--status=iterm2`: iTerm2 then reports Claude's status for tmux panes itself and the wrapper only keeps Kalmux's own jobs (tmux state, trails, tab color, alerts), so there is never a second writer.
- `~/.config/iterm2/cc-status` is iTerm2's. Kalmux up to 0.5.1 replaced it with a symlink to its wrapper; iTerm2 puts its own link back whenever a newer version launches, which silently cut Kalmux off. `setup` now gives the path back to iTerm2 (it points it at the bundled `cc-status` again) and never touches it otherwise.
- `~/.local/bin/kalmux` → the CLI, when you run from a clone. `kmux` and `tm` stay as aliases to the same file, so older muscle memory and scripts keep working. An installed package already owns the name, so `setup` leaves it alone.
- iTerm2 preferences `OpenTmuxWindowsIn=2` (tmux windows open as tabs in the attaching window) and `AutoHideTmuxClientSession=true`.
- A managed block in `~/.tmux.conf`: `allow-passthrough on`, `exit-empty off` (so killing the last session does not stop the server and look like a crash), a status line that shows `[working]` / `[waiting]` per window, and a `client-attached` hook that replays state and color into a fresh tab.
- `~/Library/Application Support/iTerm2/Scripts/AutoLaunch.scpt`, which starts the UI server whenever iTerm2 launches. An AutoLaunch script you wrote yourself is never overwritten. The generated script bakes in an absolute interpreter path and a `PATH` prefix, because iTerm2 launches under the login `PATH` where neither a modern `python3` nor Homebrew's `tmux` is visible, and a server that cannot find `tmux` shows an empty toolbelt after every reboot.
- `~/.claude/settings.json` → `statusLine` is routed through `kalmux statusline`; the previous value is saved verbatim and the whole file is copied to `settings.json.bak-kalmux` (mode 0600, it can hold API keys) before the key is rewritten. Skip this step with `--no-statusline`. The routed command is `<python> <kalmux> statusline` with both paths absolute (the interpreter that ran `setup`), so it never depends on the `PATH` Claude Code runs it under and your own status line cannot go blank because `python3` resolved to something too old. Running `kalmux setup` again upgrades the bare 0.5 form, and an interpreter that is gone or older than 3.11, to this form; a baked interpreter that still works is left alone, so running setup under another `python3` (an SSH shell, say) does not rewrite `settings.json` each time. Only the first routing copies `settings.json.bak-kalmux`: a re-bake never replaces your own copy with one that already carries Kalmux's command. `kalmux doctor` turns red if the baked interpreter has since been removed.
- The toolbelt tool, registered through iTerm2's Python API. The API cookie is fetched through AppleScript so no permission dialog appears.

**Why iTerm2 starts the server and not launchd:** talking to iTerm2 needs an Apple Event, and the repository may live on an external volume. A launchd-spawned process has neither TCC grant (Automation, Removable Volumes) and blocks on macOS permission prompts, while anything spawned from iTerm2's own process tree inherits both.

</details>

## The command

```
kalmux ls [--json]            every session with project, Claude state, context %, age, title, color
kalmux go <topic>             focus a session by name, or by part of its project or Claude title; over SSH it attaches here
kalmux open <session>         open the session as a new control-mode tab in the current window (closes with the session)
kalmux open --restored        open every session a restore recreated that nobody has opened yet, in one window
kalmux color <session> <c>    identity color: #rrggbb, one of 20 palette names, or none
kalmux new <name> [--cwd D] [--color C] [--claude] [--no-attach]
kalmux kill | detach <session>        kalmux rename <session> <new>
kalmux dead [--all] [--json]  Claude sessions that are gone: killed first, then the clean exits
kalmux resume <id|name>       recreate the tmux session and type `claude --resume <id>` at the prompt
kalmux forget <session-id>    drop a dead session's trail
kalmux restore [--dry-run] [--tabs | --no-tabs]
                              recreate the sessions that were open before the tmux server went away
kalmux attach <session>       the right verb for where you are: -CC in iTerm2, switch-client in tmux, plain over SSH
kalmux reapply [session]      re-send state and tab color to attached panes
kalmux ui show|status|start|stop|restart|install|uninstall|url|serve
kalmux config                 show the effective configuration and where it lives
kalmux statusline [install|uninstall|status]
kalmux doctor [--no-ui] [--no-statusline] [--fix]
kalmux setup  [--no-ui] [--no-statusline]
```

## How it knows

No screen scraping. Every number has a source.

| Signal                         | Where it comes from                                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `waiting` / `working` / `idle` | Claude Code hooks (Kalmux's own entries), written by the wrapper into tmux pane options (`@cc_state`, `@cc_detail`, `@cc_since`) |
| identity and liveness          | Claude Code's session registry, `~/.claude/sessions/<pid>.json`, which maps each conversation to an exact tmux pane |
| `busy`                         | tmux's own `#{pane_current_command}`: a pane with no Claude in it that is running `make`, `pytest`, a script        |
| context %, model, cost, quota  | Claude Code's status-line payload, captured by `kalmux statusline`                                                  |
| gone sessions                  | the trail files Kalmux appends on `SessionStart`, `Stop` and `SessionEnd`                                           |
| tab identity                   | iTerm2's `tmuxWindowPane` session variable, so a tmux pane maps to exactly one tab                                  |

Two states describe trouble rather than work: `gone` means the Claude process died, `stale` means hook state left behind on a pane that no longer runs Claude. In `kalmux ls`, `~` marks a value recalled from the registry and `?` (flagged `overdue` in the API, with its own `counts.overdue`) marks a working or waiting session that has shown no sign of life for over thirty minutes: no state change, no status-line refresh and no transcript write. The age on the card is time in the current state, so a long turn or a background workflow can read `working 8h` without a `?` while it keeps writing. The context gauge, model and cost stay on a card for as long as the registry says the session is alive (the tooltip says how old the reading is and an old one is dimmed); they only disappear an hour after a session is gone. A registry `waiting` status shows as `waiting`, with Claude's reason (`permission prompt`, `input needed`, `dialog open`) as the card detail.

Dead sessions are classified `killed` (no `SessionEnd` ever arrived), `clean` (logout or exit) or `superseded` (`/clear` or a resume, hidden unless you pass `--all`). Trails and status files older than `tombstones.keep_days` are pruned automatically.

**Known blind spot:** a shell running a script of its own kind, a bash script under a bash shell, reports the shell's own name, so `busy` reads it as an empty prompt. Catching that needs a shell hook rather than a tmux format.

## Restore after a reboot or a crash

A reboot kills the tmux server and every session with it, and so does an iTerm2 crash or a force quit: AutoLaunch starts the Kalmux server, the Kalmux server starts tmux, and the three of them share one macOS resource coalition, so they go down together. That is exactly what happened here on 4 October 2026: no reboot, no logout, half a second between iTerm2 dying and the last session going with it. Quitting iTerm2 normally does not do that: it only detaches its control-mode clients. Killing a session does not either: that is you saying so. So what Kalmux writes down is not "the sessions are gone" but which tmux server the saved list describes.

The UI server records the live sessions (name, directory, identity color, creation order) into `${KALMUX_STATE_DIR}/sessions.json` every five seconds, and immediately after any kill, rename, new or resume, together with the pid and start time of the tmux server they belong to. It writes only when something changed, and it never writes anything while no server answers: a server that is gone is a server whose sessions you did not kill. It also takes one last snapshot when it is asked to stop, if tmux is still there to answer.

`kalmux setup` puts `set -s exit-empty off` in the managed block of `~/.tmux.conf`, so killing the last session leaves the server up with an empty list instead of stopping it. Without that line the two cases are indistinguishable (the last session killed, and tmux dead), and the session you just killed would come back at the next restore. `kalmux doctor` checks for the line.

A restore is due when the saved list has sessions and the server that wrote it is not the one answering now: nothing is running (a reboot, or a tmux that died and nobody restarted it), or another server holds the socket. That is checked when the UI server starts, which covers the reboot and the crash that took Kalmux down with iTerm2, and again on every five-second round, which covers a tmux that died while the Kalmux server kept running. Either way the saved list is copied to `sessions.previous.json` first, every session that is not already there is recreated (an existing name is never stolen or renamed), and the state of the server that runs now is recorded, so nothing is restored twice. Everything it did is in `ui.log`, and `kalmux doctor` shows the snapshot's age, whether it still matches the live sessions, and whether it still belongs to a server that is gone.

**No tab until you ask for one.** By default (`open_tabs = false` under `[restore]`) a restore opens nothing in iTerm2: twenty tabs springing open at login is not what anyone wants. Every recreated session carries a mark in tmux (the session option `@kalmux_restored`, set to the time of the restore), so it survives a restart of the UI server. The toolbelt shows "N sessions restored at HH:MM, not opened yet" with **Open all** and **Dismiss**, plus a `restored` badge on those cards. Clicking a card opens its tab as usual and clears its mark; **Open all** opens a tab for every marked session in the current iTerm2 window, on a thread of its own, with its progress in the banner; **Dismiss** keeps the sessions and drops the marks. A card you click (or a session you dismiss) while **Open all** is still working through the list is skipped, so no session gets a second tab, and tabs that could not be opened are counted in the banner ("2 of 8 could not be opened (see ui.log)") until the next **Open all**. A session that a client attaches some other way (`tmux attach`, ssh) loses its mark within five seconds. `kalmux open --restored` does what **Open all** does from a terminal, and `ui.log` says "recreated, not opened" for each one. Set `open_tabs = true` to get the 0.5 behaviour back: one tab per restored session, a window first if there is none.

**A directory that is not there yet.** A project on an external disk (`/Volumes/<name>/...`) can be missing at login simply because the disk is not mounted yet. The restore then creates every session it can right away, waits up to 60 seconds for those volumes, and only then gives up on them. A session whose directory is still missing starts in `$HOME`, says so in `ui.log` and with a `folder missing` badge (and the missing path on the card), and keeps its real directory in the session option `@kalmux_cwd`: while it sits in `$HOME` the snapshot keeps recording the real directory, so the next restore puts it back where it belongs once the directory exists again. The badge goes away as soon as that directory is there, and `@kalmux_cwd` is dropped once you take the session somewhere else: from then on the session belongs wherever you left it, `$HOME` included. Before 0.6 the first snapshot after such a fallback wrote `$HOME` over the real directory for good.

A deliberate `tmux kill-server` is indistinguishable from a crash, so those sessions come back too. To drop sessions for good, end them one at a time: the UI, `kalmux kill`, `tmux kill-session`, or `exit` in the last pane.

Only name, directory and color come back: no windows, no panes, no running processes and no conversation. A restored session is a shell in the right directory, which is what `claude --continue` needs.

```bash
kalmux restore --dry-run     # what would happen: create / exists / its directory is not there -> ~
kalmux restore               # do it by hand; tabs follow [restore] open_tabs (override: --tabs / --no-tabs)
kalmux open --restored       # then open them all, in one window
```

`kalmux restore` reads `sessions.json` while that file still belongs to a server that is gone, and `sessions.previous.json` once the server running now has taken the file over. It never writes the snapshot, so you can run it as often as you like. Set `enabled = false` under `[restore]` to keep the recording (and the copy) and turn only the automatic part off; a list lost that way is still a line in `ui.log`, naming the file that holds it.

## Configuration

`~/.config/kalmux/config.toml` is yours; Kalmux creates it once and never rewrites it.

```toml
[claude]
# Typed into a new session by `kalmux new --claude`.
new = "claude"
# Typed into the pane by `kalmux resume`. {session_id} is replaced with the Claude session id.
resume = "claude --resume {session_id}"
# "type" leaves the command on the prompt; "run" presses Enter for you.
resume_mode = "type"

[tombstones]
keep_days = 30

[notify]
# "auto": post the iTerm2 alert Claude Code skips inside tmux, unless you set preferredNotifChannel yourself.
iterm2 = "auto"
bell = false

[restore]
# When the tmux server that held your sessions is gone (a reboot, an iTerm2 crash or quit, a tmux that
# died), Kalmux recreates every session that was open: same name, directory and color.
# `kalmux restore` does the same by hand.
# The session list is recorded either way; this only turns the automatic restore on and off.
enabled = true
# Also open one iTerm2 tab per recreated session. Off: the sessions come back without a tab, and the
# toolbelt marks them "restored" with an "Open all" button (`kalmux open --restored` does the same).
open_tabs = false
```

If you live in `--dangerously-skip-permissions`, put it in these templates once and both `new` and `resume` will follow. Changes take effect immediately; no restart.

**Notifications.** Claude Code picks its notification channel from `TERM_PROGRAM`. tmux sets that to `tmux`, so inside tmux the default `auto` channel finds no method and Claude posts nothing, while the same Claude in a plain iTerm2 tab posts a macOS notification, with its sound, the moment it needs you. That silence is the first thing people notice after moving their agents into tmux. Kalmux fills it: on the same `Notification` event the hook posts the same OSC 9 alert into the pane, and iTerm2 shows it exactly as it would have. `iterm2 = "auto"` steps aside as soon as `~/.claude.json` carries a `preferredNotifChannel` of your own (`claude config set -g preferredNotifChannel iterm2` also works inside tmux, since Claude wraps the sequence for tmux itself), `"always"` posts regardless, `"never"` turns it off, and `bell = true` adds the terminal bell, like Claude's own `iterm2_with_bell`. iTerm2's own hook entry, which runs next to Kalmux's, does not post these alerts (iTerm2's status-change alert is a one-shot toggle you arm in the Session Status tool), so the two never double up.

**State directory:** trails, status records and the saved status line live under `$KALMUX_STATE_DIR`, default `~/.local/state/kalmux` (`XDG_STATE_HOME` is not read). If you override it, set it somewhere Claude Code, the hook and the UI server all inherit, because a file written under one value is invisible to a reader started under another.

## The status line tap

`kalmux statusline` sits in front of whatever status line you already use. It stores a normalized copy of each refresh, which is where the context gauge reads from, then passes up to 1 MiB of the payload to your original command and exits with its status, so your own prompt keeps rendering exactly as before. The overhead is about 18 ms per refresh.

Your original `statusLine` object is saved verbatim. `kalmux statusline uninstall` puts it back and renames the saved copy rather than deleting it, so installing again records whatever status line you use by then. If that saved copy disappears while the key is still routed through Kalmux, uninstall refuses instead of leaving you with no status line, and `kalmux doctor` turns red and points at the settings backup.

## Security

The server binds to loopback only. Every API call carries a per-process CSRF token minted into the page, `Host` and `Origin` are pinned to loopback, and the page runs under a nonce CSP with no inline handlers and no external resources. The token defends against web pages, not against other processes of the same user: anything that can open a loopback socket as you can also run `tmux kill-session` directly.

Status replay only ever writes to character devices under `/dev`, tmux records that do not look like tmux's own ids are dropped, and the settings backup is written 0600 because that file can hold API keys.

## Troubleshooting

`kalmux doctor` checks the config file, Kalmux's own hook entries in `settings.json` (every event present, pointing at this wrapper, in the mode that matches iTerm2), that `~/.config/iterm2/cc-status` is iTerm2's again, that iTerm2 is 3.7.5 or newer when its own hooks run next to Kalmux's, whether hooks still reach Kalmux (`last event 12s ago`, plus the sessions that never reported), `jq`, the tmux.conf block, both iTerm2 preferences, the toolbelt registration, the AutoLaunch script and its baked paths, the status-line routing and its baked interpreter, and the server: that it answers, that it runs this version of the code, and which `tmux` binary it can see. Every red line is followed by a one-line remedy.

`kalmux doctor --fix` re-applies only the failing steps that are safe to repeat: the Claude Code hooks, the CLI and alias links, the tmux.conf block and the status-line command (only re-baking one that is already routed: a status line you kept with `setup --no-statusline` is never routed by `--fix`). It prints what it is about to run first. It never writes iTerm2's preferences, the AutoLaunch script or the toolbelt, and never starts the server; for those it prints the command to run yourself.

**Hooks that stop arriving.** The wrapper touches `hook.heartbeat` in the state directory on every event, and `hooks/<session id>` for the session that sent it. Not every change of state comes with a hook (pressing Esc fires no `Stop`, and an approved permission prompt fires nothing until the tool ends), so the alarm is about sessions, not moments: when a live Claude session keeps changing state for more than two minutes after the entries were written (or after it started) without sending a single hook event, or `settings.json` has lost Kalmux's entries, the UI shows a red banner naming those sessions (states, the Gone list and alerts are then out of date) with a **Repair** button that re-writes the hook entries and nothing else, and `kalmux doctor` turns red. Claude Code normally picks up edited hooks in running sessions by itself; restart a session only if the banner still names it. Repair says so when the entries were already in place and the problem is a session that never reported. While the hooks are not arriving, Kalmux keeps every trail instead of pruning the ones older than `keep_days`: they are the only record of what to resume.

After changing Kalmux's own code, run `kalmux ui restart` from a shell inside iTerm2, so the server keeps iTerm2's TCC grants.

**A restart that never happens.** iTerm2 asks "Quit iTerm2?" when something tells it to quit, and macOS waits for that dialog during a restart: the machine sits there until you answer, and if you walked away it is still sitting there when you come back. The sessions are safe either way (iTerm2 only detaches its tmux clients), but nothing restarts. Lift it with `defaults write com.googlecode.iterm2 NeverBlockSystemShutdown -bool true` (Kalmux never changes this preference for you). The confirmation then only applies to a Cmd-Q you typed yourself.

## Development

```bash
uv run --no-project --with pytest --with pytest-cov python -m pytest -q --cov=src/kalmux --cov-report=term-missing
uvx ruff check .
python3 scripts/dev/mock_server.py 47399    # the UI with fake data at http://127.0.0.1:47399/
kalmux ui restart                           # after changing server code, from a shell inside iTerm2
```

Python standard library only, no runtime dependencies. `src/kalmux/` holds the modules, `src/kalmux/assets/` the hook wrapper and the single-file front end, `bin/kalmux` runs the whole thing straight from a clone.

## License

MIT. Made by [Kalera AI](https://www.kalera.ai).
