"""Report upstream changes since the last reviewed commit, using only the standard library."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def comparison(baseline: dict) -> dict:
    repository = baseline["repository"]
    reviewed = baseline["reviewed_commit"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, flags=re.ASCII):
        raise ValueError("上游仓库必须使用 owner/repository 格式")
    if not re.fullmatch(r"[a-f0-9]{40}", reviewed):
        raise ValueError("已评审提交必须是完整的 40 位 SHA")
    branch = urllib.parse.quote(baseline["branch"], safe="")
    api = f"https://api.github.com/repos/{repository}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ForeverFishing-upstream-review",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(url: str) -> dict:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    # Resolve the branch first so the report stays tied to one commit if upstream moves.
    head = fetch(f"{api}/commits/{branch}")["sha"]
    data = fetch(f"{api}/compare/{reviewed}...{head}")
    return {
        "repository": repository,
        "reviewed_commit": reviewed,
        "head_commit": head,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": data["status"],
        "ahead_by": data["ahead_by"],
        "behind_by": data["behind_by"],
        "total_commits": data["total_commits"],
        "commit_list_complete": len(data["commits"]) == data["total_commits"],
        "compare_url": data["html_url"],
        "commits": [
            {
                "sha": commit["sha"],
                "title": commit["commit"]["message"].split("\n", 1)[0] or "（无提交说明）",
                "url": commit["html_url"],
            }
            for commit in data["commits"]
        ],
        "files": [
            {
                "path": file["filename"],
                "status": file["status"],
                "additions": file["additions"],
                "deletions": file["deletions"],
            }
            for file in data["files"]
        ],
    }


def markdown(report: dict) -> str:
    lines = [
        "# 上游变更检查",
        "",
        f"仓库：{report['repository']}；检查时间：{report['checked_at']}。",
        "",
        f"已评审：`{report['reviewed_commit']}`；当前：`{report['head_commit']}`。",
        "",
        f"比较状态：{report['status']}；新增 {report['ahead_by']} 个提交，"
        f"落后 {report['behind_by']} 个提交。",
        "",
        f"[查看上游差异]({report['compare_url']})",
        "",
    ]
    if report["commits"]:
        lines += ["## 待评审提交", ""]
        lines += [
            f"- [{commit['sha'][:8]}]({commit['url']}) {commit['title']}"
            for commit in report["commits"]
        ]
        lines.append("")
    if report["files"]:
        lines += ["## 变更文件", "", "| 文件 | 状态 | 增加 | 删除 |", "| --- | --- | ---: | ---: |"]
        lines += [
            f"| {file['path'].replace('|', r'\|')} | {file['status']} | "
            f"{file['additions']} | {file['deletions']} |"
            for file in report["files"]
        ]
        lines.append("")
    if not report["commit_list_complete"] or len(report["files"]) >= 300:
        lines += ["GitHub 比较接口可能截断长列表，完整内容请查看上游差异链接。", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查已评审基线之后的上游变化并生成报告")
    parser.add_argument("--baseline", type=Path, default=ROOT / ".github/upstream.json")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "checks/upstream")
    parser.add_argument(
        "--fail-on-changes", action="store_true", help="发现未评审变化时返回非零状态"
    )
    args = parser.parse_args()
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report = comparison(baseline)
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / "upstream.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.report_dir / "upstream.md").write_text(markdown(report), encoding="utf-8")
    except urllib.error.HTTPError as error:
        print(f"上游检查失败：GitHub API HTTP {error.code}（{error.reason}）", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"上游检查失败：{error}", file=sys.stderr)
        return 2
    changed = report["head_commit"] != report["reviewed_commit"]
    print(
        f"上游 {report['head_commit'][:8]}："
        f"{'需要评审' if changed else '已评审到最新提交'}；报告：{args.report_dir}"
    )
    return 1 if changed and args.fail_on_changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
