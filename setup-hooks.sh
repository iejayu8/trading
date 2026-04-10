#!/bin/sh
# One-time setup: tell git to use the project's tracked hooks directory.
# Run this once after cloning the repository:
#
#   sh setup-hooks.sh
#
# What it does
# ------------
# Sets core.hooksPath to .githooks so that the pre-commit hook runs
# automatically on every `git commit`.
#
# The pre-commit hook mirrors backend/ and frontend/ into
# trading-bot/backend/ and trading-bot/frontend/ so the Home Assistant
# add-on always ships the same code as the root source-of-truth folders.

set -e

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

git -C "$REPO_ROOT" config core.hooksPath .githooks
echo "Git hooks configured. The pre-commit hook will now keep trading-bot/ in sync with backend/ and frontend/."
