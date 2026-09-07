#!/usr/bin/env python3
"""Exercise the atspi-mcp tool functions directly, bypassing MCP transport.

Run against a live desktop session. Uses whatever LibreOffice Calc window
happens to be open as the table-reading fixture, and degrades gracefully if it
is absent.
"""

import re
import sys

import server as atspi_mcp_server


def report(step_description: str, result_text: str, line_limit: int = 12) -> None:
    print(f"\n=== {step_description} ===")
    result_lines = str(result_text).splitlines()
    for line in result_lines[:line_limit]:
        print("   " + line)
    if len(result_lines) > line_limit:
        print(f"   ... (+{len(result_lines) - line_limit} more lines)")


def main() -> int:
    window_listing = atspi_mcp_server.list_windows()
    report("list_windows()", window_listing, line_limit=16)

    # Pick the LibreOffice window ref out of the listing, if present.
    libreoffice_window_ref = None
    for listing_line in window_listing.splitlines():
        if "Untitled" in listing_line or "LibreOffice" in listing_line:
            reference_match = re.search(r"\[(e\d+)\]", listing_line)
            if reference_match:
                libreoffice_window_ref = reference_match.group(1)
                break

    if libreoffice_window_ref is None:
        print("\n!! No LibreOffice window found; skipping the table tests.")
        return 0

    print(f"\nUsing LibreOffice window ref: {libreoffice_window_ref}")

    tree_dump = atspi_mcp_server.describe_ui(
        libreoffice_window_ref, maximum_depth=3, maximum_nodes=25
    )
    report("describe_ui(depth=3, nodes=25)", tree_dump, line_limit=14)

    table_matches = atspi_mcp_server.find_elements(
        libreoffice_window_ref, "table", match_role_instead_of_name=True, result_limit=3
    )
    report("find_elements(role~'table')", table_matches)

    table_reference_match = re.search(r"\[(e\d+)\]", table_matches)
    if table_reference_match is None:
        print("\n!! No table element found; skipping read_table_region.")
        return 0

    table_ref = table_reference_match.group(1)
    cell_block = atspi_mcp_server.read_table_region(
        table_ref, first_row=0, first_column=0, row_count=7, column_count=3
    )
    report(f"read_table_region({table_ref})", cell_block, line_limit=12)

    # Error paths should be readable rather than tracebacks.
    for bad_call_description, bad_call in [
        ("unknown ref", lambda: atspi_mcp_server.get_element_text("e99999")),
        ("non-table read", lambda: atspi_mcp_server.read_table_region(libreoffice_window_ref)),
        ("unwritable set", lambda: atspi_mcp_server.set_element_value(
            libreoffice_window_ref, "nope")),
    ]:
        try:
            bad_call()
            print(f"\n=== error path: {bad_call_description} === -> unexpectedly succeeded")
        except Exception as expected_error:
            print(f"\n=== error path: {bad_call_description} ===")
            print(f"   {type(expected_error).__name__}: {expected_error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
