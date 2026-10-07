#!/usr/bin/env bash
# ==============================================================================
# agy-pool uninstaller
# Removes agy-pool from ~/.local/bin/agy-pool
# ==============================================================================

set -e

TARGET_BIN="${HOME}/.local/bin/agy-pool"

if [ -f "${TARGET_BIN}" ]; then
    rm -f "${TARGET_BIN}"
    echo "✓ Successfully removed ${TARGET_BIN}"
else
    echo "agy-pool binary not found at ${TARGET_BIN}"
fi

echo "Note: Account credentials and history in ~/.gemini_accounts were preserved."
echo "If you wish to delete account data as well, run:"
echo "  rm -rf ~/.gemini_accounts"
