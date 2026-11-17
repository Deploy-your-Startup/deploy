"""Keep one audit issue current; source errors remain visible findings."""

import argparse
import json
from pathlib import Path
import subprocess

MARKER = "<!-- deploy-role-dependency-audit -->"
TITLE = "Role dependency audit"


def gh(*args):
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads((args.report / "report.json").read_text())
    body = args.report / "report.md"
    if MARKER not in body.read_text() or not rows:
        raise ValueError("Missing audit marker or results")
    issues = json.loads(gh("issue", "list", "--repo", args.repo, "--state", "all",
                           "--search", f'"{TITLE}" in:title', "--limit", "100",
                           "--json", "number,title,body,state"))
    matching = [item for item in issues if MARKER in (item.get("body") or "")
                and item["title"] == TITLE]
    if len(matching) > 1:
        raise ValueError("Multiple audit issues; refusing to update an arbitrary issue")
    findings = any(row["status"] != "current" for row in rows)
    if matching:
        issue = matching[0]
        number = str(issue["number"])
        gh("issue", "edit", number, "--repo", args.repo, "--body-file", str(body))
        if findings and issue["state"] == "CLOSED":
            gh("issue", "reopen", number, "--repo", args.repo)
        elif not findings and issue["state"] == "OPEN":
            gh("issue", "close", number, "--repo", args.repo)
    elif findings:
        gh("issue", "create", "--repo", args.repo, "--title", TITLE,
           "--body-file", str(body))


if __name__ == "__main__":
    main()
