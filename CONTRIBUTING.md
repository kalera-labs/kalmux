# Contributing

Bug reports and small, focused pull requests are welcome.

## Running the tests

```bash
uv run --no-project --with pytest --with pytest-cov python -m pytest -q --cov=src/kalmux --cov-report=term-missing
uvx ruff check .
```

The suite brings its own fake `tmux` and fake iTerm2, so it does not touch your sessions, but it only means much on macOS: the hook is written for the bash 3.2 that ships with macOS, and several checks read macOS `defaults`.

## Working on Kalmux itself

```bash
git clone https://github.com/kalera-labs/kalmux.git
cd kalmux
bin/kalmux setup                            # safe to re-run; see the README for what it touches
python3 scripts/dev/mock_server.py 47399    # the UI with fake data, no tmux and no iTerm2 needed
kalmux ui restart                           # after changing server code, from a shell inside iTerm2
```

`src/kalmux/` holds the modules, `src/kalmux/assets/` the hook wrapper and the single-file front end, `bin/kalmux` runs the package straight from the clone.

## House rules

- Python standard library only. Kalmux has no runtime dependencies and should keep none.
- New behaviour comes with a test. The suite is the only thing standing between a refactor and someone's running agents.
- Never write a tmux command that can reach a user's real server from a test or a script. Sandbox tmux runs as `env -u TMUX tmux -L <own-socket> ...`, one socket per test, cleaned up per session — and never `kill-server`.
- Product surfaces (UI, CLI, docs, commit messages) are English.
