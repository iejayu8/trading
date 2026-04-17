#!/usr/bin/env python3
"""
Bump the patch version in trading-bot/config.json and prepend a new
changelog entry in trading-bot/CHANGELOG.md.

Collect all non-merge commits since the previous version-bump commit (or
the last 30 commits when no prior bump exists) and use them as bullet
points for the new entry.
"""

import json
import os
import re
import subprocess
import sys

CONFIG_PATH = "trading-bot/config.json"
CHANGELOG_PATH = "trading-bot/CHANGELOG.md"
BUMP_MARKER = "chore: bump version"


def git(*args):
    result = subprocess.run(
        ["git"] + list(args), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ── 1. Read and bump version ──────────────────────────────────────────────────

with open(CONFIG_PATH) as f:
    config_text = f.read()

config = json.loads(config_text)
current = config["version"]
major, minor, patch = map(int, current.split("."))
new_version = f"{major}.{minor}.{patch + 1}"

# Update version field in-place (preserves file formatting)
config_text = re.sub(
    r'("version"\s*:\s*")[^"]+(")',
    rf"\g<1>{new_version}\g<2>",
    config_text,
)

# ── 2. Collect commits since last bump ────────────────────────────────────────

# Find the SHA of the most recent version-bump commit
last_bump_result = subprocess.run(
    ["git", "log", "--format=%H", f"--grep={BUMP_MARKER}", "-1"],
    capture_output=True, text=True,
)
last_bump_sha = last_bump_result.stdout.strip()

if last_bump_sha:
    raw = git("log", f"{last_bump_sha}..HEAD", "--format=%s", "--no-merges")
else:
    raw = git("log", "-30", "--format=%s", "--no-merges")

subjects = [
    line for line in raw.splitlines()
    if line and not line.startswith(BUMP_MARKER)
]

if not subjects:
    # Fallback: use the HEAD commit subject
    subjects = [git("log", "-1", "--format=%s")]

bullet_lines = "\n".join(f"- {s}" for s in subjects)
changelog_entry = f"## {new_version}\n{bullet_lines}\n"

# ── 3. Prepend entry to CHANGELOG.md ─────────────────────────────────────────

with open(CHANGELOG_PATH) as f:
    existing = f.read()

# Insert after the "# Changelog" header line
header_end = existing.index("\n") + 1
new_changelog = (
    existing[:header_end]
    + "\n"
    + changelog_entry
    + "\n"
    + existing[header_end:].lstrip("\n")
)

# ── 4. Write files ────────────────────────────────────────────────────────────

with open(CONFIG_PATH, "w") as f:
    f.write(config_text)

with open(CHANGELOG_PATH, "w") as f:
    f.write(new_changelog)

# ── 5. Export new version for the workflow step ───────────────────────────────

github_output = os.environ.get("GITHUB_OUTPUT", "")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"new_version={new_version}\n")

print(f"✔  {current}  →  {new_version}")
print(f"   {len(subjects)} commit(s) added to changelog")
