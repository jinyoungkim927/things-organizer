# Things 3 Auto-Organizer

Automatically organizes your Things 3 "Today" list whenever you make changes. Uses Claude to:

- **Rephrase** messy task names into clean, scannable titles (fixes typos, removes ALL CAPS, tightens wording)
- **Merge** duplicate or overlapping tasks (folds sub-tasks into parent notes)
- **Deduplicate** conservatively — only groups tasks that are genuinely about the same thing

## How it works

1. A macOS `launchd` agent watches the Things 3 SQLite database for changes
2. When triggered, the script reads your Today list and computes a **diff** against the previous state
3. **Only new or changed tasks** are sent to Claude — unchanged tasks are never re-processed
4. Uses **Haiku** (fast/cheap) for 1-2 task changes, **Sonnet** for larger batches
5. Writes back to Things via the **URL scheme** so changes appear live in the UI

## Requirements

- macOS with [Things 3](https://culturedcode.com/things/) installed
- Python 3.10+
- [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/things-organizer.git
cd things-organizer
chmod +x setup.sh
./setup.sh
```

The setup script will:
1. Create a Python virtual environment and install dependencies
2. Prompt for your Anthropic API key and Things auth token
3. Locate your Things database
4. Install and start the launchd watcher

### Getting your Things auth token

Open Things → **Settings** → **General** → **Enable Things URLs** → copy the auth token.

## Usage

The organizer runs automatically in the background. No manual intervention needed.

### Manual commands

```bash
# Preview what would change (no modifications)
.venv/bin/python organize_things.py --dry-run --force

# Force a run even if nothing changed
.venv/bin/python organize_things.py --force

# Watch logs
tail -f logs/launchd-stdout.log

# Stop the watcher
launchctl unload ~/Library/LaunchAgents/com.things-organizer.plist

# Restart the watcher
launchctl load ~/Library/LaunchAgents/com.things-organizer.plist
```

## Cost

Very cheap. Each trigger only processes the diff (new/changed tasks), not the full list.

- **1-2 new tasks**: uses Haiku (~$0.001 per call)
- **3+ new tasks**: uses Sonnet (~$0.01 per call)
- **No changes detected**: no API call at all

## How it avoids infinite loops

The script modifies Things, which modifies the database, which triggers the watcher. To prevent loops:

1. **Content hashing** — saves full task state after each run; skips if nothing meaningful changed
2. **30s cooldown** — ignores triggers within 30s of a completed run
3. **File lock** — prevents concurrent runs
4. **launchd ThrottleInterval** — macOS won't trigger more than once per 30s
