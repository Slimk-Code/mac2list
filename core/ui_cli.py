# ============================================================
# UI PIECES  —  tiny screen parts shared by every UI shell.
# Each piece only prints; titles and words are passed in by the
# caller, so the changing part can live anywhere (main file,
# another core module, wherever is easiest).
# One piece at a time: header first.
# ============================================================

def paint_header(title):
    """Print the standard top frame with a variable title line.

    Produces exactly:
          =================================================================
          <title>
          =================================================================
    """
    print("  " + "=" * 65)
    print("  {}".format(title))
    print("  " + "=" * 65)


def paint_caption(header_line):
    """Print one caption line plus the dash below it."""
    print(header_line)
    print("  " + "-" * 65)


def paint_rows(lines):
    """Print already-formatted row lines, one per row."""
    for line in lines:
        print(line)


def paint_footer(help_lines):
    """Print the footer dash, help lines, then blank plus prompt marker."""
    if isinstance(help_lines, str):
        help_lines = [help_lines]
    print()
    print("  " + "-" * 65)
    for line in help_lines:
        print(line)
        print()
        print("  > ", end="", flush=True)


def paint_gap(count=1):
    """Print blank gap lines between blocks."""
    for _ in range(max(0, int(count))):
        print()


def paint_prompt(label):
    """Print one question label and leave the cursor for input."""
    print("  {} : ".format(label), end="")


def paint_work_head(exec_line):
    """Print the work block opener: marker, gap, executing line, gap."""
    print("  > ")
    print()
    print(exec_line)
    print()
