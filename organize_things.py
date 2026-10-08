#!/usr/bin/env python3
"""
Things 3 Organizer
Triggered on database changes. Detects NEW or CHANGED tasks only,
uses Claude to rephrase/group/deduplicate them, updates Things via URL scheme.

Only calls the LLM for the diff — unchanged tasks are never re-processed.
"""

import subprocess
import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

import anthropic

# --- Config ---
SCRIPT_DIR = Path(__file__).parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"organize_{datetime.now().strftime('%Y-%m-%d')}.log"
LOCK_FILE = SCRIPT_DIR / ".lock"
STATE_FILE = SCRIPT_DIR / ".task_state.json"
COOLDOWN_SECONDS = 30
# Merging completes the absorbed tasks, and the model merges tasks that are only
# related ("reply to michelle" into "Reply to eddie morgan"). Off: merges are logged, not applied.
AUTO_MERGE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# --- Lock & State ---

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 300:
            return False
        log.warning(f"Stale lock ({age:.0f}s old), removing")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def check_cooldown() -> bool:
    if not STATE_FILE.exists():
        return False
    age = time.time() - STATE_FILE.stat().st_mtime
    return age < COOLDOWN_SECONDS


def load_state() -> dict:
    """Load previously seen task state: {task_id: {name, notes}}"""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(tasks: list[dict]):
    """Save current task state for future diff."""
    state = {t["id"]: {"name": t["name"], "notes": t["notes"]} for t in tasks}
    STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_diff(current_tasks: list[dict], prev_state: dict) -> dict:
    """
    Compare current tasks to previous state.
    Returns: {new: [...], changed: [...], unchanged: [...], removed_ids: [...]}
    """
    current_ids = {t["id"] for t in current_tasks}
    prev_ids = set(prev_state.keys())

    new_tasks = []
    changed_tasks = []
    unchanged_tasks = []

    for t in current_tasks:
        if t["id"] not in prev_state:
            new_tasks.append(t)
        elif (t["name"] != prev_state[t["id"]]["name"] or
              t["notes"] != prev_state[t["id"]]["notes"]):
            changed_tasks.append(t)
        else:
            unchanged_tasks.append(t)

    removed_ids = prev_ids - current_ids

    return {
        "new": new_tasks,
        "changed": changed_tasks,
        "unchanged": unchanged_tasks,
        "removed_ids": list(removed_ids),
    }


# --- AppleScript (read-only) ---

def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr}")
    return result.stdout.strip()


def get_today_tasks() -> list[dict]:
    script = '''
    tell application "Things3"
        set todoList to {}
        set todayToDos to to dos of list "Today"
        repeat with t in todayToDos
            set todoName to name of t
            set todoId to id of t
            set todoNotes to notes of t
            set end of todoList to todoId & "<<SEP>>" & todoName & "<<SEP>>" & todoNotes
        end repeat
        set AppleScript's text item delimiters to "<<ROW>>"
        return todoList as text
    end tell
    '''
    raw = run_applescript(script)
    if not raw:
        return []

    tasks = []
    for row in raw.split("<<ROW>>"):
        row = row.strip()
        if not row:
            continue
        parts = row.split("<<SEP>>", 2)
        if len(parts) >= 2:
            tasks.append({
                "id": parts[0].strip(),
                "name": parts[1].strip(),
                "notes": parts[2].strip() if len(parts) > 2 else "",
            })
    return tasks


# --- Things URL scheme (writes — triggers live UI refresh) ---

def things_url_update(task_id: str, **params):
    token = os.environ.get("THINGS_AUTH_TOKEN", "")
    url = f"things:///update?auth-token={quote(token)}&id={quote(task_id)}"
    for key, val in params.items():
        url += f"&{quote(key)}={quote(str(val))}"
    subprocess.run(["open", url], check=True, timeout=10)
    time.sleep(0.3)


def set_task_notes(task_id: str, notes: str):
    things_url_update(task_id, notes=notes)


def set_task_name(task_id: str, name: str):
    things_url_update(task_id, title=name)


def complete_task(task_id: str):
    things_url_update(task_id, completed="true")


# --- AI Analysis ---

def analyze_new_and_changed(new_tasks: list[dict], changed_tasks: list[dict],
                            all_tasks: list[dict]) -> dict:
    """
    Only send new/changed tasks to Claude for processing.
    Existing task names are provided as context for grouping only.
    """
    client = anthropic.Anthropic()

    targets = new_tasks + changed_tasks
    target_ids = {t["id"] for t in targets}
    existing = [t for t in all_tasks if t["id"] not in target_ids]

    # Build the prompt sections
    targets_text = ""
    for t in targets:
        notes_display = t['notes'] if t['notes'] else '(empty)'
        label = "[NEW]" if t in new_tasks else "[EDITED]"
        targets_text += f"- {label} ID: {t['id']}\n  Name: {t['name']}\n  Notes: {notes_display}\n\n"

    existing_text = ""
    for t in existing:
        notes_display = t['notes'] if t['notes'] else '(empty)'
        existing_text += f"- ID: {t['id']}\n  Name: {t['name']}\n  Notes: {notes_display}\n\n"

    prompt = f"""You are a sharp personal assistant organizing a TODO list in the Things 3 app.

## TASKS TO PROCESS (new or recently edited — these need your attention):

{targets_text}

## EXISTING TASKS (already organized — DO NOT rename these, only use for grouping context):

{existing_text}

You have TWO jobs, but ONLY for the tasks marked [NEW] or [EDITED].

## OVERRIDING RULE: LEAVE GIBBERISH ALONE
Before applying either job, ask: "Is this task name actually intelligible enough that I can tell what the user meant?"

If a task name is genuinely unclear — random characters, half-finished fragments, personal shorthand whose meaning isn't obvious, typed-on-phone keysmash, or any string where you'd have to *guess* at intent — DO NOT rephrase it and DO NOT merge it. Omit it from "renames" entirely and never list it as a child in "groups". Leave the original text exactly as the user wrote it so they can clarify it later themselves.

Better to leave a confusing task visible and untouched than to invent a meaning that wasn't there. When uncertain whether something is gibberish vs. just messy, default to leaving it alone.

Only proceed with the jobs below if you can confidently tell what the task means.

## JOB 1: REPHRASE task names (only for [NEW] and [EDITED] tasks)
Rewrite each new/edited task name to be concise, clear, and well-phrased:
- Fix typos and grammar
- Remove ALL CAPS — use normal sentence case
- Remove filler words, redundant phrases, rambling
- Keep the core intent and key details (names, deadlines, amounts)
- Make it scannable — a busy person should instantly understand what to do
- Keep it punchy and natural, not corporate-speak
- If a task name is already clean and concise, keep it as-is
- If you can't confidently tell what the task means (see overriding rule above), leave it alone

## JOB 2: CHECK FOR GROUPING (merge new tasks into existing if they overlap)
- Check if any [NEW]/[EDITED] task clearly overlaps with or duplicates an EXISTING task
- If so, merge the new task INTO the existing one (existing becomes parent, new becomes child)
- Two new tasks can also be merged with each other if they're clearly the same thing
- Be VERY CONSERVATIVE — only merge if a reasonable person would say "these are the same thing"
- Preserve ALL notes content when merging
- When in doubt, LEAVE SEPARATE

Respond with a JSON object (no markdown fencing):
{{
    "groups": [
        {{
            "parent_id": "ID of the task to keep (can be existing or new)",
            "parent_new_name": "new name for parent (only if parent is [NEW]/[EDITED], otherwise null)",
            "child_ids": ["IDs of tasks to merge into parent"],
            "new_notes": "merged notes — ALL content from both tasks preserved",
            "reasoning": "why these are related"
        }}
    ],
    "renames": [
        {{
            "id": "task ID (only [NEW] or [EDITED] tasks)",
            "new_name": "rephrased name"
        }}
    ],
    "summary": "one sentence describing what you did"
}}

"renames" should only include [NEW]/[EDITED] tasks that are NOT being merged as children. If a task is being merged (appears in child_ids), don't include it in renames."""

    # Use Haiku for small diffs (1-2 tasks), Sonnet for larger ones
    if len(targets) <= 2:
        model, extra = "claude-haiku-4-5-20251001", {}
    else:
        # Sonnet 5.5 thinks by default; low effort skips it on simple requests like this one
        model, extra = "claude-sonnet-5-5", {"output_config": {"effort": "low"}}
    log.info(f"Using {model} for {len(targets)} task(s) to process")

    response = client.messages.create(
        model=model,
        max_tokens=16000,  # room for any thinking plus the JSON reply
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)  # not typed in anthropic 0.84
        category = getattr(details, "category", None) if details else None
        raise RuntimeError(f"{model} declined the request (category: {category})")

    # The response may start with a thinking block, so take the text block
    text = next(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    return json.loads(text)


# --- Apply ---

def apply_changes(all_tasks: list[dict], plan: dict, dry_run: bool = False):
    task_map = {t["id"]: t for t in all_tasks}

    for group in plan.get("groups", []):
        parent_id = group["parent_id"]
        child_ids = group.get("child_ids", [])
        new_notes = group.get("new_notes", "")
        new_name = group.get("parent_new_name")

        if parent_id not in task_map:
            log.warning(f"Parent {parent_id} not found, skipping")
            continue

        parent = task_map[parent_id]
        children = [task_map[cid] for cid in child_ids if cid in task_map and cid != parent_id]
        if not children:
            continue

        child_names = [c["name"] for c in children]
        if not AUTO_MERGE:
            log.info(f"SUGGESTED MERGE (not applied): '{parent['name']}' + {child_names}")
            log.info(f"  Reason: {group.get('reasoning', 'N/A')}")
            continue

        log.info(f"MERGE: '{parent['name']}' absorbs {child_names}")
        log.info(f"  Reason: {group.get('reasoning', 'N/A')}")

        if dry_run:
            log.info("  [DRY RUN]")
            continue

        if new_notes:
            set_task_notes(parent_id, new_notes)
            log.info(f"  Updated notes")

        if new_name:
            set_task_name(parent_id, new_name)
            log.info(f"  Renamed to '{new_name}'")

        for child in children:
            complete_task(child["id"])
            log.info(f"  Completed (merged): '{child['name']}'")

    for rename in plan.get("renames", []):
        rid = rename["id"]
        new_name = rename.get("new_name")
        if not new_name or rid not in task_map:
            continue
        old_name = task_map[rid]["name"]
        if old_name == new_name:
            continue
        log.info(f"RENAME: '{old_name}' → '{new_name}'")
        if not dry_run:
            set_task_name(rid, new_name)

    groups = len(plan.get("groups", [])) if AUTO_MERGE else 0
    renames = len(plan.get("renames", []))
    log.info(f"Applied {groups} merges, {renames} renames")


# --- Main ---

def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    if not force and not dry_run and check_cooldown():
        # Don't even log — this fires very frequently
        return

    if not force and not dry_run and not acquire_lock():
        return

    try:
        log.info("=" * 50)
        log.info(f"Things Organizer — {datetime.now().strftime('%H:%M:%S')}")
        if dry_run:
            log.info("*** DRY RUN ***")
        log.info("=" * 50)

        # 1. Read today's tasks
        tasks = get_today_tasks()
        log.info(f"Today: {len(tasks)} tasks")

        if len(tasks) < 1:
            log.info("No tasks — done")
            return

        # 2. Compute diff against previous state
        prev_state = load_state()
        diff = compute_diff(tasks, prev_state)

        new_count = len(diff["new"])
        changed_count = len(diff["changed"])
        removed_count = len(diff["removed_ids"])

        log.info(f"Diff: {new_count} new, {changed_count} changed, "
                 f"{len(diff['unchanged'])} unchanged, {removed_count} removed")

        # If nothing new or changed, just update state and exit
        if new_count == 0 and changed_count == 0:
            if removed_count > 0:
                # Tasks were removed/completed — update state but no LLM needed
                save_state(tasks)
                log.info("Only removals — state updated, no LLM call needed")
            else:
                log.info("No changes detected — skipping")
            return

        for t in diff["new"]:
            log.info(f"  [NEW] {t['name']}")
        for t in diff["changed"]:
            log.info(f"  [EDITED] {t['name']}")

        # 3. Analyze ONLY new/changed tasks (with existing as context)
        log.info("Calling Claude for new/changed tasks only...")
        try:
            plan = analyze_new_and_changed(diff["new"], diff["changed"], tasks)
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse response: {e}")
            return
        except Exception as e:
            log.error(f"API error: {e}")
            return

        log.info(f"Summary: {plan.get('summary', 'N/A')}")

        # 4. Apply
        apply_changes(tasks, plan, dry_run=dry_run)

        # 5. Save new state AFTER changes are applied
        if not dry_run:
            time.sleep(2)
            new_tasks = get_today_tasks()
            save_state(new_tasks)

        log.info("Done!")

    finally:
        if not dry_run:
            release_lock()


if __name__ == "__main__":
    main()
