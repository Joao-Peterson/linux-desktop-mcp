# atspi-mcp

An MCP server that exposes the Linux desktop through **AT-SPI2**, the same
accessibility bus screen readers use. Applications publish a typed widget tree
over D-Bus, so elements are addressed by role and name instead of pixels, text is
*read* rather than recognised, and values are *set* through an interface rather
than typed.

Built and verified on Linux Mint / Cinnamon, X11, LibreOffice 24.2, Python 3.12.

## Why

Screenshot-and-click automation fails on a live desktop in ways that stay
invisible until they bite. All three of these were hit while building this
server, on this machine:

| Failure | Cause |
|---|---|
| Clicked the Name Box instead of the AutoSum button | Coordinates read off a downscaled crop of a 3780x2128 HiDPI window, then mapped back by hand |
| `=SUM(R[-5]C:R[-1]C)` arrived as `=SUM(R[-5]C^R[-1]C)` | XKB layout is `us,ru,us`; on the ru group `:` sits on Shift+6, which is `^` on the us group. Silent, and only affects punctuation |
| `Ctrl+V` did nothing at all | Ctrl+letter shortcuts do not resolve under a non-Latin group |

None of these can occur when reading and writing through the accessibility tree.
A typical response is a few hundred bytes of exact text, against ~1 MB for a
screen capture that then has to be interpreted.

## Install

System packages first — PyGObject (`gi`, providing `Atspi`) cannot be
pip-installed into an isolated venv, which is why the venv below is created with
`--system-site-packages`:

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core xclip xdotool wmctrl
```

Fedora: `sudo dnf install python3-gobject at-spi2-core xclip xdotool wmctrl`.
Arch: `sudo pacman -S python-gobject at-spi2-core xclip xdotool wmctrl`.

Then, from a clone, inside your graphical session:

```bash
./install.sh
```

That creates the venv, registers `atspi-desktop` with Claude Code (`--scope
user`) and with Claude Desktop if its config exists, and runs a smoke test. Paths
come from the checkout location and the D-Bus address from `id -u`, so nothing is
machine-specific. Claude Desktop must be fully quit and reopened to load it.

Then, for Electron/Chromium apps (see below):

```bash
python3 enable_chromium_accessibility.py --list   # what it detected
python3 enable_chromium_accessibility.py         # apply
```

Requirements the installer checks for you: the accessibility bus running
(`at-spi-bus-launcher`), and `org.gnome.desktop.interface toolkit-accessibility`
true — it sets that if needed. `set_text_via_clipboard` additionally needs
`xclip`, `xdotool` and `wmctrl`, and is **X11 only**; the reading tools and
`set_element_value` work under Wayland.

## Tools

| Tool | Purpose |
|---|---|
| `list_windows` | Applications on the bus and their top-level windows; the usual entry point |
| `describe_ui` | Indented subtree dump, depth- and node-capped |
| `find_elements` | Substring search over name/text/role within a subtree |
| `get_element_text` | Full text via the Text interface |
| `set_element_value` | Write via EditableText, or numerically via Value |
| `invoke_element_action` | Perform a named action (click, press, activate) |
| `read_table_region` | Read a rectangular block of cells from a grid |
| `get_table_cell_ref` | Ref for one cell by (row, column) |
| `set_text_via_clipboard` | Guarded fallback for widgets that refuse a direct write |
| `focus_element` | Grab focus via the Component interface |

Element refs (`e1`, `e2`, ...) are handed out by `list_windows`, `describe_ui`,
`find_elements` and `get_table_cell_ref`, and live as long as the server process
and the target widget.

## Measured limitations

These are findings from testing, not speculation.

**Coverage is uneven, and that is the real ceiling.** Measured on this machine,
walking each window with a 1200-node budget to depth 14:

| Application | Toolkit | Nodes reached | Max depth | Notes |
|---|---|---|---|---|
| Firefox | Gecko | 1200 (hit cap) | 14 | 919 EditableText widgets; full menu structure |
| Telegram | Qt | 800 (hit cap) | 8 | table cells, list items, buttons |
| Cisco Secure Client | GTK | 87 | 8 | labels, menu items, 3 EditableText |
| VSCodium | Electron | **1** | 0 | frame only — no children at all |
| Slack | Electron | **1** | 0 | frame only — no children at all |

Gecko, Qt and GTK are all richly introspectable. **Electron/Chromium apps expose
nothing but their frame** — not merely a shallow tree, literally zero children.

This is not a permanent limitation, and it was verified rather than assumed.
Chromium builds its accessibility tree lazily, only when it believes an
assistive technology is listening. On this system `org.a11y.Status` reports
`IsEnabled: true` but `ScreenReaderEnabled: false`, and neither app was launched
with an accessibility flag, so the tree is never constructed.

Two VSCodium instances measured side by side, same version, same machine:

| Instance | Nodes | Depth | Content |
|---|---|---|---|
| default launch | 1 | 0 | frame only |
| `--force-renderer-accessibility` | 99+ | 16+ | `document web`, `menu bar`, named `File`/`Edit`/`View` items, headings |

**The global D-Bus switch is not usable on this desktop.** Setting
`ScreenReaderEnabled` on `org.a11y.Status` to true looks like the clean one-line
fix, and it is documented as such, but on Cinnamon it is bidirectionally coupled
to the `org.gnome.desktop.a11y.applications screen-reader-enabled` GSettings key.
Measured behaviour:

1. set the D-Bus property true → the settings daemon starts **Orca**, which
   speaks and takes over large parts of the keyboard;
2. stop Orca, or set the GSettings key back to false → the D-Bus property returns
   to false and Chromium goes blind again.

There is no state where the flag is on and no screen reader is running. So the
per-launcher flag is the only workable route:

```bash
python3 enable_chromium_accessibility.py --list      # detect only
python3 enable_chromium_accessibility.py --dry-run   # show the rewrites
python3 enable_chromium_accessibility.py             # apply
python3 enable_chromium_accessibility.py --revert    # remove what it wrote
python3 enable_chromium_accessibility.py --exclude zoom --exclude docker
```

Apps are detected, not hardcoded: each packaged `.desktop` file's program is
resolved and its directory searched for a Chromium runtime marker
(`v8_context_snapshot.bin`, `app.asar`, `chrome-sandbox`, …). Gecko has no
equivalent, so Firefox is correctly skipped. Markers under a `cef/` or
`qtwebengine/` subdirectory are ignored, because a native app that merely embeds
a browser view — Zoom is Qt with CEF under `/opt/zoom/cef` — is already
accessible through its own toolkit and has no reason to accept a Chromium switch.
Detection cannot be perfect; `--list` first, and `--exclude` anything that looks
wrong.

The script writes `--force-renderer-accessibility` into per-user copies under
`~/.local/share/applications`, which override the packaged launchers by XDG
basename precedence and survive package and Flatpak updates. Nothing is modified
in place, and it refuses to overwrite a user override it did not write itself.
`--revert` deletes only its own output, recognised by a marker comment.

Verified afterwards with Orca **not** running and `ScreenReaderEnabled: false`:
VSCodium relaunched from its menu entry exposed `menu item "File"`,
`page tab "Explorer"` and `document web` at depth 14. Apps already running when
the override was written stay blind until their next launch.

Caveats worth knowing:

- it covers **menu, launcher and URL-handler launches only**. A Flatpak started
  by hand with `flatpak run com.vscodium.codium` bypasses the `.desktop` file and
  gets no flag;
- a newly installed Chromium app needs the script re-run to be picked up.

**Electron trees are deep.** Native toolkits put useful widgets within ~8 levels;
VSCodium's menu bar is at depth 15, under a stack of anonymous `panel` and
`section` nodes whose text reads as `￼` (object-replacement) placeholders.
`find_elements` therefore defaults to `maximum_depth=24`. A shallower search
returns "no match" on content that is present — during development a depth-12
search reported the File menu as absent when it was simply three levels below the
cut-off. Treat a negative search result on an Electron app as inconclusive until
you have raised the depth.

Canvas- and game-rendered UIs expose nothing regardless. An empty tree usually
means the information is genuinely absent, not that the call failed.

**LibreOffice Calc cells expose only the numeric `Value` interface, not
`EditableText`.** Consequences:

- Writing numbers works exactly: `set_element_value(cell, "42")` lands, no keystrokes.
- Writing **text** to a specific cell is **not possible** through AT-SPI here.
- The `set_text_via_clipboard` fallback does not rescue this: `grab_focus` on a
  cell does not move Calc's cell cursor, so the paste is swallowed with no error.
  For text in Calc, use the UNO scripting API or write the `.ods` file directly.

**`Atspi.Component.grab_focus()` returning `True` does not mean the window holds
X keyboard focus.** This is the sharp edge. An early version of
`set_text_via_clipboard` trusted it and fired `Shift+Insert`+`Return` blind; the
keystrokes went to whichever window actually had focus rather than the target.
`set_text_via_clipboard` now resolves the target's top-level window, matches it
to an X window id via `wmctrl`, activates it, and **verifies** with
`xdotool getactivewindow` before sending anything — raising instead of typing if
any step is unconfirmed. It also refuses when two windows share a title.

**Grids report enormous child counts.** An empty Calc sheet honestly reports
1048576 x 16384 children. Enumerating them eagerly wedges the server, so child
enumeration is capped at 200 per node and truncation is stated in the output.
Use `read_table_region` / `get_table_cell_ref` for grids; `find_elements` cannot
reach past the first row because children are ordered row-major.

**Any synthetic-input path remains inherently racy** on a desktop the user is
also using: focus can change between the check and the keystroke. Prefer
`set_element_value`, which sends no input at all.

## Testing

```bash
./.venv/bin/python test_atspi_tools.py    # exercises tools against the live session
```

The suite reads whatever LibreOffice Calc window is open as its table fixture
and skips those checks if none is present.

## A note on what this can see

AT-SPI is a full accessibility bus: with the tools here, a window's tree includes
message text, document contents and page titles. That is the point — it is what
makes the server useful instead of a screenshot parser — but it means the same
call that lists a spreadsheet's cells will read a chat window's messages. Treat
tool output as you would the screen itself, and be deliberate about pasting it
into logs or issues.

## License

MIT — see [LICENSE](LICENSE).
