#!/usr/bin/env python3
"""spritz-cli — RSVP speed reader for the terminal."""

import argparse
import os
import re
import sys
import termios
import time
import tty


def parse_args():
    p = argparse.ArgumentParser(
        prog="spritz",
        description="RSVP speed reader. Shows text one word at a time.",
    )
    p.add_argument("file", help="Text file to read (.txt, .md, etc.)")
    p.add_argument("--wpm", type=int, default=500, help="Target words per minute (default: 500)")
    p.add_argument("--ramp", type=int, default=10, help="Words to ramp up/down over (default: 10)")
    p.add_argument("--start", type=int, default=0, help="Start from word N (0-indexed)")
    p.add_argument("--no-pause", action="store_true", help="Start playing immediately")
    p.add_argument("--step", type=int, default=50, help="WPM change per q/e keypress (default: 50)")
    p.add_argument("--min-wpm", type=int, default=100, help="Minimum WPM for ramp and speed down (default: 100)")
    return p.parse_args()


def load_words(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    tokens = text.split()
    cleaned = []
    for t in tokens:
        # Strip markdown syntax tokens
        if re.match(r'^(#{1,6}|---+|\*{3,}|_{3,}|```|~~|>\s*)$', t):
            continue
        # Strip leading markdown markers (headings, bullets, blockquotes)
        t = re.sub(r'^#{1,6}\s*', '', t)
        t = re.sub(r'^[>*\-+]\s*', '', t)
        # Strip markdown inline formatting: **bold**, *italic*, __x__, _x_, ~~strike~~, `code`
        t = re.sub(r'[*_~`]{1,3}', '', t)
        # Strip markdown links: [text](url) → text
        t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
        # Trim leading/trailing non-alphanumeric chars, but keep punctuation attached to words
        # e.g. "hello!" stays, but "***" or "---" becomes empty
        t = re.sub(r'^[^\w]+', '', t)  # strip leading specials
        t = re.sub(r'[^\w.!?,;:\'"…\-]+$', '', t)  # strip trailing, keep sentence punctuation
        # Skip if nothing readable remains (no letters or digits)
        if not re.search(r'[a-zA-Z0-9]', t):
            continue
        # Collapse excessive special chars within word (e.g. "hel---lo" → "hel-lo")
        t = re.sub(r'([^\w])\1{2,}', r'\1', t)
        cleaned.append(t)
    return cleaned


def orp_index(word):
    """Optimal Recognition Point: ~1/3 into the word."""
    n = len(word)
    if n <= 1:
        return 0
    if n <= 3:
        return 0
    if n <= 5:
        return 1
    return n // 3


def get_wpm(target, min_wpm, ramp, word_idx, total_words):
    """Calculate current WPM with ramp up at start and ramp down at end."""
    ramp_up_end = ramp
    ramp_down_start = total_words - ramp

    if word_idx < ramp_up_end:
        t = word_idx / max(ramp, 1)
        return int(min_wpm + (target - min_wpm) * t)
    elif word_idx >= ramp_down_start and ramp_down_start > ramp_up_end:
        remaining = total_words - word_idx
        t = remaining / max(ramp, 1)
        return int(min_wpm + (target - min_wpm) * t)
    return target


def render(word, orp, cols):
    """Render word with ORP letter in red, centered so ORP is at screen center."""
    center = cols // 2
    # Position word so ORP char is at center column
    padding = center - orp
    if padding < 0:
        padding = 0

    before = word[:orp]
    letter = word[orp] if orp < len(word) else ""
    after = word[orp + 1:] if orp + 1 < len(word) else ""

    # ANSI: red for ORP letter, white/default for rest
    RED = "\033[91m"
    RESET = "\033[0m"
    WHITE = "\033[97m"

    line = " " * padding + WHITE + before + RED + letter + WHITE + after + RESET
    return line


def render_progress(current, total, target_wpm, current_wpm, cols):
    """Render progress bar with word count and ETA based on target WPM."""
    remaining = total - current
    eta = fmt_time(remaining * 60 / max(target_wpm, 1))
    label = f" {current}/{total} {current_wpm}wpm {eta} left "
    bar_width = cols - len(label) - 4
    if bar_width < 10:
        bar_width = 10
    filled = int(bar_width * current / max(total, 1))
    empty = bar_width - filled
    bar = "\033[90m" + "━" * filled + "\033[38;5;238m" + "━" * empty + "\033[0m"
    return f"  {bar}{label}"


def render_marker(orp_col, cols):
    """Render the ▼ marker above the word at the ORP position."""
    center = cols // 2
    return " " * center + "\033[91m▼\033[0m"


def render_marker_below(cols):
    center = cols // 2
    return " " * center + "\033[91m▲\033[0m"


def fmt_time(seconds):
    """Format seconds as Xm Ys."""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def get_key(fd):
    """Read a single keypress. Returns special strings for ESC and arrow keys."""
    import select
    ch = os.read(fd, 1)
    if not ch:
        return ""
    if ch == b'\x1b':
        # Check if more bytes follow (arrow key sequence) or bare ESC
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            seq = os.read(fd, 2).decode("utf-8", errors="ignore")
            if seq == "[C":
                return "RIGHT"
            elif seq == "[D":
                return "LEFT"
            return ""  # unknown escape sequence
        return "ESC"  # bare escape key
    return ch.decode("utf-8", errors="ignore")


def main():
    args = parse_args()
    words = load_words(args.file)
    if not words:
        print("No words found in file.")
        sys.exit(1)

    total = len(words)
    idx = min(args.start, total - 1)
    target_wpm = args.wpm
    paused = not args.no_pause

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        # Hide cursor
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

        while idx < total:
            cols = os.get_terminal_size().columns
            rows = os.get_terminal_size().lines
            word = words[idx]
            orp = orp_index(word)
            current_wpm = get_wpm(target_wpm, args.min_wpm, args.ramp, idx, total)

            # Clear screen and position
            sys.stdout.write("\033[2J\033[H")

            # Calculate vertical center
            mid_row = rows // 2

            # Move to marker row
            sys.stdout.write(f"\033[{mid_row - 1};1H")
            sys.stdout.write(render_marker(orp, cols))

            # Word row
            sys.stdout.write(f"\033[{mid_row};1H")
            sys.stdout.write(render(word, orp, cols))

            # Below marker
            sys.stdout.write(f"\033[{mid_row + 1};1H")
            sys.stdout.write(render_marker_below(cols))

            # Progress bar
            sys.stdout.write(f"\033[{mid_row + 3};1H")
            sys.stdout.write(render_progress(idx + 1, total, target_wpm, current_wpm, cols))

            # Pause indicator
            if paused:
                pause_text = "\033[93m⏸  PAUSED — space to start, q/e speed, ←/→ skip, s quit\033[0m"
                sys.stdout.write(f"\033[{mid_row + 5};1H")
                vis_len = len("⏸  PAUSED — space to start, q/e speed, ←/→ skip, s quit")
                pad = max(0, (cols - vis_len) // 2)
                sys.stdout.write(" " * pad + pause_text)

            sys.stdout.flush()

            if paused:
                # Blocking read while paused
                while True:
                    ch = get_key(fd)
                    if ch == " ":
                        paused = False
                        break
                    elif ch in ("\x03", "ESC", "s"):
                        raise KeyboardInterrupt
                    elif ch == "e":
                        target_wpm = min(target_wpm + args.step, 2000)
                        break
                    elif ch == "q":
                        target_wpm = max(target_wpm - args.step, args.min_wpm)
                        break
                    elif ch == "RIGHT":
                        idx = min(idx + 1, total - 1)
                        break
                    elif ch == "LEFT":
                        idx = max(idx - 1, 0)
                        break
                continue

            # Playing: wait for word duration, check for input
            delay = 60.0 / max(current_wpm, 1)

            # Add extra delay for long words and punctuation
            if len(word) > 8:
                delay *= 1.3
            if word[-1] in ".!?":
                delay *= 1.5
            elif word[-1] in ",;:":
                delay *= 1.2

            deadline = time.monotonic() + delay

            while time.monotonic() < deadline:
                import select
                ready, _, _ = select.select([fd], [], [], 0.01)
                if ready:
                    ch = get_key(fd)
                    if ch == " ":
                        paused = True
                        break
                    elif ch in ("\x03", "ESC", "s"):
                        raise KeyboardInterrupt
                    elif ch == "e":
                        target_wpm = min(target_wpm + args.step, 2000)
                    elif ch == "q":
                        target_wpm = max(target_wpm - args.step, args.min_wpm)
                    elif ch == "RIGHT":
                        idx = min(idx + 10, total - 1)
                        break
                    elif ch == "LEFT":
                        idx = max(idx - 10, 0)
                        break

            if not paused:
                idx += 1

        # Done — show completion
        cols = os.get_terminal_size().columns
        rows = os.get_terminal_size().lines
        sys.stdout.write("\033[2J\033[H")
        mid_row = rows // 2
        done_msg = "Done!"
        pad = (cols - len(done_msg)) // 2
        sys.stdout.write(f"\033[{mid_row};1H" + " " * pad + "\033[92m" + done_msg + "\033[0m")
        sys.stdout.write(f"\033[{mid_row + 2};1H")
        sys.stdout.write(render_progress(total, total, target_wpm, target_wpm, cols))
        sys.stdout.flush()
        # Wait for any key
        os.read(fd, 1)

    except KeyboardInterrupt:
        pass
    finally:
        # Restore terminal
        sys.stdout.write("\033[?25h")  # Show cursor
        sys.stdout.write("\033[0m")    # Reset colors
        sys.stdout.write("\033[2J\033[H")  # Clear screen
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
