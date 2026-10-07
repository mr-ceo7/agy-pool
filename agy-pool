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
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

VERSION = "1.0.0"

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

    def add_account_from_dir(self, account_id: str, source_gemini_dir: str, email: str = "") -> dict:
        clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", account_id).strip("_")
        account_home = os.path.join(self.accounts_dir, clean_id)
        target_gemini = os.path.join(account_home, ".gemini", "antigravity-cli")
        os.makedirs(target_gemini, exist_ok=True)

        for fname in ["antigravity-oauth-token", "settings.json", "installation_id", "antigravity_state.pbtxt"]:
            src = os.path.join(source_gemini_dir, fname)
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

def is_quota_error(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"429",
        r"RESOURCE_EXHAUSTED",
        r"quota.*exceeded",
        r"rate.*limit",
        r"exhausted.*quota",
        r"too many requests",
        r"resource has been exhausted",
        r"exceeded your current quota"
    ]
    low = text.lower()
    return any(re.search(p, low) for p in patterns)

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

def cmd_status(pool: AccountPool, args: List[str]):
    accounts = pool.list_accounts()
    active = pool.get_active_account()
    active_id = active.get("id") if active else None

    print("\n" + "=" * 68)
    print("  AGY ACCOUNT ROTATION POOL")
    print("=" * 68)

    if not accounts:
        print("  No accounts registered yet.")
        print("  Import current: agy-pool import <name>")
        print("  Add new:        agy-pool add <name>")
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

def cmd_test(pool: AccountPool, args: List[str]):
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

    print("\n--- Testing Accounts in Pool ---")
    for acc in accounts:
        acc_id = acc["id"]
        acc_home = acc["home_dir"]
        ensure_account_symlinks(acc_home)
        print(f"\nTesting '{acc_id}' ({acc.get('email', 'N/A')})...")

        env = os.environ.copy()
        env["HOME"] = acc_home
        env["DBUS_SESSION_BUS_ADDRESS"] = "disabled"
        env.pop("SSH_CONNECTION", None)
        env.pop("SSH_CLIENT", None)

        start = time.time()
        res = subprocess.run(
            ["agy", "-p", "Respond with PONG"],
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

# ---------------------------------------------------------------------------
# Main Execution Entry Point
# ---------------------------------------------------------------------------

def main():
    pool = AccountPool()

    # Route subcommands
    if len(sys.argv) > 1:
        sub = sys.argv[1].lower()
        if sub in ("status", "list", "pool-status", "pool-list"):
            cmd_status(pool, sys.argv[2:])
            return
        elif sub in ("switch", "pool-switch"):
            cmd_switch(pool, sys.argv[2:])
            return
        elif sub in ("add", "pool-add"):
            cmd_add(pool, sys.argv[2:])
            return
        elif sub in ("import", "import-current"):
            cmd_import_current(pool, sys.argv[2:])
            return
        elif sub in ("import-keyring",):
            cmd_import_keyring(pool, sys.argv[2:])
            return
        elif sub in ("test", "probe"):
            cmd_test(pool, sys.argv[2:])
            return
        elif sub in ("remove", "rm", "delete"):
            cmd_remove(pool, sys.argv[2:])
            return
        elif sub in ("-v", "--version", "version"):
            print(f"agy-pool v{VERSION}")
            return
        elif sub in ("-h", "--help", "help"):
            print(__doc__.strip())
            print("""
Commands:
  agy-pool                         Launch interactive session with active account
  agy-pool -p "prompt"             Run prompt with automatic quota failover & retry
  agy-pool add <name>              Log in and add a new Google account
  agy-pool import [name]           Import current active local agy token into pool
  agy-pool import-keyring [name]   Import token from system desktop keyring
  agy-pool status                  Show registered accounts and quota cooldowns
  agy-pool switch [name]           Switch active account
  agy-pool test [name]             Probe accounts with a test prompt
  agy-pool remove <name>           Remove an account from the pool
""")
            return

    # Normal execution: Get active account
    account = pool.get_active_account()
    if not account:
        print("[agy-pool] No accounts in pool, executing with default environment...", file=sys.stderr)
        os.execvp("agy", ["agy"] + sys.argv[1:])

    ensure_account_symlinks(account["home_dir"])

    env = os.environ.copy()
    env["HOME"] = account["home_dir"]

    # Disconnect from desktop keyring so agy uses the isolated account token in HOME,
    # without faking an SSH session (which breaks clipboard image pasting in the terminal).
    env["DBUS_SESSION_BUS_ADDRESS"] = "disabled"
    env.pop("SSH_CONNECTION", None)
    env.pop("SSH_CLIENT", None)
    env.pop("SSH_TTY", None)

    is_print_mode = any(arg in ("-p", "--print") or arg.startswith("-p=") for arg in sys.argv[1:])

    if not is_print_mode:
        # Interactive mode: Replace process directly for full TTY & terminal compatibility
        print(f"[agy-pool] Account: {account['id']} ({account.get('email')}) | Workspace: {os.getcwd()}", file=sys.stderr)
        os.execvpe("agy", ["agy"] + sys.argv[1:], env)
    else:
        # Print mode: Stream stdout live, capture stderr for quota detection & failover
        while True:
            proc = subprocess.Popen(
                ["agy"] + sys.argv[1:],
                env=env,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=subprocess.PIPE,
                text=True
            )
            _, stderr = proc.communicate()

            if proc.returncode != 0 and is_quota_error(stderr):
                print(f"\n[agy-pool] Quota limit hit on '{account['id']}'. Rolling over to next account...", file=sys.stderr)
                next_acc = pool.mark_quota_exhausted(account["id"], stderr)
                if next_acc and next_acc["id"] != account["id"]:
                    account = next_acc
                    ensure_account_symlinks(next_acc["home_dir"])
                    env["HOME"] = next_acc["home_dir"]
                    print(f"[agy-pool] Retrying with account: '{next_acc['id']}' ({next_acc.get('email')})...\n", file=sys.stderr)
                    continue

            if stderr:
                sys.stderr.write(stderr)
            if proc.returncode == 0:
                pool.record_success(account["id"])
            sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
