import json
import os

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


def test_standalone_script_matches_package():
    """install.sh and `agy-pool update` ship the top-level agy-pool file; pip ships agy_pool/cli.py."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agy-pool")) as a, open(os.path.join(root, "agy_pool", "cli.py")) as b:
        assert a.read() == b.read(), "copy agy_pool/cli.py over agy-pool"
