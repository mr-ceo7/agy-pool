#!/usr/bin/env bash
# ==============================================================================
# agy-pool installer
# Installs agy-pool into ~/.local/bin/agy-pool
# ==============================================================================

set -e

REPO_RAW_URL="https://raw.githubusercontent.com/mr-ceo7/agy-pool/main"
TARGET_DIR="${HOME}/.local/bin"
TARGET_BIN="${TARGET_DIR}/agy-pool"

echo "=== Installing agy-pool ==="

# Check Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required but not installed." >&2
    exit 1
fi

# Ensure ~/.local/bin exists
mkdir -p "${TARGET_DIR}"

# If running from local repo clone, copy; otherwise download
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -f "${SCRIPT_DIR}/agy-pool" ]; then
    echo "Installing from local repository..."
    cp -f "${SCRIPT_DIR}/agy-pool" "${TARGET_BIN}"
else
    echo "Downloading agy-pool from GitHub..."
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "${REPO_RAW_URL}/agy-pool" -o "${TARGET_BIN}"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "${TARGET_BIN}" "${REPO_RAW_URL}/agy-pool"
    else
        echo "Error: Neither curl nor wget was found." >&2
        exit 1
    fi
fi

chmod +x "${TARGET_BIN}"

# Create convenience symlinks for other CLI pools
ln -sf "${TARGET_BIN}" "${TARGET_DIR}/claude-pool"
ln -sf "${TARGET_BIN}" "${TARGET_DIR}/copilot-pool"

echo "✓ agy-pool successfully installed to ${TARGET_BIN}"
echo "✓ Created convenience aliases: claude-pool, copilot-pool"

# Check PATH
case ":$PATH:" in
    *":${TARGET_DIR}:"*) ;;
    *)
        echo ""
        echo "Note: ${TARGET_DIR} is not in your \$PATH."
        echo "Add it to your shell configuration (~/.bashrc or ~/.zshrc):"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Get started:"
echo "  agy-pool import primary   # Import current active account"
echo "  agy-pool add backup       # Add and authenticate another account"
echo "  agy-pool status           # View account pool status"
echo "  agy-pool                  # Launch interactive session"
echo ""
