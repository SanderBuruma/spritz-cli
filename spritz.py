#!/usr/bin/env python3
"""spritz-cli — RSVP speed reader for the terminal."""

import argparse
import os
import re
import select
import sys
import termios
import time
import tty

# ── ANSI helpers ──────────────────────────────────────────────────────────────

RED = "\033[91m"
WHITE = "\033[97m"
GRAY = "\033[90m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BG_BLUE = "\033[44m"
BG_GRAY = "\033[48;5;236m"
BG_DARK = "\033[48;5;234m"


def pill(text, fg=WHITE, bg=BG_GRAY):
    """Render a colored pill/badge."""
    return f"{fg}{bg} {text} {RESET}"


def center_text(text, cols, visible_len=None):
    """Center text accounting for ANSI escape codes."""
    if visible_len is None:
        visible_len = len(re.sub(r'\033\[[^m]*m', '', text))
    pad = max(0, (cols - visible_len) // 2)
    return " " * pad + text


# ── CLI args ──────────────────────────────────────────────────────────────────

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


# ── Document loading ──────────────────────────────────────────────────────────

def clean_token(t):
    """Clean a single token, return cleaned word or None."""
    if re.match(r'^(#{1,6}|---+|\*{3,}|_{3,}|```|~~|>\s*)$', t):
        return None
    t = re.sub(r'^[>*\-+]\s*', '', t)
    t = re.sub(r'[*_~`]{1,3}', '', t)
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'^[^\w]+', '', t)
    t = re.sub(r'[^\w.!?,;:\'"…\-]+$', '', t)
    if not re.search(r'[a-zA-Z0-9]', t):
        return None
    t = re.sub(r'([^\w])\1{2,}', r'\1', t)
    return t


def load_document(path):
    """Load file and return words, headings, and word-to-heading mapping."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    is_md = path.lower().endswith(('.md', '.markdown', '.mkd'))

    words = []
    headings = []  # {"level": int, "text": str, "word_idx": int}
    word_heading_idx = []  # parallel to words: index into headings or -1

    current_heading_idx = -1

    for line in lines:
        # Detect markdown headings
        if is_md:
            m = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                # Strip inline formatting from heading text
                text = re.sub(r'[*_~`]{1,3}', '', text)
                headings.append({"level": level, "text": text, "word_idx": len(words)})
                current_heading_idx = len(headings) - 1
                # Don't add heading words to the word stream — skip to next line
                continue

        # Process normal tokens
        for t in line.split():
            # Strip leading heading markers if somehow inline
            t = re.sub(r'^#{1,6}\s*', '', t)
            cleaned = clean_token(t)
            if cleaned:
                words.append(cleaned)
                word_heading_idx.append(current_heading_idx)

    return words, headings, word_heading_idx


def get_breadcrumb(headings, word_heading_idx, word_idx):
    """Get the heading breadcrumb chain for a given word index."""
    if not headings or word_idx >= len(word_heading_idx):
        return []

    h_idx = word_heading_idx[word_idx]
    if h_idx < 0:
        return []

    # Build chain: walk backwards from current heading to find parent hierarchy
    chain = []
    current = headings[h_idx]
    chain.append(current)

    # Walk backwards to find parent headings (lower level numbers)
    for i in range(h_idx - 1, -1, -1):
        h = headings[i]
        if h["level"] < chain[-1]["level"]:
            chain.append(h)
            if h["level"] == 1:
                break

    chain.reverse()
    return chain


def get_section_bounds(headings, h_idx, total_words):
    """Get (start_word, end_word) for a heading's section.
    A section ends where the next heading of same or higher level begins, or at EOF."""
    if h_idx < 0 or h_idx >= len(headings):
        return (0, total_words)
    start = headings[h_idx]["word_idx"]
    level = headings[h_idx]["level"]
    end = total_words
    for i in range(h_idx + 1, len(headings)):
        if headings[i]["level"] <= level:
            end = headings[i]["word_idx"]
            break
    return (start, end)


def get_parent_heading_idx(headings, h_idx):
    """Find the parent heading (lower level number) for a given heading."""
    if h_idx <= 0:
        return -1
    target_level = headings[h_idx]["level"]
    for i in range(h_idx - 1, -1, -1):
        if headings[i]["level"] < target_level:
            return i
    return -1


# ── ORP & timing ──────────────────────────────────────────────────────────────

def orp_index(word):
    n = len(word)
    if n <= 3:
        return 0
    if n <= 5:
        return 1
    return n // 3


def get_wpm(target, min_wpm, ramp, word_idx, total_words):
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


def fmt_time(seconds):
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_word(word, orp, cols):
    center = cols // 2
    padding = max(0, center - orp)
    before = word[:orp]
    letter = word[orp] if orp < len(word) else ""
    after = word[orp + 1:] if orp + 1 < len(word) else ""
    return " " * padding + WHITE + before + RED + letter + WHITE + after + RESET


def render_marker_above(cols):
    return " " * (cols // 2) + RED + "▼" + RESET


def render_marker_below(cols):
    return " " * (cols // 2) + RED + "▲" + RESET


def render_breadcrumb(chain, cols):
    """Render heading breadcrumb with colored pills."""
    if not chain:
        return ""
    parts = []
    colors = [CYAN, MAGENTA, YELLOW, GREEN, WHITE, GRAY]
    for i, h in enumerate(chain):
        color = colors[i % len(colors)]
        parts.append(f"{color}{BOLD}{h['text']}{RESET}")
    crumb = f" {GRAY}›{RESET} ".join(parts)
    # Calculate visible length
    vis_len = sum(len(h['text']) for h in chain) + 3 * (len(chain) - 1)
    return center_text(crumb, cols, vis_len)


BAR_WIDTH = 30
SECTION_COLORS = [CYAN, MAGENTA, YELLOW, GREEN]


def render_progress(current, total, target_wpm, current_wpm, cols):
    remaining = total - current
    eta = fmt_time(remaining * 60 / max(target_wpm, 1))
    wpm_pill = pill(f"{current_wpm} wpm", CYAN, BG_DARK)
    eta_pill = pill(eta, YELLOW, BG_DARK)
    count_text = f"{GRAY}{current}/{total}{RESET}"
    filled = int(BAR_WIDTH * current / max(total, 1))
    bar = f"{GREEN}{'━' * filled}{GRAY}{'━' * (BAR_WIDTH - filled)}{RESET}"
    return f"  {bar} {count_text} {wpm_pill} {eta_pill}"


def _section_bar(pct, label_text, level, cols, dim=False):
    """Render a single section progress bar line."""
    filled = int(BAR_WIDTH * pct)
    color = SECTION_COLORS[min(level - 1, len(SECTION_COLORS) - 1)]
    bar = f"{color}{'━' * filled}{GRAY}{'━' * (BAR_WIDTH - filled)}{RESET}"
    label = f"{color}{DIM if dim else ''}{label_text}{RESET}"
    return f"  {bar} {label} {GRAY}{int(pct * 100)}%{RESET}"


def render_section_progress(word_idx, headings, word_heading_idx, total_words, cols):
    """Render section progress bars for current heading and its parent."""
    if not headings or word_idx >= len(word_heading_idx):
        return ""
    h_idx = word_heading_idx[word_idx]
    if h_idx < 0:
        return ""

    lines = []

    # Current section
    h = headings[h_idx]
    start, end = get_section_bounds(headings, h_idx, total_words)
    if end - start > 0:
        lines.append(_section_bar((word_idx - start) / (end - start), h["text"], h["level"], cols))

    # Parent section
    parent_idx = get_parent_heading_idx(headings, h_idx)
    if parent_idx >= 0:
        ph = headings[parent_idx]
        p_start, p_end = get_section_bounds(headings, parent_idx, total_words)
        if p_end - p_start > 0:
            lines.append(_section_bar((word_idx - p_start) / (p_end - p_start), ph["text"], ph["level"], cols, dim=True))

    return "\r\n".join(lines)


def render_help_bar(cols, has_headings):
    """Render the bottom help bar with pill-styled keys."""
    keys = [
        (pill("SPACE", WHITE, BG_BLUE), "play/pause"),
        (pill("q", WHITE, BG_DARK) + "/" + pill("e", WHITE, BG_DARK), "speed"),
        (pill("←", WHITE, BG_DARK) + "/" + pill("→", WHITE, BG_DARK), "skip"),
    ]
    if has_headings:
        keys.append((pill("i", CYAN, BG_DARK), "index"))
    keys.append((pill("s", RED, BG_DARK), "quit"))

    parts = []
    for key, desc in keys:
        parts.append(f"{key} {GRAY}{desc}{RESET}")
    line = "  ".join(parts)
    vis_len = sum(len(desc) + 5 + 3 for _, desc in keys)  # rough estimate
    return center_text(line, cols, vis_len)


# ── Index view ────────────────────────────────────────────────────────────────

def render_index(headings, cursor, scroll_offset, cols, rows, current_heading_idx):
    """Render the full-screen heading index/TOC view."""
    out = []

    # Title bar
    title = pill(" TABLE OF CONTENTS ", BOLD + WHITE, BG_BLUE)
    out.append("\033[2J\033[H")
    out.append(center_text(title, cols, len(" TABLE OF CONTENTS ") + 2))
    out.append("\r\n\r\n")

    # Available rows for headings (leave room for title, help, padding)
    avail_rows = rows - 6
    if avail_rows < 3:
        avail_rows = 3

    visible_start = scroll_offset
    visible_end = min(len(headings), scroll_offset + avail_rows)

    for i in range(visible_start, visible_end):
        h = headings[i]
        indent = "  " * (h["level"] - 1)
        marker = "▸" if h["level"] <= 2 else "▹"

        is_cursor = (i == cursor)
        is_current = (i == current_heading_idx)

        if is_cursor:
            # Highlighted row
            prefix = f"  {BG_BLUE}{WHITE}{BOLD}"
            suffix = f"{RESET}"
            word_count = ""
            if i + 1 < len(headings):
                word_count = f"  {GRAY}({headings[i + 1]['word_idx'] - h['word_idx']} words)"
            line = f"{prefix} {indent}{marker} {h['text']}{suffix}{word_count}"
        elif is_current:
            # Current reading position
            line = f"  {CYAN}{indent}{marker} {h['text']}{RESET} {pill('HERE', CYAN, BG_DARK)}"
        else:
            color = WHITE if h["level"] <= 2 else GRAY
            line = f"  {color}{indent}{marker} {h['text']}{RESET}"

        out.append(line + "\r\n")

    # Scroll indicators
    if scroll_offset > 0:
        out.insert(3, center_text(f"{GRAY}▲ more above{RESET}", cols, 12) + "\r\n")
    if visible_end < len(headings):
        out.append(center_text(f"{GRAY}▼ more below{RESET}", cols, 12) + "\r\n")

    # Help bar at bottom
    out.append(f"\033[{rows - 1};1H")
    help_parts = [
        f"{pill('j', WHITE, BG_DARK)}/{pill('k', WHITE, BG_DARK)} {GRAY}navigate{RESET}",
        f"{pill('ENTER', WHITE, BG_BLUE)} {GRAY}jump{RESET}",
        f"{pill('i', CYAN, BG_DARK)}/{pill('ESC', RED, BG_DARK)} {GRAY}close{RESET}",
    ]
    help_line = "   ".join(help_parts)
    out.append(center_text(help_line, cols, 50))

    return "".join(out)


def index_loop(fd, headings, current_heading_idx):
    """Run the index view. Returns word_idx to jump to, or -1 to cancel."""
    cursor = max(0, current_heading_idx)
    scroll_offset = 0
    num_buf = ""  # vim-style number prefix

    while True:
        ts = os.get_terminal_size()
        cols, rows = ts.columns, ts.lines
        avail = rows - 6

        # Keep cursor in scroll view
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + avail:
            scroll_offset = cursor - avail + 1

        sys.stdout.write(render_index(headings, cursor, scroll_offset, cols, rows, current_heading_idx))
        sys.stdout.flush()

        ch = get_key(fd)
        if ch.isdigit():
            num_buf += ch
            continue
        count = int(num_buf) if num_buf else 1
        num_buf = ""
        if ch in ("j", "DOWN"):
            cursor = min(cursor + count, len(headings) - 1)
        elif ch in ("k", "UP"):
            cursor = max(cursor - count, 0)
        elif ch == "\r":  # Enter
            return headings[cursor]["word_idx"]
        elif ch in ("i", "ESC", "\x03", "s"):
            return -1


# ── Input ─────────────────────────────────────────────────────────────────────

def get_key(fd):
    ch = os.read(fd, 1)
    if not ch:
        return ""
    if ch == b'\x1b':
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            seq = os.read(fd, 2).decode("utf-8", errors="ignore")
            if seq == "[C":
                return "RIGHT"
            elif seq == "[D":
                return "LEFT"
            elif seq == "[A":
                return "UP"
            elif seq == "[B":
                return "DOWN"
            return ""
        return "ESC"
    return ch.decode("utf-8", errors="ignore")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    words, headings, word_heading_idx = load_document(args.file)
    if not words:
        print("No words found in file.")
        sys.exit(1)

    has_headings = len(headings) > 0
    total = len(words)
    idx = min(args.start, total - 1)
    target_wpm = args.wpm
    paused = not args.no_pause
    minimal = False  # v toggles: hide everything except the word

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?25l")  # Hide cursor
        sys.stdout.flush()

        while idx < total:
            ts = os.get_terminal_size()
            cols, rows = ts.columns, ts.lines
            word = words[idx]
            orp = orp_index(word)
            current_wpm = get_wpm(target_wpm, args.min_wpm, args.ramp, idx, total)

            sys.stdout.write("\033[2J\033[H")
            mid_row = rows // 2

            if not minimal:
                # Breadcrumb (above everything)
                if has_headings:
                    chain = get_breadcrumb(headings, word_heading_idx, idx)
                    if chain:
                        sys.stdout.write(f"\033[{mid_row - 3};1H")
                        sys.stdout.write(render_breadcrumb(chain, cols))

                # Marker above
                sys.stdout.write(f"\033[{mid_row - 1};1H")
                sys.stdout.write(render_marker_above(cols))

            # Word (always shown)
            sys.stdout.write(f"\033[{mid_row};1H")
            sys.stdout.write(render_word(word, orp, cols))

            if not minimal:
                # Marker below
                sys.stdout.write(f"\033[{mid_row + 1};1H")
                sys.stdout.write(render_marker_below(cols))

                # Progress bars (stacked, no gaps; always reserve 3 rows)
                prog_row = mid_row + 3
                sys.stdout.write(f"\033[{prog_row};1H")
                sys.stdout.write(render_progress(idx + 1, total, target_wpm, current_wpm, cols))
                prog_row += 1

                if has_headings:
                    sec_lines = render_section_progress(idx, headings, word_heading_idx, total, cols)
                    parts = sec_lines.split("\r\n") if sec_lines else []
                    for i in range(2):
                        sys.stdout.write(f"\033[{prog_row};1H")
                        sys.stdout.write(parts[i] if i < len(parts) else "")
                        prog_row += 1

                # Help bar / pause indicator
                row_offset = prog_row + 1
                if paused:
                    sys.stdout.write(f"\033[{row_offset};1H")
                    pause_pill = pill("PAUSED", YELLOW, BG_DARK)
                    sys.stdout.write(center_text(pause_pill, cols, 8))
                    sys.stdout.write(f"\033[{row_offset + 2};1H")
                    sys.stdout.write(render_help_bar(cols, has_headings))
                else:
                    sys.stdout.write(f"\033[{rows - 1};1H")
                    sys.stdout.write(render_help_bar(cols, has_headings))

            sys.stdout.flush()

            if paused:
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
                    elif ch == "i" and has_headings:
                        cur_h = word_heading_idx[idx] if idx < len(word_heading_idx) else -1
                        jump = index_loop(fd, headings, cur_h)
                        if jump >= 0:
                            idx = min(jump, total - 1)
                        break
                    elif ch == "v":
                        minimal = not minimal
                        break
                continue

            # Playing
            delay = 60.0 / max(current_wpm, 1)
            if len(word) > 8:
                delay *= 1.3
            if word[-1] in ".!?":
                delay *= 1.5
            elif word[-1] in ",;:":
                delay *= 1.2

            deadline = time.monotonic() + delay

            while time.monotonic() < deadline:
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
                    elif ch == "i" and has_headings:
                        paused = True
                        cur_h = word_heading_idx[idx] if idx < len(word_heading_idx) else -1
                        jump = index_loop(fd, headings, cur_h)
                        if jump >= 0:
                            idx = min(jump, total - 1)
                        break
                    elif ch == "v":
                        minimal = not minimal

            if not paused:
                idx += 1

        # Done
        ts = os.get_terminal_size()
        cols, rows = ts.columns, ts.lines
        sys.stdout.write("\033[2J\033[H")
        mid_row = rows // 2
        done = pill(" DONE ", GREEN + BOLD, BG_DARK)
        sys.stdout.write(f"\033[{mid_row};1H")
        sys.stdout.write(center_text(done, cols, 6))
        sys.stdout.write(f"\033[{mid_row + 2};1H")
        sys.stdout.write(render_progress(total, total, target_wpm, target_wpm, cols))
        sys.stdout.flush()
        os.read(fd, 1)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\033[0m\033[2J\033[H")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
