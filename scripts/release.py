"""预览、创建本地 Tag 或发布 GitHub Release；默认不修改 Git 或远程状态。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, timeout: int = 120) -> str:
    """使用参数数组调用命令，任何失败立即停止，且不等待交互式凭据输入。"""
    result = subprocess.run(
        args, cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"},
    )
    return result.stdout.strip()


def repository_name(url: str) -> str:
    """接受 GitHub 的 HTTPS/SSH 地址，统一为 owner/repo 以校验发布目标。"""
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url,
    )
    if not match:
        raise ValueError("仅支持 github.com 的 HTTPS 或 SSH 仓库地址")
    return match[1]


def load_release() -> tuple[str, str, Path]:
    """从 Manifest 派生版本与仓库，版本说明按版本号保存。"""
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("Manifest version 必须是无前后缀的三段式版本号")
    if manifest["manifest_version"] != 2:
        raise ValueError("仅支持 Manifest v2")
    notes = ROOT / "releases" / f"{version}.md"
    if not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
        raise ValueError(f"缺少非空 Release 说明：{notes}")
    for filename in ("plugin.py", "README.md", "LICENSE"):
        if not (ROOT / filename).is_file():
            raise ValueError(f"缺少发布文件：{filename}")
    return version, repository_name(manifest["urls"]["repository"]), notes


def check_local_tag(tag: str, head: str) -> bool:
    """已有 Tag 只允许复用同一提交，绝不移动或覆盖已有版本。"""
    if not run("git", "tag", "--list", tag):
        return False
    if run("git", "rev-parse", f"refs/tags/{tag}^{{commit}}") != head:
        raise ValueError(f"{tag} 已指向其他提交，请使用新版本号")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tag-only", action="store_true", help="检查并创建本地附注 Tag，不联网")
    mode.add_argument("--publish", action="store_true", help="检查、推送 main 和 Tag，然后创建正式 Release")
    args = parser.parse_args(argv)
    version, repo, notes = load_release()
    tag = f"v{version}"
    head = run("git", "rev-parse", "HEAD")
    branch = run("git", "branch", "--show-current")
    dirty = run("git", "status", "--porcelain", "--untracked-files=all")
    print(f"仓库：{repo}\n版本：{version}\nTag：{tag}\n分支：{branch}\n提交：{head}")
    print(f"\nRelease 说明：\n{notes.read_text(encoding='utf-8')}")
    if not args.tag_only and not args.publish:
        print("预览完成；未创建 Tag，未推送，未发布。")
        if dirty:
            print("工作区有未提交内容，正式操作前请先提交。")
        return 0

    if branch != "main":
        raise ValueError("请切换到 main 分支发布")
    if dirty:
        raise ValueError("工作区有未提交或未跟踪文件，请先提交发布内容")
    # 避免同一 Manifest 版本产生带 v 与不带 v 的两个 Tag。
    if run("git", "tag", "--list", version):
        raise ValueError(f"已存在 {version} Tag；本脚本统一使用 v 前缀，请人工核对现有发布")
    tag_exists = check_local_tag(tag, head)
    print(run(sys.executable, "-m", "pytest", "-q", timeout=300))
    run("git", "diff", "--check")
    if run("git", "rev-parse", "HEAD") != head or run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("验证期间仓库发生变化，请重新检查后执行")

    if args.publish:
        # 仅允许一个且与 Manifest 一致的推送目标；不会修改用户的 SSH/HTTPS 设置。
        urls = run("git", "remote", "get-url", "--push", "--all", "origin").splitlines()
        if len(urls) != 1 or repository_name(urls[0]).lower() != repo.lower():
            raise ValueError("origin 的推送目标与 Manifest 不一致或包含多个地址")
        run("gh", "auth", "status", "--hostname", "github.com")
        # 读取实际推送地址，避免 origin 拉取地址与推送地址不同导致误判。
        remote_tags = run("git", "ls-remote", "--tags", urls[0],
                          f"refs/tags/{version}", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
        refs = dict(line.split()[::-1] for line in remote_tags.splitlines())
        if f"refs/tags/{version}" in refs:
            raise ValueError(f"远程已有 {version} Tag，请人工核对现有发布")
        remote_head = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
        if remote_head and remote_head != head:
            raise ValueError(f"远程 {tag} 指向其他提交，不允许覆盖")
        # API 查询失败也必须停止，不能把网络或认证失败视为 Release 不存在。
        existing = run("gh", "api", f"repos/{repo}/releases", "--paginate", "--jq",
                       f'.[] | select(.tag_name == "{tag}" or .tag_name == "{version}") | .html_url')
        if existing:
            raise ValueError(f"该版本已有 Release，未修改：{existing}")

    if not tag_exists:
        run("git", "tag", "-a", tag, head, "-m", f"Release {version}")
    if args.tag_only:
        print(f"本地 Tag {tag} 已就绪，未推送。")
        return 0

    # 分支与 Tag 一起成功或一起失败；不使用 force，也不推送其他 Tag。
    run("git", "push", "--atomic", "origin", f"{head}:refs/heads/main", f"refs/tags/{tag}:refs/tags/{tag}")
    url = run("gh", "release", "create", tag, "--repo", repo, "--verify-tag",
              "--title", f"GitHub 监控插件 {version}", "--notes-file", str(notes))
    print(f"Release 已发布：{url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"发布未完成：{exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr or exc.stdout, file=sys.stderr)
        print("脚本不会自动回滚 Tag 或远程内容。修复问题后可重试；已有 Release 不会被覆盖。", file=sys.stderr)
        raise SystemExit(1) from None
