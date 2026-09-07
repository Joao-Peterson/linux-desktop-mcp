#!/usr/bin/env python3
"""linux-desktop-mcp — an MCP server exposing the Linux desktop through AT-SPI2.

Rationale
---------
Screenshot-and-click automation on a live desktop fails in ways that are
invisible until they bite: HiDPI coordinate scaling, multi-monitor origins, and
synthesised keystrokes being re-mapped by whichever XKB group happens to be
active. AT-SPI2 sidesteps all of it. Applications publish a typed widget tree
over D-Bus, so elements are addressed by role and name, text is *read* rather
than recognised, and values are *set* through an interface rather than typed.

Every tool returns compact plain text rather than images, which keeps responses
in the hundreds of bytes instead of the megabytes a screen capture costs.

IMPORTANT: stdout is reserved for the MCP protocol. Diagnostics go to stderr.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Optional

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# AT-SPI calls are synchronous D-Bus round-trips into other processes. An
# unresponsive application would otherwise hang this server indefinitely.
ATSPI_METHOD_TIMEOUT_MILLISECONDS = 800
ATSPI_STARTUP_TIMEOUT_MILLISECONDS = 2000

# Guard rails against accidentally serialising an enormous tree. LibreOffice
# Calc, for instance, honestly reports a 1048576 x 16384 grid.
DEFAULT_MAXIMUM_TREE_DEPTH = 4
DEFAULT_MAXIMUM_NODE_COUNT = 250
ABSOLUTE_MAXIMUM_NODE_COUNT = 2000
MAXIMUM_TABLE_CELLS_PER_CALL = 2000

# Never enumerate more children than this from a single node. A spreadsheet grid
# reports its children as rows x columns — tens of billions for an empty Calc
# sheet — so eagerly listing them wedges the server. Use read_table_region to
# address such grids by coordinate instead.
MAXIMUM_CHILDREN_PER_NODE = 200

# Electron/Chromium apps nest their real UI far deeper than native toolkits: in
# VSCodium the menu bar sits at depth 15, below a stack of anonymous panels and
# sections. A search depth of 12 silently finds nothing in such apps, so the
# default has to clear that.
DEFAULT_SEARCH_DEPTH = 24

# Applied at import so that every consumer of this module — the MCP server and
# the test harness alike — is protected from an unresponsive application.
Atspi.set_timeout(ATSPI_METHOD_TIMEOUT_MILLISECONDS, ATSPI_STARTUP_TIMEOUT_MILLISECONDS)

mcp_server = MCPServer(
    name="linux-desktop",
    version="0.1.0",
    instructions=(
        "Inspect and control Linux desktop applications through the AT-SPI2 "
        "accessibility bus. Prefer this over screenshots and synthetic input: "
        "it is exact, cheap, and immune to HiDPI scaling and keyboard-layout "
        "problems.\n\n"
        "Typical flow: list_windows() to see what is open, then describe_ui() "
        "on a window ref to get a tree of elements with refs, then act on a ref "
        "with set_element_value() or invoke_element_action().\n\n"
        "Element refs (e1, e2, ...) are handed out by list_windows, "
        "describe_ui and find_elements, and stay valid while the target "
        "application keeps that widget alive. If a ref goes stale, re-run the "
        "call that produced it.\n\n"
        "Coverage is uneven by design of the underlying toolkits: GTK and Qt "
        "applications expose rich trees, Electron/Chromium apps are often "
        "shallow, and canvas- or game-rendered UIs expose nothing at all. When "
        "a tree is empty, the information genuinely is not there - say so "
        "rather than assuming the call failed."
    ),
)


# --------------------------------------------------------------------------- #
# Element reference registry
# --------------------------------------------------------------------------- #


class AccessibleReferenceRegistry:
    """Hands out short string refs for live Atspi.Accessible proxies.

    AT-SPI objects cannot be serialised into a tool response, so each one is
    cached here and addressed by a short id. The human-readable location string
    is kept alongside it purely so error messages can say *which* element went
    stale.
    """

    def __init__(self) -> None:
        self._accessible_by_reference: dict[str, Atspi.Accessible] = {}
        self._location_by_reference: dict[str, str] = {}
        self._next_reference_number: int = 1

    def register(self, accessible: Atspi.Accessible, location_description: str) -> str:
        reference_id = f"e{self._next_reference_number}"
        self._next_reference_number += 1
        self._accessible_by_reference[reference_id] = accessible
        self._location_by_reference[reference_id] = location_description
        return reference_id

    def resolve(self, reference_id: str) -> Atspi.Accessible:
        """Return the accessible for a ref, raising a readable error if unknown."""
        normalised_reference = reference_id.strip()
        if normalised_reference not in self._accessible_by_reference:
            raise ValueError(
                f"Unknown element ref '{reference_id}'. Refs are issued by "
                f"list_windows, describe_ui and find_elements within this "
                f"server session — re-run one of those to obtain a current ref."
            )
        return self._accessible_by_reference[normalised_reference]

    def describe_location(self, reference_id: str) -> str:
        return self._location_by_reference.get(reference_id.strip(), "<unknown>")


reference_registry = AccessibleReferenceRegistry()


# --------------------------------------------------------------------------- #
# Low-level AT-SPI helpers
# --------------------------------------------------------------------------- #


def _safe_role_name(accessible: Atspi.Accessible) -> str:
    try:
        return accessible.get_role_name() or "?"
    except GLib.Error:
        return "<unreachable>"


def _safe_element_name(accessible: Atspi.Accessible) -> str:
    try:
        return (accessible.get_name() or "").strip()
    except GLib.Error:
        return ""


def _supported_interfaces(accessible: Atspi.Accessible) -> list[str]:
    try:
        return list(Atspi.Accessible.get_interfaces(accessible))
    except (GLib.Error, AttributeError):
        return []


def _interesting_states(accessible: Atspi.Accessible) -> list[str]:
    """Return only the states worth showing, to keep the tree readable."""
    states_worth_reporting = {
        Atspi.StateType.FOCUSED: "focused",
        Atspi.StateType.SELECTED: "selected",
        Atspi.StateType.CHECKED: "checked",
        Atspi.StateType.EXPANDED: "expanded",
        Atspi.StateType.EDITABLE: "editable",
        Atspi.StateType.SENSITIVE: None,   # absence is what matters
        Atspi.StateType.SHOWING: None,     # absence is what matters
    }
    reported_states: list[str] = []
    try:
        state_set = accessible.get_state_set()
        for state_type, label in states_worth_reporting.items():
            present = state_set.contains(state_type)
            if label is not None and present:
                reported_states.append(label)
            if label is None and not present:
                reported_states.append(
                    "insensitive" if state_type == Atspi.StateType.SENSITIVE else "hidden"
                )
    except GLib.Error:
        return ["<states unreachable>"]
    return reported_states


def _read_text_contents(accessible: Atspi.Accessible) -> Optional[str]:
    """Read an element's text via the Text interface, or None if unsupported.

    The Text methods must be called as unbound interface functions; calling
    accessible.get_text(...) hits a deprecated single-argument shim instead.
    """
    if "Text" not in _supported_interfaces(accessible):
        return None
    try:
        character_count = Atspi.Text.get_character_count(accessible)
        if character_count <= 0:
            return ""
        return Atspi.Text.get_text(accessible, 0, character_count)
    except GLib.Error:
        return None


def _read_numeric_value(accessible: Atspi.Accessible) -> Optional[float]:
    if "Value" not in _supported_interfaces(accessible):
        return None
    try:
        return float(Atspi.Value.get_current_value(accessible))
    except (GLib.Error, TypeError):
        return None


def _available_action_names(accessible: Atspi.Accessible) -> list[str]:
    if "Action" not in _supported_interfaces(accessible):
        return []
    try:
        return [
            Atspi.Action.get_action_name(accessible, action_index)
            for action_index in range(Atspi.Action.get_n_actions(accessible))
        ]
    except GLib.Error:
        return []


def _format_node_line(
    accessible: Atspi.Accessible,
    reference_id: str,
    indent_depth: int,
) -> str:
    """Render one accessible as a single compact line of the tree."""
    line_parts: list[str] = [
        "  " * indent_depth + f"[{reference_id}] {_safe_role_name(accessible)}"
    ]

    element_name = _safe_element_name(accessible)
    if element_name:
        line_parts.append(f'"{element_name[:80]}"')

    text_contents = _read_text_contents(accessible)
    if text_contents and text_contents.strip() and text_contents.strip() != element_name:
        collapsed_text = " ".join(text_contents.split())[:80]
        line_parts.append(f"text={collapsed_text!r}")

    numeric_value = _read_numeric_value(accessible)
    if numeric_value is not None:
        line_parts.append(f"value={numeric_value:g}")

    state_labels = _interesting_states(accessible)
    if state_labels:
        line_parts.append("(" + ",".join(state_labels) + ")")

    action_names = _available_action_names(accessible)
    if action_names:
        line_parts.append("actions=" + "|".join(action_names[:4]))

    return " ".join(line_parts)


def _find_toplevel_accessible(accessible: Atspi.Accessible) -> Optional[Atspi.Accessible]:
    """Walk up to the containing window/frame/dialog, or None if not found."""
    toplevel_roles = {"frame", "window", "dialog", "alert"}
    current_node: Optional[Atspi.Accessible] = accessible
    for _ in range(32):  # depth guard against a cyclic or broken hierarchy
        if current_node is None:
            return None
        if _safe_role_name(current_node) in toplevel_roles:
            return current_node
        try:
            current_node = current_node.get_parent()
        except GLib.Error:
            return None
    return None


def _locate_x_window_id_by_title(window_title: str) -> Optional[int]:
    """Map a window title to an X window id via wmctrl, or None if ambiguous."""
    if not window_title.strip():
        return None
    try:
        wmctrl_output = subprocess.run(
            ["wmctrl", "-l"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None

    matching_window_ids: list[int] = []
    for wmctrl_line in wmctrl_output.splitlines():
        line_fields = wmctrl_line.split(None, 3)
        if len(line_fields) < 4:
            continue
        window_id_hex, _desktop, _host, line_title = line_fields
        if line_title.strip() == window_title.strip():
            try:
                matching_window_ids.append(int(window_id_hex, 16))
            except ValueError:
                continue

    # Refuse to guess when several windows share a title — sending input to the
    # wrong one is exactly the failure this function exists to prevent.
    return matching_window_ids[0] if len(matching_window_ids) == 1 else None


def _activate_and_verify_x_focus(target_x_window_id: int) -> bool:
    """Activate a window and confirm it really holds focus. False means abort."""
    try:
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", str(target_x_window_id)],
            timeout=8, check=True, capture_output=True,
        )
        focused_window_output = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return int(focused_window_output) == target_x_window_id
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def _child_count(accessible: Atspi.Accessible) -> int:
    try:
        return accessible.get_child_count()
    except GLib.Error:
        return 0


def _iterate_children(
    accessible: Atspi.Accessible,
    maximum_children: int = MAXIMUM_CHILDREN_PER_NODE,
) -> list[Atspi.Accessible]:
    """Return up to `maximum_children` children, never the whole reported set.

    The cap is load-bearing, not defensive tidiness: a spreadsheet grid reports
    rows x columns children, so an unbounded walk here never returns.
    """
    total_child_count = _child_count(accessible)
    collected_children: list[Atspi.Accessible] = []
    for child_index in range(min(total_child_count, maximum_children)):
        try:
            child_accessible = accessible.get_child_at_index(child_index)
        except GLib.Error:
            break
        if child_accessible is not None:
            collected_children.append(child_accessible)
    return collected_children


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp_server.tool(
    description=(
        "List applications on the AT-SPI bus and their top-level windows. "
        "Returns an element ref for each window, which is the usual starting "
        "point for describe_ui or find_elements."
    )
)
def list_windows(include_applications_without_windows: bool = False) -> str:
    """Enumerate accessible applications and their windows."""
    Atspi.init()
    desktop_root = Atspi.get_desktop(0)

    output_lines: list[str] = []
    application_count = 0

    for application_index in range(desktop_root.get_child_count()):
        try:
            application = desktop_root.get_child_at_index(application_index)
            if application is None:
                continue
            application_name = _safe_element_name(application) or "<unnamed>"
            try:
                toolkit_name = application.get_toolkit_name() or "?"
            except GLib.Error:
                toolkit_name = "?"

            window_accessibles = _iterate_children(application)
            if not window_accessibles and not include_applications_without_windows:
                continue

            application_count += 1
            output_lines.append(f"{application_name}  [toolkit={toolkit_name}]")
            for window_accessible in window_accessibles:
                if window_accessible is None:
                    continue
                window_reference = reference_registry.register(
                    window_accessible, f"{application_name} window"
                )
                window_title = _safe_element_name(window_accessible) or "<untitled>"
                window_role = _safe_role_name(window_accessible)
                output_lines.append(
                    f"    [{window_reference}] {window_role}: {window_title}"
                )
        except GLib.Error as atspi_error:
            output_lines.append(f"  <application {application_index} unreachable: {atspi_error}>")

    if not output_lines:
        return "No applications are currently exposing an accessibility tree."

    header = f"{application_count} application(s) with windows:\n"
    return header + "\n".join(output_lines)


@mcp_server.tool(
    description=(
        "Dump the accessibility tree beneath an element as indented text, with "
        "a ref for every node. Depth and node count are capped because some "
        "trees are effectively unbounded; increase them deliberately."
    )
)
def describe_ui(
    element_ref: str,
    maximum_depth: int = DEFAULT_MAXIMUM_TREE_DEPTH,
    maximum_nodes: int = DEFAULT_MAXIMUM_NODE_COUNT,
) -> str:
    """Walk an element's subtree breadth-first-ish, depth-limited."""
    root_accessible = reference_registry.resolve(element_ref)
    node_budget = min(max(1, maximum_nodes), ABSOLUTE_MAXIMUM_NODE_COUNT)

    rendered_lines: list[str] = []
    nodes_emitted = 0
    truncated_by_budget = False

    def walk(accessible: Atspi.Accessible, current_depth: int) -> None:
        nonlocal nodes_emitted, truncated_by_budget
        if nodes_emitted >= node_budget:
            truncated_by_budget = True
            return
        node_reference = reference_registry.register(
            accessible, f"{_safe_role_name(accessible)}:{_safe_element_name(accessible)}"
        )
        rendered_lines.append(_format_node_line(accessible, node_reference, current_depth))
        nodes_emitted += 1
        if current_depth >= maximum_depth:
            return

        total_child_count = _child_count(accessible)
        if total_child_count > MAXIMUM_CHILDREN_PER_NODE:
            # Say so explicitly: a silently clipped grid would otherwise look
            # like an application that exposes almost nothing.
            rendered_lines.append(
                "  " * (current_depth + 1)
                + f"... this node reports {total_child_count} children; showing the "
                + f"first {MAXIMUM_CHILDREN_PER_NODE}. If it is a grid, use "
                + f"read_table_region on [{node_reference}] instead."
            )

        for child_accessible in _iterate_children(accessible):
            walk(child_accessible, current_depth + 1)

    walk(root_accessible, 0)

    result = "\n".join(rendered_lines)
    if truncated_by_budget:
        result += (
            f"\n\n... truncated at {node_budget} nodes. Narrow the starting ref, "
            f"lower maximum_depth, or use find_elements instead of dumping."
        )
    return result or "<element exposes no accessible subtree>"


@mcp_server.tool(
    description=(
        "Search an element's subtree for nodes whose name, text or role matches "
        "a case-insensitive substring. Cheaper and far more targeted than "
        "dumping a whole tree. Note that a negative result can mean the search "
        "did not go deep enough rather than that nothing matched — Electron apps "
        "bury their real UI around depth 15, so raise maximum_depth before "
        "concluding an element is absent."
    )
)
def find_elements(
    element_ref: str,
    query: str,
    match_role_instead_of_name: bool = False,
    result_limit: int = 20,
    maximum_depth: int = DEFAULT_SEARCH_DEPTH,
) -> str:
    """Find descendants matching a substring query."""
    root_accessible = reference_registry.resolve(element_ref)
    lowercased_query = query.strip().lower()
    if not lowercased_query:
        raise ValueError("query must not be empty")

    matching_lines: list[str] = []
    # A search that matches nothing would otherwise walk the entire subtree; with
    # up to MAXIMUM_CHILDREN_PER_NODE children per level that is unbounded in
    # practice, so cap the total nodes examined.
    nodes_visited = 0
    visit_budget_exhausted = False

    def walk(accessible: Atspi.Accessible, current_depth: int) -> None:
        nonlocal nodes_visited, visit_budget_exhausted
        if len(matching_lines) >= result_limit or current_depth > maximum_depth:
            return
        if nodes_visited >= ABSOLUTE_MAXIMUM_NODE_COUNT:
            visit_budget_exhausted = True
            return
        nodes_visited += 1

        if match_role_instead_of_name:
            haystack = _safe_role_name(accessible).lower()
        else:
            haystack = " ".join(
                filter(
                    None,
                    [
                        _safe_element_name(accessible).lower(),
                        (_read_text_contents(accessible) or "").lower(),
                        _safe_role_name(accessible).lower(),
                    ],
                )
            )

        if lowercased_query in haystack:
            node_reference = reference_registry.register(
                accessible, f"{_safe_role_name(accessible)}:{_safe_element_name(accessible)}"
            )
            matching_lines.append(_format_node_line(accessible, node_reference, 0))

        for child_accessible in _iterate_children(accessible):
            walk(child_accessible, current_depth + 1)

    walk(root_accessible, 0)

    if not matching_lines:
        exhaustion_note = (
            f" (search stopped after examining {nodes_visited} nodes, so the "
            f"subtree was not covered exhaustively)"
            if visit_budget_exhausted
            else ""
        )
        return f"No descendant of {element_ref} matched {query!r}.{exhaustion_note}"

    suffix_notes: list[str] = []
    if len(matching_lines) >= result_limit:
        suffix_notes.append(f"stopped at result_limit={result_limit}; refine the query for more")
    if visit_budget_exhausted:
        suffix_notes.append(f"examined only the first {nodes_visited} nodes of the subtree")
    suffix = f"\n\n({'; '.join(suffix_notes)})" if suffix_notes else ""
    return "\n".join(matching_lines) + suffix


@mcp_server.tool(
    description="Read an element's full text through the Text interface, unabbreviated."
)
def get_element_text(element_ref: str) -> str:
    """Return the complete text of an element."""
    accessible = reference_registry.resolve(element_ref)
    text_contents = _read_text_contents(accessible)
    if text_contents is None:
        supported = _supported_interfaces(accessible) or ["<none>"]
        return (
            f"{element_ref} does not implement the Text interface. "
            f"Interfaces present: {', '.join(supported)}. "
            f"Its accessible name is {_safe_element_name(accessible)!r}."
        )
    return text_contents if text_contents else "<empty>"


@mcp_server.tool(
    description=(
        "Set an element's contents through the EditableText or Value interface "
        "— no keystrokes are synthesised, so this is immune to keyboard-layout "
        "and focus problems. Fails cleanly if the element is not writable."
    )
)
def set_element_value(element_ref: str, new_value: str) -> str:
    """Write a value into an element without typing."""
    accessible = reference_registry.resolve(element_ref)
    supported_interfaces = _supported_interfaces(accessible)

    if "EditableText" in supported_interfaces:
        try:
            was_written = Atspi.EditableText.set_text_contents(accessible, new_value)
        except GLib.Error as atspi_error:
            raise RuntimeError(f"EditableText.set_text_contents failed: {atspi_error}") from None
        if not was_written:
            raise RuntimeError(
                f"{element_ref} accepted the EditableText call but refused the write. "
                f"It may be read-only or not currently editable."
            )
        return f"Set {element_ref} via EditableText to {new_value!r}."

    if "Value" in supported_interfaces:
        try:
            numeric_value = float(new_value)
        except ValueError:
            raise ValueError(
                f"{element_ref} exposes only the numeric Value interface, "
                f"but {new_value!r} is not a number."
            ) from None
        try:
            Atspi.Value.set_current_value(accessible, numeric_value)
        except GLib.Error as atspi_error:
            raise RuntimeError(f"Value.set_current_value failed: {atspi_error}") from None
        return f"Set {element_ref} via Value to {numeric_value:g}."

    raise RuntimeError(
        f"{element_ref} is not writable through AT-SPI. "
        f"Interfaces present: {', '.join(supported_interfaces) or '<none>'}. "
        f"Available actions: {', '.join(_available_action_names(accessible)) or '<none>'}."
    )


@mcp_server.tool(
    description=(
        "Invoke a named action on an element (click, press, activate, toggle, "
        "...) through the Action interface. Call with no action_name to list "
        "what the element offers."
    )
)
def invoke_element_action(element_ref: str, action_name: str = "") -> str:
    """Perform an accessible action by name."""
    accessible = reference_registry.resolve(element_ref)
    action_names = _available_action_names(accessible)

    if not action_names:
        raise RuntimeError(
            f"{element_ref} exposes no actions. "
            f"Interfaces present: {', '.join(_supported_interfaces(accessible)) or '<none>'}."
        )

    if not action_name:
        return f"{element_ref} offers actions: {', '.join(action_names)}"

    requested_action = action_name.strip().lower()
    for action_index, candidate_name in enumerate(action_names):
        if candidate_name.strip().lower() == requested_action:
            try:
                succeeded = Atspi.Action.do_action(accessible, action_index)
            except GLib.Error as atspi_error:
                raise RuntimeError(f"do_action({candidate_name}) failed: {atspi_error}") from None
            return (
                f"Invoked {candidate_name!r} on {element_ref}"
                + ("" if succeeded else " (the application reported no effect)")
            )

    raise ValueError(
        f"{element_ref} has no action named {action_name!r}. "
        f"It offers: {', '.join(action_names)}"
    )


@mcp_server.tool(
    description=(
        "Read a rectangular block of cells from an element implementing the "
        "Table interface (spreadsheets, data grids, file lists). A region is "
        "required because grids routinely declare millions of rows."
    )
)
def read_table_region(
    element_ref: str,
    first_row: int = 0,
    first_column: int = 0,
    row_count: int = 10,
    column_count: int = 5,
) -> str:
    """Read table cell text by coordinate."""
    accessible = reference_registry.resolve(element_ref)
    if "Table" not in _supported_interfaces(accessible):
        raise RuntimeError(
            f"{element_ref} does not implement the Table interface. "
            f"Interfaces present: {', '.join(_supported_interfaces(accessible)) or '<none>'}."
        )

    if row_count * column_count > MAXIMUM_TABLE_CELLS_PER_CALL:
        raise ValueError(
            f"Requested {row_count * column_count} cells, over the "
            f"{MAXIMUM_TABLE_CELLS_PER_CALL} limit. Read a smaller region."
        )

    try:
        declared_row_count = Atspi.Table.get_n_rows(accessible)
        declared_column_count = Atspi.Table.get_n_columns(accessible)
    except GLib.Error as atspi_error:
        raise RuntimeError(f"Could not read table dimensions: {atspi_error}") from None

    output_lines = [
        f"Table declares {declared_row_count} rows x {declared_column_count} columns. "
        f"Showing rows {first_row}-{first_row + row_count - 1}, "
        f"columns {first_column}-{first_column + column_count - 1} (0-indexed):"
    ]

    for row_index in range(first_row, min(first_row + row_count, declared_row_count)):
        row_cell_texts: list[str] = []
        for column_index in range(
            first_column, min(first_column + column_count, declared_column_count)
        ):
            try:
                cell_accessible = Atspi.Table.get_accessible_at(
                    accessible, row_index, column_index
                )
                if cell_accessible is None:
                    row_cell_texts.append("")
                    continue
                cell_text = _read_text_contents(cell_accessible)
                if cell_text is None:
                    cell_text = _safe_element_name(cell_accessible)
                row_cell_texts.append((cell_text or "").strip())
            except GLib.Error:
                row_cell_texts.append("<unreachable>")
        output_lines.append(f"  row {row_index}: {row_cell_texts}")

    return "\n".join(output_lines)


@mcp_server.tool(
    description=(
        "Escape hatch for elements that refuse a direct write: focus the element "
        "through AT-SPI (no coordinates involved), then insert text via the "
        "clipboard and Shift+Insert. Use when set_element_value reports that "
        "only the numeric Value interface is available — LibreOffice Calc cells "
        "are the common case. Layout-safe: nothing is typed character by "
        "character, so XKB group mismatches cannot corrupt punctuation, and "
        "Shift+Insert avoids the Ctrl+letter shortcuts that break under "
        "non-Latin layouts. Requires xclip and xdotool."
    )
)
def set_text_via_clipboard(element_ref: str, text: str) -> str:
    """Focus an element accessibly, then paste text into it."""
    for required_executable in ("xclip", "xdotool", "wmctrl"):
        if shutil.which(required_executable) is None:
            raise RuntimeError(
                f"{required_executable} is not installed, so clipboard insertion "
                f"is unavailable. Install it, or use set_element_value if the "
                f"element supports a direct write."
            )

    accessible = reference_registry.resolve(element_ref)
    if "Component" not in _supported_interfaces(accessible):
        raise RuntimeError(
            f"{element_ref} cannot be focused (no Component interface), so text "
            f"cannot be directed to it."
        )

    # Atspi.Component.grab_focus() reports success for the *application's* notion
    # of focus while X keyboard focus stays on another window entirely. Trusting
    # it means synthetic keystrokes land in whatever the user happens to have in
    # front — a stray Return in a chat window sends a message. So establish and
    # verify real X focus first, and refuse to send anything if we cannot.
    toplevel_accessible = _find_toplevel_accessible(accessible)
    if toplevel_accessible is None:
        raise RuntimeError(
            f"Could not find the top-level window containing {element_ref}, so "
            f"there is no way to confirm where keystrokes would go. Refusing to "
            f"send input."
        )

    toplevel_window_title = _safe_element_name(toplevel_accessible)
    target_x_window_id = _locate_x_window_id_by_title(toplevel_window_title)
    if target_x_window_id is None:
        raise RuntimeError(
            f"Could not uniquely match the window titled "
            f"{toplevel_window_title!r} to an X window (missing, or several "
            f"windows share that title). Refusing to send input, because it "
            f"would go to whichever window currently has focus."
        )

    if not _activate_and_verify_x_focus(target_x_window_id):
        raise RuntimeError(
            f"Asked the window manager to focus {toplevel_window_title!r} "
            f"(0x{target_x_window_id:08x}) but it did not take focus. Refusing "
            f"to send input rather than typing into the wrong window."
        )

    try:
        Atspi.Component.grab_focus(accessible)
    except GLib.Error as atspi_error:
        raise RuntimeError(f"Could not focus {element_ref}: {atspi_error}") from None

    # Preserve whatever the user had on their clipboard.
    previous_clipboard_contents: Optional[bytes]
    try:
        previous_clipboard_contents = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            capture_output=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        previous_clipboard_contents = None

    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode(), timeout=5, check=True,
        )
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "shift+Insert"],
            timeout=5, check=True,
        )
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "Return"], timeout=5, check=True
        )
    except (subprocess.SubprocessError, OSError) as insertion_error:
        raise RuntimeError(f"Clipboard insertion failed: {insertion_error}") from None
    finally:
        if previous_clipboard_contents is not None:
            try:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=previous_clipboard_contents, timeout=5,
                )
            except (subprocess.SubprocessError, OSError):
                pass  # restoring the clipboard is best-effort

    return (
        f"Focused {element_ref} and inserted {text!r} via clipboard. "
        f"Verify with get_element_text or read_table_region — this path is "
        f"less certain than a direct write."
    )


@mcp_server.tool(
    description=(
        "Get an element ref for a single table cell by (row, column). Needed "
        "because a grid's children are enumerated row-major, so find_elements "
        "cannot reach cells beyond the first row. Use the returned ref with "
        "get_element_text, set_element_value or invoke_element_action."
    )
)
def get_table_cell_ref(element_ref: str, row: int, column: int) -> str:
    """Return a ref for one cell of a table, addressed by coordinate."""
    accessible = reference_registry.resolve(element_ref)
    if "Table" not in _supported_interfaces(accessible):
        raise RuntimeError(
            f"{element_ref} does not implement the Table interface. "
            f"Interfaces present: {', '.join(_supported_interfaces(accessible)) or '<none>'}."
        )
    try:
        cell_accessible = Atspi.Table.get_accessible_at(accessible, row, column)
    except GLib.Error as atspi_error:
        raise RuntimeError(
            f"Could not reach cell (row={row}, column={column}): {atspi_error}"
        ) from None
    if cell_accessible is None:
        raise RuntimeError(f"Cell (row={row}, column={column}) is not exposed.")

    cell_reference = reference_registry.register(
        cell_accessible, f"cell(row={row},column={column}) of {element_ref}"
    )
    return _format_node_line(cell_accessible, cell_reference, 0)


@mcp_server.tool(
    description=(
        "Give an element keyboard focus through the Component interface. Useful "
        "before an action that only applies to the focused widget."
    )
)
def focus_element(element_ref: str) -> str:
    """Grab focus for an element."""
    accessible = reference_registry.resolve(element_ref)
    if "Component" not in _supported_interfaces(accessible):
        raise RuntimeError(
            f"{element_ref} does not implement the Component interface, so it "
            f"cannot be focused."
        )
    try:
        focus_was_granted = Atspi.Component.grab_focus(accessible)
    except GLib.Error as atspi_error:
        raise RuntimeError(f"grab_focus failed: {atspi_error}") from None
    return (
        f"Focused {element_ref}."
        if focus_was_granted
        else f"{element_ref} declined focus (it may not be focusable right now)."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Initialise AT-SPI, then serve MCP over stdio."""
    Atspi.set_timeout(
        ATSPI_METHOD_TIMEOUT_MILLISECONDS, ATSPI_STARTUP_TIMEOUT_MILLISECONDS
    )
    initialisation_status: Any = Atspi.init()
    print(f"linux-desktop-mcp: Atspi.init() -> {initialisation_status}", file=sys.stderr)
    mcp_server.run("stdio")


if __name__ == "__main__":
    main()
