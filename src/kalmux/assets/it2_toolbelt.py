#!/usr/bin/env python3
"""Register the Kalmux web UI as an iTerm2 toolbelt tool (and reveal the toolbelt).

Needs the `iterm2` package and an API cookie so no permission dialog appears:
    export ITERM2_COOKIE="$(osascript -e 'tell application "iTerm2" to request cookie')"
    uv run --no-project --with iterm2 python scripts/it2_toolbelt.py --url http://127.0.0.1:47321/ --show
`kalmux setup` / `kalmux ui show` do exactly this. The registration persists in iTerm2's prefs
(NoSyncDynamicTools + ToolbeltTools), so it only has to run once per URL.
"""
import argparse
import sys

TOOL_ID = "vn.kal.kalmux.toolbelt"
TOOL_NAME = "Kalmux"
SHOW_TOOLBELT_MENU_ID = "Show Toolbelt"     # "Toggle Toolbelt" is not a valid identifier in 3.7


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--show", action="store_true", help="also make the toolbelt visible in the current window")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        import iterm2  # noqa: WPS433 (optional dependency, installed by uv on demand)
    except ImportError:
        print("it2_toolbelt: the `iterm2` package is missing (run via: uv run --no-project --with iterm2 python ...)", file=sys.stderr)
        return 2

    async def run(connection):
        await iterm2.tool.async_register_web_view_tool(connection, TOOL_NAME, TOOL_ID, True, args.url)
        note = ""
        if args.show:
            # Fails with DISABLED when no terminal window is key (iTerm2 in the background): registration
            # already succeeded, so report it and let the user press the shortcut instead.
            try:
                state = await iterm2.MainMenu.async_get_menu_item_state(connection, SHOW_TOOLBELT_MENU_ID)
                if not state.checked:
                    await iterm2.MainMenu.async_select_menu_item(connection, SHOW_TOOLBELT_MENU_ID)
                note = " (toolbelt shown)"
            except iterm2.MenuItemException as exc:
                note = f" (could not show the toolbelt now: {exc}; use View > Show Toolbelt, shift-cmd-B)"
        print(f"registered toolbelt tool {TOOL_ID!r} -> {args.url}{note}")

    try:
        iterm2.run_until_complete(run)
    except Exception as exc:  # connection refused, auth denied, ...
        print(f"it2_toolbelt: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
