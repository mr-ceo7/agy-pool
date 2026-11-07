import json
import os
import signal
import sys

import pytest

from agy_pool import cli


@pytest.fixture
def copilot_home(tmp_path):
    home = tmp_path / "real_copilot"
    home.mkdir()
    (home / "config.json").write_text(
        "// managed automatically\n"
        + json.dumps({"lastLoggedInUser": {"host": "https://github.com", "login": "octocat"},
                      "loggedInUsers": [{"host": "https://github.com", "login": "octocat"}]})
    )
    (home / "session-state").mkdir()
    (home / "session-store.db").write_bytes(b"")
    return home


def test_copilot_login_reads_commented_config(copilot_home, tmp_path):
    assert cli.copilot_login(str(copilot_home)) == "octocat"
    assert cli.copilot_login(str(tmp_path / "missing")) is None


def test_copilot_account_copies_config_and_links_shared_state(copilot_home, tmp_path):
    pool = cli.AccountPool(base_dir=str(tmp_path / "pool"))
    rec = pool.add_account_from_dir("work", str(copilot_home), tool_type="copilot")
    assert rec["email"] == "octocat@github"
    home = rec["home_dir"]
    assert cli.copilot_login(home) == "octocat"
    assert not os.path.islink(os.path.join(home, "config.json"))  # per-account login

    cli.ensure_copilot_symlinks(home, real_home=str(copilot_home))
    assert os.readlink(os.path.join(home, "session-state")) == str(copilot_home / "session-state")
    assert os.readlink(os.path.join(home, "session-store.db")) == str(copilot_home / "session-store.db")
    # items missing from the real home are not linked
    assert not os.path.lexists(os.path.join(home, "installed-plugins"))


def test_symlinks_skip_the_real_home(copilot_home):
    cli.ensure_copilot_symlinks(str(copilot_home), real_home=str(copilot_home))
    assert not os.path.islink(copilot_home / "session-state")


def test_quota_detection():
    assert cli.is_quota_error("Error 429: RESOURCE_EXHAUSTED")
    assert cli.is_quota_error("Individual quota reached. Please upgrade your subscription.")
    assert cli.is_quota_error("You've hit your 5-hour limit", "claude")
    assert not cli.is_quota_error("all good")


def test_quota_message_details():
    message = (
        "Individual quota reached. Resets in 122h59m56s. "
        "Error ID: 5b31dcbd-82cc-4d6f-8cef-bc1034e57a1d"
    )
    assert cli.quota_reset_seconds(message) == 122 * 3600 + 59 * 60 + 56
    assert cli.quota_error_id(message) == "5b31dcbd-82cc-4d6f-8cef-bc1034e57a1d"
    assert cli.quota_reset_seconds("Individual quota reached.") is None


def test_quota_failover_uses_provider_reset_duration(tmp_path):
    pool = cli.AccountPool(base_dir=str(tmp_path / "pool"))
    for name in ("a", "b"):
        pool.add_account_from_dir(name, str(tmp_path), tool_type="claude")
    now = cli.time.time()
    pool.mark_quota_exhausted("a", "quota", cooldown_seconds=122 * 3600 + 59 * 60 + 56)
    account = next(acc for acc in pool.list_accounts() if acc["id"] == "a")
    assert account["cooldown_until"] - now > 122 * 3600 + 59 * 60


def test_quota_error_ids_persist_between_pool_instances(tmp_path):
    pool_dir = tmp_path / "pool"
    pool = cli.AccountPool(base_dir=str(pool_dir))
    for name in ("a", "b"):
        pool.add_account_from_dir(name, str(tmp_path), tool_type="claude")
    error_id = "5b31dcbd-82cc-4d6f-8cef-bc1034e57a1d"
    pool.mark_quota_exhausted("a", "quota", error_id=error_id)

    restarted_pool = cli.AccountPool(base_dir=str(pool_dir))
    assert restarted_pool.get_seen_quota_error_ids() == {error_id}


def test_failover_rolls_to_next_ready_account(tmp_path):
    pool = cli.AccountPool(base_dir=str(tmp_path / "pool"))
    for name in ("a", "b"):
        pool.add_account_from_dir(name, str(tmp_path), tool_type="claude")
    assert pool.get_active_account()["id"] == "a"
    nxt = pool.mark_quota_exhausted("a", "429")
    assert nxt["id"] == "b" and pool.get_active_account()["id"] == "b"
    assert pool.mark_quota_exhausted("b", "429") is None  # everyone cooling down


def test_ctrl_c_switches_to_next_ready_account_without_cooling_current(tmp_path):
    pool = cli.AccountPool(base_dir=str(tmp_path / "pool"))
    for name in ("a", "b"):
        pool.add_account_from_dir(name, str(tmp_path), tool_type="claude")
    nxt = pool.switch_to_next_ready("a")
    assert nxt["id"] == "b"
    assert pool.get_active_account()["id"] == "b"
    assert pool.list_accounts()[0]["status"] == "ready"


def test_failover_probes_accounts_before_activating_them(tmp_path, monkeypatch):
    pool = cli.AccountPool(base_dir=str(tmp_path / "pool"))
    for name in ("current", "no_reply", "working"):
        pool.add_account_from_dir(name, str(tmp_path), tool_type="agy")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_agy = fake_bin / "agy"
    fake_agy.write_text(
        "#!/bin/sh\n"
        "case \"$HOME\" in\n"
        "  */no_reply) exit 1 ;;\n"
        "  */working) echo hi ;;\n"
        "esac\n"
    )
    fake_agy.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    next_account = cli.probe_next_agy_account(pool, "current", set())
    assert next_account is not None
    account, env = next_account
    assert account["id"] == "working"
    assert env["HOME"] == account["home_dir"]
    assert pool.get_active_account()["id"] == "working"
    assert next(acc for acc in pool.list_accounts() if acc["id"] == "no_reply")["status"] == "ready"


def test_interactive_resume_args_preserve_explicit_conversation():
    assert cli.interactive_resume_args(["--model", "test"]) == ["--model", "test", "--continue"]
    assert cli.interactive_resume_args(["--conversation", "abc"]) == ["--conversation", "abc"]
    assert cli.interactive_resume_args(["-c"]) == ["-c"]


def test_interactive_session_detects_quota_output(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"\n")
    os.close(write_fd)
    stdin = os.fdopen(read_fd, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger, error_id, cooldown = cli.run_interactive_session(
            [sys.executable, "-c", "import time; print('Error 429: RESOURCE_EXHAUSTED', flush=True); time.sleep(30)"],
            os.environ.copy(),
        )
        output.flush()
        assert trigger == "quota"
        assert code == -signal.SIGINT
        assert error_id is None
        assert cooldown is None
        assert b"RESOURCE_EXHAUSTED" in (tmp_path / "terminal-output").read_bytes()
    finally:
        stdin.close()
        output.close()


def test_interactive_session_ignores_startup_quota_transcript(tmp_path, monkeypatch):
    stdin = open(os.devnull, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger, error_id, cooldown = cli.run_interactive_session(
            [
                sys.executable,
                "-c",
                "print('Individual quota reached. Resets in 2h. Error ID: "
                "5b31dcbd-82cc-4d6f-8cef-bc1034e57a1d', flush=True)",
            ],
            os.environ.copy(),
        )
        assert code == 0
        assert trigger is None
        assert error_id is None
        assert cooldown is None
    finally:
        stdin.close()
        output.close()


def test_interactive_session_ignores_replayed_quota_error(tmp_path, monkeypatch):
    stdin = open(os.devnull, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    error_id = "5b31dcbd-82cc-4d6f-8cef-bc1034e57a1d"
    try:
        code, trigger, detected_id, cooldown = cli.run_interactive_session(
            [
                sys.executable,
                "-c",
                "import sys, time; "
                "print('Individual quota reached. Resets in 2h.', flush=True); "
                "time.sleep(0.2); "
                f"print('Error ID: {error_id}', flush=True)",
            ],
            os.environ.copy(),
            ignored_quota_ids={error_id},
        )
        assert code == 0
        assert trigger is None
        assert detected_id is None
        assert cooldown is None
    finally:
        stdin.close()
        output.close()


def test_interactive_session_ctrl_c_requests_rotation(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"\x03")
    os.close(write_fd)
    stdin = os.fdopen(read_fd, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger, error_id, cooldown = cli.run_interactive_session(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
        )
        assert trigger == "interrupt"
        assert code == -signal.SIGINT
        assert error_id is None
        assert cooldown is None
    finally:
        stdin.close()
        output.close()


def test_interactive_session_drains_output_after_process_exit(tmp_path, monkeypatch):
    stdin = open(os.devnull, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger, error_id, cooldown = cli.run_interactive_session(
            [sys.executable, "-c", "print('x' * 12000, end='', flush=True)"],
            os.environ.copy(),
        )
        output.flush()
        assert code == 0
        assert trigger is None
        assert error_id is None
        assert cooldown is None
        assert (tmp_path / "terminal-output").read_bytes() == b"x" * 12000
    finally:
        stdin.close()
        output.close()


def test_standalone_script_matches_package():
    """install.sh and `agy-pool update` ship the top-level agy-pool file; pip ships agy_pool/cli.py."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agy-pool")) as a, open(os.path.join(root, "agy_pool", "cli.py")) as b:
        assert a.read() == b.read(), "copy agy_pool/cli.py over agy-pool"
