"""验证发布流程的关键保护与失败恢复，所有 Git/GitHub 调用均被模拟。"""

import json
import subprocess

import pytest

from scripts import release


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo", "https://github.com/owner/repo.git",
    "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git",
])
def test_repository_address_formats(url):
    assert release.repository_name(url) == "owner/repo"


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    """最小发布目录和可控制失败点的命令替身，不触碰真实仓库引用。"""
    monkeypatch.setattr(release, "ROOT", tmp_path)
    (tmp_path / "_manifest.json").write_text(json.dumps({
        "version": "2.0.0", "manifest_version": 2,
        "urls": {"repository": "https://github.com/owner/repo"},
    }), encoding="utf-8")
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases/2.0.0.md").write_text("Release notes", encoding="utf-8")
    for name in ("plugin.py", "README.md", "LICENSE"):
        (tmp_path / name).write_text("test", encoding="utf-8")
    state = {"dirty": "", "tag": "", "remote_tag": "", "release": "", "fail": None}
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if state["fail"] and args[:len(state["fail"])] == state["fail"]:
            raise subprocess.CalledProcessError(1, args, stderr="simulated failure")
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "abc123"
        if args[:2] == ("git", "rev-parse"):
            return state["tag"]
        if args[:2] == ("git", "branch"):
            return "main"
        if args[:2] == ("git", "status"):
            return state["dirty"]
        if args[:3] == ("git", "tag", "--list"):
            return "v2.0.0" if args[3] == "v2.0.0" and state["tag"] else ""
        if args[:2] == ("git", "remote"):
            return "git@github.com:owner/repo.git"
        if args[:2] == ("git", "ls-remote"):
            return state["remote_tag"]
        if args[:2] == ("gh", "api"):
            return state["release"]
        return ""

    monkeypatch.setattr(release, "run", run)
    return state, calls


def test_preview_never_changes_git_or_calls_github(release_repo):
    state, calls = release_repo
    state["dirty"] = " M README.md"
    assert release.main([]) == 0
    assert all(call[:2] in [("git", "rev-parse"), ("git", "branch"), ("git", "status")] for call in calls)


def test_dirty_worktree_blocks_tag(release_repo):
    state, calls = release_repo
    state["dirty"] = "?? new-file"
    with pytest.raises(ValueError, match="未提交"):
        release.main(["--tag-only"])
    assert not any(call[:2] == ("git", "tag") for call in calls)


def test_tag_only_runs_tests_without_network(release_repo):
    _, calls = release_repo
    assert release.main(["--tag-only"]) == 0
    assert any(call[1:3] == ("-m", "pytest") for call in calls)
    assert ("git", "tag", "-a", "v2.0.0", "abc123", "-m", "Release 2.0.0") in calls
    assert not any(call[0] == "gh" or call[:2] == ("git", "push") for call in calls)


def test_publish_uses_atomic_push_and_explicit_notes(release_repo):
    _, calls = release_repo
    assert release.main(["--publish"]) == 0
    push = ("git", "push", "--atomic", "origin", "abc123:refs/heads/main", "refs/tags/v2.0.0:refs/tags/v2.0.0")
    assert push in calls
    create = next(call for call in calls if call[:3] == ("gh", "release", "create"))
    assert calls.index(push) < calls.index(create)
    assert "--verify-tag" in create and "--notes-file" in create


@pytest.mark.parametrize("failure", [(release.sys.executable, "-m", "pytest"), ("gh", "auth"), ("gh", "api")])
def test_failed_preflight_never_creates_tag(release_repo, failure):
    state, calls = release_repo
    state["fail"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        release.main(["--publish"])
    assert not any(call[:3] == ("git", "tag", "-a") for call in calls)
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_conflicting_remote_tag_is_rejected(release_repo):
    state, calls = release_repo
    state["remote_tag"] = "other123\trefs/tags/v2.0.0"
    with pytest.raises(ValueError, match="远程.*其他提交"):
        release.main(["--publish"])
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_existing_release_is_not_overwritten(release_repo):
    state, calls = release_repo
    state["release"] = "https://github.com/owner/repo/releases/tag/v2.0.0"
    with pytest.raises(ValueError, match="已有 Release"):
        release.main(["--publish"])
    assert not any(call[:3] == ("gh", "release", "create") for call in calls)


def test_resume_after_tag_push_reuses_tag(release_repo):
    state, calls = release_repo
    state["tag"] = "abc123"
    state["remote_tag"] = "tagobject\trefs/tags/v2.0.0\nabc123\trefs/tags/v2.0.0^{}"
    assert release.main(["--publish"]) == 0
    assert not any(call[:3] == ("git", "tag", "-a") for call in calls)
    assert any(call[:3] == ("gh", "release", "create") for call in calls)


def test_push_failure_does_not_create_release(release_repo):
    state, calls = release_repo
    state["fail"] = ("git", "push")
    with pytest.raises(subprocess.CalledProcessError):
        release.main(["--publish"])
    assert not any(call[:3] == ("gh", "release", "create") for call in calls)
