# spritz-cli

RSVP (Rapid Serial Visual Presentation) speed reader for the terminal. Displays text one word at a time with an optimal recognition point (ORP) highlighted in red, so your eyes never move — your brain just absorbs.

## Why

Reading a 60,000-word book at 250 WPM (normal reading) takes **4 hours**. At 500 WPM with RSVP, that's **2 hours**. At 800 WPM (achievable with practice), **1 hour 15 minutes**. You skip subvocalization and saccadic eye movement — the two biggest bottlenecks in traditional reading.

| Speed | 10k words | 60k words (book) | 120k words (long book) |
|-------|-----------|-------------------|------------------------|
| 250 WPM (normal) | 40 min | 4h 0m | 8h 0m |
| 500 WPM (default) | 20 min | 2h 0m | 4h 0m |
| 800 WPM | 12 min | 1h 15m | 2h 30m |

## Install

Requires Python 3.6+. No dependencies.

```bash
# Clone and alias
git clone <repo-url> ~/Projects/spritz-cli
echo 'alias spritz="python3 ~/Projects/spritz-cli/spritz.py"' >> ~/.bashrc
source ~/.bashrc
```

## Usage

```bash
spritz book.txt
spritz --wpm 600 article.md
spritz --wpm 800 --ramp 20 --no-pause novel.txt
spritz --start 5000 long-book.txt   # resume from word 5000
```

## Controls

| Key | Action |
|-----|--------|
| Space | Pause / resume (starts paused) |
| q | Decrease speed |
| e | Increase speed |
| ← → | Skip backward / forward |
| Ctrl+C | Quit |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--wpm` | 500 | Target words per minute |
| `--ramp` | 10 | Words to ramp up/down over |
| `--start` | 0 | Start from word N |
| `--step` | 50 | WPM change per keypress |
| `--min-wpm` | 100 | Minimum WPM (ramp floor) |
| `--no-pause` | off | Start playing immediately |

## How it works

Each word is positioned so its **Optimal Recognition Point** (roughly 1/3 into the word) is always at screen center, highlighted in red. Your eyes stay fixed on one point while words flash by. Speed ramps up gradually at the start and back down at the end. Longer words and sentences ending in punctuation get slightly more display time.

Accepts `.txt`, `.md`, and any plain text file. Markdown formatting is stripped automatically.
