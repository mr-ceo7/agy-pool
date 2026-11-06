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
    assert cli.is_quota_error("You've hit your 5-hour limit", "claude")
    assert not cli.is_quota_error("all good")


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


def test_interactive_resume_args_preserve_explicit_conversation():
    assert cli.interactive_resume_args(["--model", "test"]) == ["--model", "test", "--continue"]
    assert cli.interactive_resume_args(["--conversation", "abc"]) == ["--conversation", "abc"]
    assert cli.interactive_resume_args(["-c"]) == ["-c"]


def test_interactive_session_detects_quota_output(tmp_path, monkeypatch):
    stdin = open(os.devnull, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger = cli.run_interactive_session(
            [sys.executable, "-c", "import time; print('Error 429: RESOURCE_EXHAUSTED', flush=True); time.sleep(30)"],
            os.environ.copy(),
        )
        output.flush()
        assert trigger == "quota"
        assert code == -signal.SIGINT
        assert b"RESOURCE_EXHAUSTED" in (tmp_path / "terminal-output").read_bytes()
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
        code, trigger = cli.run_interactive_session(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
        )
        assert trigger == "interrupt"
        assert code == -signal.SIGINT
    finally:
        stdin.close()
        output.close()


def test_interactive_session_drains_output_after_process_exit(tmp_path, monkeypatch):
    stdin = open(os.devnull, "rb")
    output = open(tmp_path / "terminal-output", "wb")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", output)
    try:
        code, trigger = cli.run_interactive_session(
            [sys.executable, "-c", "print('x' * 12000, end='', flush=True)"],
            os.environ.copy(),
        )
        output.flush()
        assert code == 0
        assert trigger is None
        assert (tmp_path / "terminal-output").read_bytes() == b"x" * 12000
    finally:
        stdin.close()
        output.close()


def test_standalone_script_matches_package():
    """install.sh and `agy-pool update` ship the top-level agy-pool file; pip ships agy_pool/cli.py."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agy-pool")) as a, open(os.path.join(root, "agy_pool", "cli.py")) as b:
        assert a.read() == b.read(), "copy agy_pool/cli.py over agy-pool"
