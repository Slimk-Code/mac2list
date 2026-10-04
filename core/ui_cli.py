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
    """Print the work block opener: gap, executing line, gap (no extra >)."""
    print()
    print(exec_line)
    print()


def repaint_for_work(header_fn, frame=None, quiet=False):
    """Unique clear + repaint + blank-line-below-> frame.

    Non-quiet: clear_screen() + header_fn() + one blank print().
    Quiet (Scan #): frame() when given, else a single print().
    The caller then runs one step at a time.
    """
    if quiet:
        if frame is not None:
            frame()
            print()
        else:
            print()
    else:
        header_fn()
        print()


def start_work_frame(header_fn, exec_line, frame=None, quiet=False):
    """One call doing the full scrape-1 opener: repaint + work head.

    Covers clear + repaint + blank-line-below-> + Executing line.
    Call it once per step inside a one-at-a-time loop.
    """
    repaint_for_work(header_fn, frame=frame, quiet=quiet)
    paint_work_head(exec_line)


def clear_screen():
    """Clear the terminal screen."""
    import os as _os
    _os.system("cls" if _os.name == "nt" else "clear")


def cooldown(seconds=3):
    """Simple pause."""
    import time as _time
    _time.sleep(seconds)


def countdown(seconds=5):
    """Visible countdown pause."""
    import sys as _sys
    import time as _time
    for i in range(seconds, 0, -1):
        _sys.stdout.write('\r  Continuing in {}s...  '.format(i))
        _sys.stdout.flush()
        _time.sleep(1)
    _sys.stdout.write('\r' + ' ' * 40 + '\r')
    _sys.stdout.flush()


def progress_bar(current, total, prefix="", width=30):
    """Single-line progress bar."""
    import sys as _sys
    if total <= 0:
        pct = 100.0
        filled = width
    else:
        pct = (current / total) * 100
        filled = int(width * current / total)
    bar = "=" * filled + "-" * (width - filled)
    line = "{}[{}] {:5.1f}% ({}/{})".format(prefix, bar, pct, current, total)
    _sys.stdout.write(chr(13) + line.ljust(80))
    _sys.stdout.flush()
    if current >= total:
        print()


def clear_batch_counter():
    """Erase the current batch counter line."""
    import sys as _sys
    _sys.stdout.write(chr(13) + " " * 80 + chr(13))
    _sys.stdout.flush()
