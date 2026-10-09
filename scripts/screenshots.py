"""Regenerate the README screenshots in docs/.

Runs the real agy-pool CLI inside a throwaway home directory, with small stand-in
programs for agy, claude and copilot, so the output is genuine but involves no real
accounts or tokens. Needs `rich` (pip install rich).

    python scripts/screenshots.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CLI = ROOT / "agy-pool"  # the standalone script the installer ships

FAKE_AGY = r'''#!/usr/bin/env python3
"""Stand-in for agy used only to render screenshots."""
import base64, json, os, sys
home = os.environ["HOME"]
args = sys.argv[1:]
cli_dir = os.path.join(home, ".gemini", "antigravity-cli")
if args[:2] == ["-p", "Hello"]:  # `agy-pool add` login
    print("Sign in with Google: https://accounts.google.com/o/oauth2/v2/auth?client_id=demo&...")
    print("Enter authorization code: ****************")
    os.makedirs(cli_dir, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": os.environ["DEMO_EMAIL"]}).encode()).decode().rstrip("=")
    with open(os.path.join(cli_dir, "antigravity-oauth-token"), "w") as f:
        json.dump({"id_token": "e30." + claims + ".sig"}, f)
    print("Signed in.")
    sys.exit(0)
if "-p" in args:
    if os.path.basename(home) in os.environ.get("DEMO_EXHAUSTED", "").split(","):
        print("Error: 429 RESOURCE_EXHAUSTED: You have exhausted your capacity on this model.", file=sys.stderr)
        sys.exit(1)
    prompt = args[args.index("-p") + 1]
    if "PONG" in prompt:
        print("PONG")
    else:
        print("Found 3 open TODOs:")
        print("  src/auth/routes.py:41   validate refresh-token expiry")
        print("  src/auth/routes.py:88   rate-limit /login")
        print("  src/billing/invoice.py:12  remove legacy tax table")
    sys.exit(0)
print("(interactive session)")
'''

FAKE_COPILOT = r'''#!/usr/bin/env python3
"""Stand-in for the GitHub Copilot CLI used only to render screenshots."""
import json, os, sys
home = os.environ["COPILOT_HOME"]
if sys.argv[1:2] == ["login"]:
    login = os.environ["DEMO_LOGIN"]
    print("To authenticate, visit https://github.com/login/device and enter code 1A2B-3C4D")
    with open(os.path.join(home, "config.json"), "w") as f:
        f.write("// This file is managed automatically.\n")
        json.dump({"lastLoggedInUser": {"host": "https://github.com", "login": login},
                   "loggedInUsers": [{"host": "https://github.com", "login": login}]}, f)
    print(f"Signed in successfully as {login}.")
    sys.exit(0)
print("PONG")
'''


def jwt_for(email: str) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"e30.{claims}.sig"


class Sandbox:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="agy-pool-demo-"))
        self.home = self.dir / "home"
        self.bin = self.dir / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        for name, body in (("agy", FAKE_AGY), ("copilot", FAKE_COPILOT), ("claude", FAKE_AGY)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        for name in ("agy-pool", "claude-pool", "copilot-pool"):
            (self.bin / name).symlink_to(CLI)
        # an already logged-in agy on the "machine"
        cli_dir = self.home / ".gemini" / "antigravity-cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "antigravity-oauth-token").write_text(json.dumps({"id_token": jwt_for("dev@example.com")}))
        (self.home / ".copilot").mkdir()

    def run(self, cmd: str, **extra_env: str) -> str:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TERM": "dumb",
            "PYTHONUNBUFFERED": "1",  # keep stdout/stderr in the order a terminal shows them
            "PYTHONPATH": str(ROOT),
            **extra_env,
        }
        res = subprocess.run(cmd, shell=True, cwd=self.home, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return res.stdout.replace(str(self.home), "~")


def shot(name: str, title: str, blocks: list[tuple[str, str]], width: int = 84) -> None:
    console = Console(record=True, width=width, force_terminal=True, color_system="truecolor",
                      file=open(os.devnull, "w"))
    for i, (cmd, output) in enumerate(blocks):
        if i:
            console.print()
        line = Text("$ ", style="bold #6a9955")
        line.append(cmd, style="bold #e6e6e6")
        console.print(line)
        console.print(Text(output.rstrip("\n"), style="#c8ccd4"))
    DOCS.mkdir(exist_ok=True)
    (DOCS / f"{name}.svg").write_text(console.export_svg(title=title))
    print(f"wrote docs/{name}.svg")


def main() -> int:
    sb = Sandbox()
    out_import = sb.run("agy-pool import primary")
    out_add = sb.run("agy-pool add backup", DEMO_EMAIL="backup@example.com")
    sb.run("agy-pool add spare", DEMO_EMAIL="spare@example.com")
    sb.run("agy-pool test primary")
    out_failover = sb.run('agy-pool -p "List the open TODOs in this repo"', DEMO_EXHAUSTED="primary")
    out_status = sb.run("agy-pool status")

    shot("setup", "agy-pool: building a pool", [
        ("agy-pool import primary", out_import),
        ("agy-pool add backup", out_add),
    ])
    shot("failover", "agy-pool: quota failover", [
        ('agy-pool -p "List the open TODOs in this repo"', out_failover),
    ])
    shot("status", "agy-pool status", [("agy-pool status", out_status)])

    out_cp_import = sb.run("copilot-pool add personal", DEMO_LOGIN="octocat")
    sb.run("copilot-pool add work", DEMO_LOGIN="octo-work")
    out_cp_status = sb.run("copilot-pool status")
    shot("copilot", "copilot-pool", [
        ("copilot-pool add personal", out_cp_import),
        ("copilot-pool status", out_cp_status),
    ])

    for label, text in (("import", out_import), ("add", out_add), ("failover", out_failover),
                        ("status", out_status), ("copilot add", out_cp_import), ("copilot status", out_cp_status)):
        print(f"----- {label}\n{text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
