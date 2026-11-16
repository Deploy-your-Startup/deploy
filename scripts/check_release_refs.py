"""Check that this repository's references to itself form one immutable release.

The template refers to the owner copy's ``main``; `startup sync` rewrites those
references to a tag it creates on the published commit. Either form is valid.
A mix, or a tag that holds different contents, means a later sync could change
what a pinned project runs.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

TEMPLATE = "§§deploy_your_startup.github_username§§/§§deploy_your_startup.deploy_repo_name§§"


def self_references(root, repositories):
    names = "|".join(re.escape(name) for name in repositories)
    pattern = re.compile(r"uses:\s*(?:" + names + r")/\.github/[\w./-]+@([^\s#]+)")
    for path in sorted((root / ".github").rglob("*.yml")):
        for reference in pattern.findall(path.read_text()):
            yield path.relative_to(root), reference


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    repositories = [TEMPLATE, *filter(None, [os.environ.get("GITHUB_REPOSITORY")])]
    found = list(self_references(root, repositories))
    references = {reference for _path, reference in found}
    if len(references) > 1:
        lines = "\n".join(f"  {path}: @{reference}" for path, reference in found)
        raise SystemExit("References to this repository must share one revision:\n" + lines)
    if references <= {"main"}:
        print(f"{len(found)} references follow main: not a synced release")
        return

    def tree(revision):
        return subprocess.run(["git", "-C", str(root), "rev-parse", revision + "^{tree}"],
                              capture_output=True, text=True).stdout.strip()

    tag = references.pop()
    if not tree("refs/tags/" + tag) or tree("refs/tags/" + tag) != tree("HEAD"):
        raise SystemExit(f"Release tag {tag} does not hold the contents that refer to it")
    print(f"{len(found)} references resolve to this release, {tag}")


if __name__ == "__main__":
    main()
