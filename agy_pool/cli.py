#!/usr/bin/env python3
"""
agy-pool: Multi-account rotation, quota failover, and unified conversation management
for Google Antigravity CLI (agy).

Executes agy in your current workspace with the active, healthy account from your rotation pool.
Automatically falls over to ready accounts upon hitting quota limits (429 / RESOURCE_EXHAUSTED).
"""

import os
import sys
import json
import time
import datetime
import threading
import subprocess
import shutil
import re
import fcntl
import base64
import urllib.request
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

VERSION = "1.0.0"
UPDATE_URL = "https://raw.githubusercontent.com/mr-ceo7/agy-pool/main/agy-pool"

DEFAULT_ACCOUNTS_DIR = os.getenv(
    "AGY_ACCOUNTS_DIR",
    os.path.join(os.path.expanduser("~"), ".gemini_accounts")
)

# ---------------------------------------------------------------------------
# Account Pool Manager
# ---------------------------------------------------------------------------

class AccountPool:
    def __init__(self, base_dir: str = DEFAULT_ACCOUNTS_DIR, cooldown_seconds: int = 3600):
        self.base_dir = os.path.abspath(base_dir)
        self.accounts_dir = os.path.join(self.base_dir, "accounts")
        self.metadata_file = os.path.join(self.base_dir, "accounts.json")
        self.cooldown_seconds = cooldown_seconds
        self.lock = threading.Lock()

        os.makedirs(self.accounts_dir, exist_ok=True)
        self._ensure_metadata_initialized()

    @contextmanager
    def _file_lock(self):
        lock_path = self.metadata_file + ".lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(lock_fd)
            except Exception:
                pass

    def _ensure_metadata_initialized(self):
        with self.lock:
            with self._file_lock():
                if not os.path.exists(self.metadata_file):
                    initial_data = {
                        "accounts": [],
                        "active_index": 0,
                        "cooldown_seconds": self.cooldown_seconds
                    }
                    with open(self.metadata_file, "w", encoding="utf-8") as f:
                        json.dump(initial_data, f, indent=2)

    def _load_data(self) -> dict:
        if not os.path.exists(self.metadata_file):
            return {"accounts": [], "active_index": 0, "cooldown_seconds": self.cooldown_seconds}
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for acc in data.get("accounts", []):
                    acc["home_dir"] = os.path.join(self.accounts_dir, acc["id"])
                return data
        except Exception as e:
            print(f"[agy-pool] Error reading pool metadata: {e}", file=sys.stderr)
            return {"accounts": [], "active_index": 0, "cooldown_seconds": self.cooldown_seconds}

    def _save_data(self, data: dict):
        try:
            tmp_path = self.metadata_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.metadata_file)
        except Exception as e:
            print(f"[agy-pool] Error saving pool metadata: {e}", file=sys.stderr)

    def list_accounts(self) -> List[dict]:
        with self.lock:
            with self._file_lock():
                data = self._load_data()
                now = time.time()
                for acc in data.get("accounts", []):
                    cooldown = acc.get("cooldown_until")
                    if cooldown and now >= cooldown:
                        acc["cooldown_until"] = None
                        acc["status"] = "ready"
                return data.get("accounts", [])

    def get_active_account(self) -> Optional[dict]:
        with self.lock:
            with self._file_lock():
                data = self._load_data()
                accounts = data.get("accounts", [])
                if not accounts:
                    return None

                now = time.time()
                total = len(accounts)
                start_index = data.get("active_index", 0) % total

                for i in range(total):
                    idx = (start_index + i) % total
                    acc = accounts[idx]
                    cooldown = acc.get("cooldown_until")

                    if cooldown and now >= cooldown:
                        acc["cooldown_until"] = None
                        acc["status"] = "ready"

                    if not acc.get("cooldown_until") and acc.get("status") != "disabled":
                        if idx != start_index:
                            data["active_index"] = idx
                            self._save_data(data)
                            print(f"[agy-pool] Switched active account to: '{acc['id']}'", file=sys.stderr)
                        return acc

                sorted_by_cooldown = sorted(
                    accounts,
                    key=lambda a: a.get("cooldown_until") or float("inf")
                )
                earliest = sorted_by_cooldown[0]
                print(f"[agy-pool] Warning: All accounts are in quota cooldown. Using earliest: '{earliest['id']}'", file=sys.stderr)
                return earliest

    def mark_quota_exhausted(self, account_id: str, error_msg: str = "") -> Optional[dict]:
        with self.lock:
            with self._file_lock():
                data = self._load_data()
                accounts = data.get("accounts", [])
                if not accounts:
                    return None

                now = time.time()
                cooldown_period = data.get("cooldown_seconds", self.cooldown_seconds)
                cooldown_until = now + cooldown_period

                target_idx = None
                for idx, acc in enumerate(accounts):
                    if acc.get("id") == account_id:
                        acc["status"] = "quota_exhausted"
                        acc["cooldown_until"] = cooldown_until
                        acc["last_exhausted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        acc["fail_count"] = acc.get("fail_count", 0) + 1
                        target_idx = idx
                        print(f"[agy-pool] Account '{account_id}' quota exhausted (cooling down for {cooldown_period // 60}m)", file=sys.stderr)
                        break

                total = len(accounts)
                next_acc = None
                if target_idx is not None:
                    for i in range(1, total + 1):
                        cand_idx = (target_idx + i) % total
                        cand = accounts[cand_idx]
                        cd = cand.get("cooldown_until")
                        if cd and now >= cd:
                            cand["cooldown_until"] = None
                            cand["status"] = "ready"
                        if not cand.get("cooldown_until") and cand.get("status") != "disabled":
                            data["active_index"] = cand_idx
                            next_acc = cand
                            print(f"[agy-pool] Failover successful! New active account: '{cand['id']}'", file=sys.stderr)
                            break

                self._save_data(data)
                return next_acc

    def record_success(self, account_id: str):
        with self.lock:
            with self._file_lock():
                data = self._load_data()
                for acc in data.get("accounts", []):
                    if acc.get("id") == account_id:
                        acc["success_count"] = acc.get("success_count", 0) + 1
                        acc["last_used_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        break
                self._save_data(data)

    def add_account_from_dir(self, account_id: str, source_dir: str, tool_type: str = "agy", email: str = "") -> dict:
        clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", account_id).strip("_")
        account_home = os.path.join(self.accounts_dir, clean_id)
        os.makedirs(account_home, exist_ok=True)

        if tool_type == "agy":
            target_gemini = os.path.join(account_home, ".gemini", "antigravity-cli")
            os.makedirs(target_gemini, exist_ok=True)
            for fname in ["antigravity-oauth-token", "settings.json", "installation_id", "antigravity_state.pbtxt"]:
                src = os.path.join(source_dir, fname)
                if os.path.isfile(src):
                    dst = os.path.join(target_gemini, fname)
                    shutil.copy2(src, dst)

            if not email:
                tok_path = os.path.join(target_gemini, "antigravity-oauth-token")
                if os.path.isfile(tok_path):
                    try:
                        with open(tok_path, "r", encoding="utf-8") as f:
                            tok_data = json.load(f)
                            id_tok = tok_data.get("id_token")
                            if id_tok and id_tok.count(".") == 2:
                                payload = id_tok.split(".")[1]
                                payload += "=" * (-len(payload) % 4)
                                claims = json.loads(base64.b64decode(payload).decode("utf-8", errors="ignore"))
                                email = claims.get("email", "")
                    except Exception:
                        pass
        elif tool_type == "claude":
            for fname in [".credentials.json", ".claude.json", "settings.json"]:
                src = os.path.join(source_dir, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(account_home, fname))
            if not email:
                email = f"{clean_id}@claude"
        elif tool_type == "copilot":
            for fname in ["hosts.yml", "config.json"]:
                src = os.path.join(source_dir, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(account_home, fname))
            if not email:
                email = f"{clean_id}@copilot"

        with self.lock:
            with self._file_lock():
                data = self._load_data()
                accounts = data.get("accounts", [])
                existing = next((a for a in accounts if a["id"] == clean_id), None)
                record = {
                    "id": clean_id,
                    "email": email or clean_id,
                    "home_dir": account_home,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "status": "ready",
                    "cooldown_until": None,
                    "success_count": 0,
                    "fail_count": 0
                }
                if existing:
                    existing.update(record)
                else:
                    accounts.append(record)

                data["accounts"] = accounts
                self._save_data(data)
                return record

    def remove_account(self, account_id: str) -> bool:
        with self.lock:
            with self._file_lock():
                data = self._load_data()
                accounts = data.get("accounts", [])
                new_accounts = [a for a in accounts if a["id"] != account_id]
                if len(new_accounts) == len(accounts):
                    return False
                data["accounts"] = new_accounts
                if data.get("active_index", 0) >= len(new_accounts):
                    data["active_index"] = 0
                self._save_data(data)

                # Clean up disk files
                target_home = os.path.join(self.accounts_dir, account_id)
                if os.path.isdir(target_home):
                    shutil.rmtree(target_home, ignore_errors=True)
                return True

# ---------------------------------------------------------------------------
# Quota Error Detection
# ---------------------------------------------------------------------------

def is_quota_error(text: str, tool_id: str = "agy") -> bool:
    if not text:
        return False
    low = text.lower()
    if tool_id == "claude":
        patterns = [
            r"429",
            r"rate_limit",
            r"usage_limit_reached",
            r"weekly limit",
            r"5-hour limit",
            r"limit.*resets",
            r"out_of_credits",
            r"overloaded_error",
        ]
    else:
        patterns = [
            r"429",
            r"resource_exhausted",
            r"quota.*exceeded",
            r"rate.*limit",
            r"exhausted.*quota",
            r"too many requests",
            r"resource has been exhausted",
            r"exceeded your current quota",
        ]
    return any(re.search(p, low) for p in patterns)

def ensure_claude_symlinks(account_home: str):
    """
    Auto-links shared projects, session histories, and plugins from ~/.claude
    into isolated Claude account directories so all sessions resume transparently.
    """
    real_claude = os.path.expanduser("~/.claude")
    if not account_home or os.path.realpath(account_home) == os.path.realpath(real_claude):
        return

    os.makedirs(account_home, exist_ok=True)

    # 1. Shared projects
    real_proj = os.path.join(real_claude, "projects")
    acc_proj = os.path.join(account_home, "projects")
    if os.path.isdir(real_proj):
        os.makedirs(acc_proj, exist_ok=True)
        for proj in os.listdir(real_proj):
            src = os.path.join(real_proj, proj)
            dst = os.path.join(acc_proj, proj)
            if not os.path.exists(dst) and not os.path.islink(dst):
                try:
                    os.symlink(src, dst)
                except Exception:
                    pass

    # 2. Shared file history
    real_fh = os.path.join(real_claude, "file-history")
    acc_fh = os.path.join(account_home, "file-history")
    if os.path.isdir(real_fh):
        os.makedirs(acc_fh, exist_ok=True)
        for fh in os.listdir(real_fh):
            src = os.path.join(real_fh, fh)
            dst = os.path.join(acc_fh, fh)
            if not os.path.exists(dst) and not os.path.islink(dst):
                try:
                    os.symlink(src, dst)
                except Exception:
                    pass

    # 3. Shared plugins, skills, sessions
    for item in ["plugins", "skills", "session-env", "sessions", "shell-snapshots"]:
        src = os.path.join(real_claude, item)
        dst = os.path.join(account_home, item)
        if os.path.exists(src) and not os.path.exists(dst) and not os.path.islink(dst):
            try:
                os.symlink(src, dst)
            except Exception:
                pass

def auto_import_claude_accounts(pool: AccountPool):
    """Auto-imports ~/.claude as primary and ~/.claude2 as secondary if pool is empty."""
    if pool.list_accounts():
        return

    primary_dir = os.path.expanduser("~/.claude")
    secondary_dir = os.path.expanduser("~/.claude2")

    if os.path.isdir(primary_dir):
        pool.add_account_from_dir("primary", primary_dir, tool_type="claude", email="primary-account")
        print("[claude-pool] Auto-imported existing ~/.claude as 'primary'.", file=sys.stderr)

    if os.path.isdir(secondary_dir):
        pool.add_account_from_dir("claude2", secondary_dir, tool_type="claude", email="claude2-account")
        print("[claude-pool] Auto-imported existing ~/.claude2 as 'claude2'.", file=sys.stderr)

# ---------------------------------------------------------------------------
# Self-Healing Symlinks & Shared Conversation Management
# ---------------------------------------------------------------------------

def ensure_account_symlinks(account_home: str):
    """
    Guarantees global configs, SSH keys, skills, plugins, and unified conversations
    are accessible inside the isolated account home without token contamination.
    """
    real_home = os.path.expanduser("~")
    if not account_home or account_home == real_home:
        return

    # 1. Global tools and configurations
    for item in [".gitconfig", ".ssh"]:
        src = os.path.join(real_home, item)
        dst = os.path.join(account_home, item)
        if os.path.exists(src) and not os.path.exists(dst) and not os.path.islink(dst):
            try:
                os.symlink(src, dst)
            except Exception:
                pass

    # 2. Shared plugins, skills, and prompts
    gem_src = os.path.join(real_home, ".gemini", "config")
    gem_dst = os.path.join(account_home, ".gemini", "config")
    if os.path.exists(gem_src) and not os.path.exists(gem_dst) and not os.path.islink(gem_dst):
        try:
            os.symlink(gem_src, gem_dst)
        except Exception:
            pass

    # 3. Unified conversation history, transcripts, and command line cache
    central_cli = os.path.join(real_home, ".gemini", "antigravity-cli")
    acc_cli = os.path.join(account_home, ".gemini", "antigravity-cli")
    os.makedirs(acc_cli, exist_ok=True)

    shared_items = ["brain", "conversations", "conversation_summaries.db", "history.jsonl", "cache"]
    for item in shared_items:
        src = os.path.join(central_cli, item)
        dst = os.path.join(acc_cli, item)

        if not os.path.exists(src):
            if "." in item:
                open(src, "w").close()
            else:
                os.makedirs(src, exist_ok=True)

        if not os.path.exists(dst) and not os.path.islink(dst):
            try:
                os.symlink(src, dst)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Subcommands: Account Management
# ---------------------------------------------------------------------------

def cmd_status(pool: AccountPool, args: List[str], tool_name: str = "AGY"):
    accounts = pool.list_accounts()
    active = pool.get_active_account()
    active_id = active.get("id") if active else None

    print("\n" + "=" * 68)
    print(f"  {tool_name.upper()} ACCOUNT ROTATION POOL")
    print("=" * 68)

    if not accounts:
        print("  No accounts registered yet.")
        prefix = f"agy-pool {tool_name.lower()}" if tool_name.upper() != "AGY" else "agy-pool"
        print(f"  Import current: {prefix} import [name]")
        print(f"  Add new:        {prefix} add <name>")
        print("=" * 68 + "\n")
        return

    now = time.time()
    for idx, acc in enumerate(accounts, 1):
        is_active = (acc["id"] == active_id)
        marker = "★ [ACTIVE]" if is_active else "  [READY] "
        status_str = acc.get("status", "ready").upper()
        cd = acc.get("cooldown_until")
        if cd and now < cd:
            mins_left = int((cd - now) // 60)
            status_str = f"COOLDOWN ({mins_left}m left)"
            marker = "⏳ [COOLDOWN]"

        print(f"\n{marker} #{idx}: {acc['id']}")
        print(f"   Email:    {acc.get('email', 'N/A')}")
        print(f"   Status:   {status_str}")
        print(f"   Requests: {acc.get('success_count', 0)} succeeded, {acc.get('fail_count', 0)} failed")
        print(f"   Home Dir: {acc['home_dir']}")

    print("\n" + "=" * 68 + "\n")

def cmd_switch(pool: AccountPool, args: List[str]):
    accounts = pool.list_accounts()
    if not accounts:
        print("No accounts in pool.", file=sys.stderr)
        return

    target = args[0] if args else None
    data = pool._load_data()
    accs = data.get("accounts", [])
    if target:
        idx = next((i for i, a in enumerate(accs) if a["id"] == target), None)
        if idx is None:
            print(f"Error: Account '{target}' not found in pool.", file=sys.stderr)
            return
        data["active_index"] = idx
    else:
        data["active_index"] = (data.get("active_index", 0) + 1) % len(accs)

    pool._save_data(data)
    new_active = accs[data["active_index"]]
    print(f"✓ Switched active account to: '{new_active['id']}' ({new_active.get('email')})")

def cmd_import_current(pool: AccountPool, args: List[str]):
    acc_name = args[0] if args else "primary"
    local_gemini = os.path.expanduser("~/.gemini/antigravity-cli")
    token_file = os.path.join(local_gemini, "antigravity-oauth-token")

    if not os.path.isfile(token_file):
        print(f"Error: No token found at {token_file}. Please run 'agy' first to log in.", file=sys.stderr)
        sys.exit(1)

    record = pool.add_account_from_dir(acc_name, local_gemini)
    ensure_account_symlinks(record["home_dir"])
    print(f"✓ Imported current local account into pool as '{record['id']}'")
    print(f"  Email: {record.get('email')}")
    print(f"  Isolated Home: {record['home_dir']}")

def cmd_import_keyring(pool: AccountPool, args: List[str]):
    acc_name = args[0] if args else "keyring_account"
    try:
        out = subprocess.check_output(
            ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        data = json.loads(out)
    except Exception as e:
        print(f"Error: Failed to lookup token in system keyring: {e}", file=sys.stderr)
        sys.exit(1)

    temp_home = f"/tmp/agy_keyring_{acc_name}_{int(time.time())}"
    temp_gem = os.path.join(temp_home, ".gemini", "antigravity-cli")
    os.makedirs(temp_gem, exist_ok=True)
    with open(os.path.join(temp_gem, "antigravity-oauth-token"), "w", encoding="utf-8") as f:
        json.dump(data, f)

    record = pool.add_account_from_dir(acc_name, temp_gem)
    shutil.rmtree(temp_home, ignore_errors=True)
    ensure_account_symlinks(record["home_dir"])
    print(f"✓ Imported keyring account into pool as '{record['id']}'")
    print(f"  Email: {record.get('email')}")
    print(f"  Isolated Home: {record['home_dir']}")

def cmd_add(pool: AccountPool, args: List[str]):
    if not args:
        print("Usage: agy-pool add <account_name>", file=sys.stderr)
        sys.exit(1)

    acc_name = args[0]
    temp_home = f"/tmp/agy_login_{acc_name}_{int(time.time())}"
    temp_gemini = os.path.join(temp_home, ".gemini", "antigravity-cli")
    os.makedirs(temp_gemini, exist_ok=True)

    print(f"\n[LOGIN] Launching isolated agy authentication for '{acc_name}'...")
    print("1. Click the Google OAuth URL that appears below.")
    print("2. Choose or log into your Google Account.")
    print("3. Copy the authorization code and paste it back here.\n")

    env = os.environ.copy()
    env["HOME"] = temp_home
    # Emulate headless session during login so agy outputs the OAuth URL in terminal
    env["SSH_CONNECTION"] = "127.0.0.1 12345 127.0.0.1 22"
    env["SSH_CLIENT"] = "127.0.0.1 12345 22"

    try:
        subprocess.run(["agy", "-p", "Hello", "--dangerously-skip-permissions"], env=env, check=False)
    except Exception as e:
        print(f"Error launching agy: {e}", file=sys.stderr)
        shutil.rmtree(temp_home, ignore_errors=True)
        sys.exit(1)

    token_path = os.path.join(temp_gemini, "antigravity-oauth-token")
    if not os.path.isfile(token_path):
        print("\n[FAILED] Login was not completed. No oauth token found.", file=sys.stderr)
        shutil.rmtree(temp_home, ignore_errors=True)
        sys.exit(1)

    record = pool.add_account_from_dir(acc_name, temp_gemini)
    shutil.rmtree(temp_home, ignore_errors=True)
    ensure_account_symlinks(record["home_dir"])

    print(f"\n✓ Successfully registered account '{record['id']}' into pool!")
    print(f"  Email: {record.get('email')}")
    print(f"  Isolated Home: {record['home_dir']}")

def cmd_test(pool: AccountPool, args: List[str], tool_bin: str = "agy", tool_id: str = "agy"):
    target_name = args[0] if args else None
    accounts = pool.list_accounts()

    if target_name:
        accounts = [a for a in accounts if a["id"] == target_name]
        if not accounts:
            print(f"Account '{target_name}' not found in pool.", file=sys.stderr)
            sys.exit(1)

    if not accounts:
        print("No accounts to test.", file=sys.stderr)
        sys.exit(1)

    print(f"\n--- Testing Accounts in Pool ({tool_id.upper()}) ---")
    for acc in accounts:
        acc_id = acc["id"]
        acc_home = acc["home_dir"]
        env = os.environ.copy()
        if tool_id == "claude":
            ensure_claude_symlinks(acc_home)
            env["CLAUDE_CONFIG_DIR"] = acc_home
        elif tool_id == "copilot":
            env["XDG_CONFIG_HOME"] = acc_home
        else:
            ensure_account_symlinks(acc_home)
            env["HOME"] = acc_home
            env["DBUS_SESSION_BUS_ADDRESS"] = "disabled"
            env.pop("SSH_CONNECTION", None)
            env.pop("SSH_CLIENT", None)

        print(f"\nTesting '{acc_id}' ({acc.get('email', 'N/A')})...")

        start = time.time()
        res = subprocess.run(
            [tool_bin, "-p", "Respond with PONG"],
            env=env,
            capture_output=True,
            text=True,
            timeout=35
        )
        elapsed = time.time() - start

        if res.returncode == 0:
            snippet = res.stdout.strip().replace("\n", " ")[:60]
            print(f"  ✓ SUCCESS ({elapsed:.1f}s): {snippet}")
            pool.record_success(acc_id)
        else:
            err_snippet = res.stderr.strip()[:100]
            print(f"  ✗ FAILED ({elapsed:.1f}s): Exit {res.returncode}, {err_snippet}")

    print("\nTest completed.\n")

def cmd_remove(pool: AccountPool, args: List[str]):
    if not args:
        print("Usage: agy-pool remove <account_name>", file=sys.stderr)
        sys.exit(1)
    acc_name = args[0]
    if pool.remove_account(acc_name):
        print(f"✓ Removed account '{acc_name}' from pool.")
    else:
        print(f"Account '{acc_name}' not found.", file=sys.stderr)

def get_remote_version() -> Tuple[Optional[str], Optional[str]]:
    """Fetch remote content and parsed version string."""
    try:
        req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"agy-pool/{VERSION}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("utf-8")
        match = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1), content
    except Exception:
        pass
    return None, None

def check_for_updates_background(pool_base_dir: str):
    """Check for updates every 24 hours non-blockingly."""
    cache_file = os.path.join(pool_base_dir, ".update_check.json")
    now = time.time()
    try:
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                data = json.load(f)
            if now - data.get("timestamp", 0) < 86400:
                remote_ver = data.get("remote_version")
                if remote_ver and remote_ver != VERSION:
                    print(f"[agy-pool] Notice: Update available (v{VERSION} -> v{remote_ver}). Run 'agy-pool update' to upgrade.", file=sys.stderr)
                return
    except Exception:
        pass

    def _worker():
        r_ver, _ = get_remote_version()
        if r_ver:
            try:
                with open(cache_file, "w") as f:
                    json.dump({"timestamp": time.time(), "remote_version": r_ver}, f)
            except Exception:
                pass
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def cmd_update(args: List[str]):
    check_only = "--check" in args or "-c" in args
    print(f"Checking for updates (current version: v{VERSION})...")
    remote_version, content = get_remote_version()
    if not remote_version or not content:
        print("Failed to fetch remote version. Check your internet connection.", file=sys.stderr)
        sys.exit(1)

    if remote_version == VERSION:
        print(f"✓ agy-pool is already up to date (v{VERSION}).")
        return

    print(f"A new version is available: v{remote_version} (current: v{VERSION})")
    if check_only:
        print("Run 'agy-pool update' to install.")
        return

    # Determine target binary location
    target_path = os.path.realpath(sys.argv[0])
    if not os.path.basename(target_path).startswith("agy-pool") or not os.access(target_path, os.W_OK):
        candidate = os.path.expanduser("~/.local/bin/agy-pool")
        if os.path.exists(candidate) and os.access(candidate, os.W_OK):
            target_path = candidate
        else:
            print(f"Cannot write to executable at {target_path}.", file=sys.stderr)
            print("Try updating with: curl -fsSL https://raw.githubusercontent.com/mr-ceo7/agy-pool/main/install.sh | bash", file=sys.stderr)
            sys.exit(1)

    try:
        compile(content, target_path, "exec")
    except SyntaxError as e:
        print(f"Error: Downloaded script has syntax errors: {e}", file=sys.stderr)
        sys.exit(1)

    tmp_path = target_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, target_path)
        print(f"✓ Successfully updated agy-pool to v{remote_version} at {target_path}")
    except Exception as e:
        print(f"Error applying update: {e}", file=sys.stderr)
        sys.exit(1)

# ---------------------------------------------------------------------------
def cmd_add_claude(pool: AccountPool, args: List[str]):
    if not args:
        print("Usage: agy-pool claude add <account_name>", file=sys.stderr)
        sys.exit(1)
    acc_name = args[0]
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", acc_name).strip("_")
    account_home = os.path.join(pool.accounts_dir, clean_id)
    os.makedirs(account_home, exist_ok=True)
    ensure_claude_symlinks(account_home)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = account_home
    print(f"\n[claude-pool] Launching isolated Claude authentication for '{clean_id}'...")
    print("Authenticate in the terminal/browser session.")
    subprocess.run(["claude"], env=env)
    record = pool.add_account_from_dir(clean_id, account_home, tool_type="claude", email=f"{clean_id}@claude")
    print(f"\n✓ Registered Claude account '{clean_id}' into pool.")

def cmd_import_claude(pool: AccountPool, args: List[str]):
    source = args[0] if args else "primary"
    source_dir = os.path.expanduser(f"~/.{source}") if not os.path.isabs(source) else source
    if not os.path.isdir(source_dir):
        print(f"Directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)
    clean_id = os.path.basename(source_dir).lstrip(".")
    record = pool.add_account_from_dir(clean_id, source_dir, tool_type="claude", email=f"{clean_id}@claude")
    ensure_claude_symlinks(record["home_dir"])
    print(f"✓ Imported Claude account '{clean_id}' from {source_dir}")

# ---------------------------------------------------------------------------
# Main Execution Entry Point
# ---------------------------------------------------------------------------

def main():
    invoked_name = os.path.basename(sys.argv[0]).lower()
    tool_id = "agy"
    if "claude" in invoked_name:
        tool_id = "claude"
    elif "copilot" in invoked_name:
        tool_id = "copilot"
    elif len(sys.argv) > 1 and sys.argv[1].lower() in ("claude", "copilot", "gemini", "agy"):
        tool_id = sys.argv[1].lower()
        if tool_id == "gemini":
            tool_id = "agy"
        sys.argv.pop(1)

    if tool_id == "claude":
        pool_base = os.path.expanduser("~/.claude_accounts")
        tool_name = "Claude"
        default_bin = "claude"
    elif tool_id == "copilot":
        pool_base = os.path.expanduser("~/.copilot_accounts")
        tool_name = "Copilot"
        default_bin = "copilot"
    else:
        pool_base = DEFAULT_ACCOUNTS_DIR
        tool_name = "AGY"
        default_bin = "agy"

    pool = AccountPool(base_dir=pool_base)

    if tool_id == "claude":
        auto_import_claude_accounts(pool)

    # Route subcommands
    if len(sys.argv) > 1:
        sub = sys.argv[1].lower()
        if sub in ("status", "list", "pool-status", "pool-list"):
            cmd_status(pool, sys.argv[2:], tool_name=tool_name)
            return
        elif sub in ("switch", "pool-switch"):
            cmd_switch(pool, sys.argv[2:])
            return
        elif sub in ("add", "pool-add"):
            if tool_id == "claude":
                cmd_add_claude(pool, sys.argv[2:])
            else:
                cmd_add(pool, sys.argv[2:])
            return
        elif sub in ("import", "import-current"):
            if tool_id == "claude":
                cmd_import_claude(pool, sys.argv[2:])
            else:
                cmd_import_current(pool, sys.argv[2:])
            return
        elif sub in ("import-keyring",):
            cmd_import_keyring(pool, sys.argv[2:])
            return
        elif sub in ("test", "probe"):
            cmd_test(pool, sys.argv[2:], tool_bin=default_bin, tool_id=tool_id)
            return
        elif sub in ("remove", "rm", "delete"):
            cmd_remove(pool, sys.argv[2:])
            return
        elif sub in ("update", "upgrade"):
            cmd_update(sys.argv[2:])
            return
        elif sub in ("-v", "--version", "version"):
            print(f"agy-pool v{VERSION} ({tool_name} mode)")
            return
        elif sub in ("-h", "--help", "help"):
            print(f"agy-pool v{VERSION} - Multi-account rotation pool for {tool_name}")
            print(f"""
Commands ({tool_name} mode):
  {invoked_name}                         Launch interactive session with active account
  {invoked_name} -p "prompt"             Run prompt with automatic quota failover & retry
  {invoked_name} add <name>              Authenticate and add a new account
  {invoked_name} import [name]           Import current active local credentials into pool
  {invoked_name} status                  Show registered accounts and quota cooldowns
  {invoked_name} switch [name]           Switch active account
  {invoked_name} test [name]             Probe accounts with a test prompt
  {invoked_name} update [--check]        Check for updates or update to the latest version
  {invoked_name} remove <name>           Remove an account from the pool

Multi-CLI Support:
  agy-pool                         Default Google Antigravity mode
  agy-pool claude [command]        Claude Code mode (or use 'claude-pool')
  agy-pool copilot [command]       GitHub Copilot mode (or use 'copilot-pool')
""")
            return

    check_for_updates_background(pool.base_dir)

    # Normal execution: Get active account
    account = pool.get_active_account()
    if not account:
        print(f"[{tool_id}-pool] No accounts in pool, executing with default environment...", file=sys.stderr)
        os.execvp(default_bin, [default_bin] + sys.argv[1:])

    env = os.environ.copy()
    if tool_id == "claude":
        ensure_claude_symlinks(account["home_dir"])
        env["CLAUDE_CONFIG_DIR"] = account["home_dir"]
    elif tool_id == "copilot":
        env["XDG_CONFIG_HOME"] = account["home_dir"]
    else:
        ensure_account_symlinks(account["home_dir"])
        env["HOME"] = account["home_dir"]
        env["DBUS_SESSION_BUS_ADDRESS"] = "disabled"
        env.pop("SSH_CONNECTION", None)
        env.pop("SSH_CLIENT", None)
        env.pop("SSH_TTY", None)

    is_print_mode = any(arg in ("-p", "--print") or arg.startswith("-p=") for arg in sys.argv[1:])

    if not is_print_mode:
        print(f"[{tool_id}-pool] Account: {account['id']} ({account.get('email')}) | Workspace: {os.getcwd()}", file=sys.stderr)
        os.execvpe(default_bin, [default_bin] + sys.argv[1:], env)
    else:
        while True:
            proc = subprocess.Popen(
                [default_bin] + sys.argv[1:],
                env=env,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=subprocess.PIPE,
                text=True
            )
            _, stderr = proc.communicate()

            if proc.returncode != 0 and is_quota_error(stderr, tool_id):
                print(f"\n[{tool_id}-pool] Quota limit hit on '{account['id']}'. Rolling over to next account...", file=sys.stderr)
                next_acc = pool.mark_quota_exhausted(account["id"], stderr)
                if next_acc and next_acc["id"] != account["id"]:
                    account = next_acc
                    if tool_id == "claude":
                        ensure_claude_symlinks(next_acc["home_dir"])
                        env["CLAUDE_CONFIG_DIR"] = next_acc["home_dir"]
                    elif tool_id == "copilot":
                        env["XDG_CONFIG_HOME"] = next_acc["home_dir"]
                    else:
                        ensure_account_symlinks(next_acc["home_dir"])
                        env["HOME"] = next_acc["home_dir"]
                    print(f"[{tool_id}-pool] Retrying with account: '{next_acc['id']}' ({next_acc.get('email')})...\n", file=sys.stderr)
                    continue

            if stderr:
                sys.stderr.write(stderr)
            if proc.returncode == 0:
                pool.record_success(account["id"])
            sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
