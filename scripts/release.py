"""Publish a tested `main` commit as an immutable, self-contained release.

GitHub resolves `uses:` literally, so on `main` this repository refers to its
own actions at `@main`. A release commit rewrites those references to the
release tag, sits on top of the released `main` commit and is reachable only
through that tag. Projects pin the release commit; later merges to `main`
change nothing they run.

    uv run --locked python scripts/release.py v1.2.0

A fork releases the same way and its release then refers to the fork.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SELF = "Deploy-your-Startup/deploy"
VERSION = re.compile(r"v\d+\.\d+\.\d+")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def git(*args, cwd, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def repository_of(root, remote):
    match = GITHUB_REMOTE.search(git("remote", "get-url", remote, cwd=root))
    if not match:
        raise SystemExit(f"Cannot derive owner/repository from remote {remote}; pass --repository")
    return match.group(1)


def rewrite(root, repository, version):
    names = "|".join(re.escape(name) for name in {SELF, repository})
    pattern = re.compile(r"(uses:\s*)(?:" + names + r")(/\.github/[\w./-]+)@main\b")
    count = 0
    for path in sorted((root / ".github").rglob("*.yml")):
        content = path.read_text()
        updated, replaced = pattern.subn(rf"\g<1>{repository}\g<2>@{version}", content)
        if replaced:
            path.write_text(updated)
            count += replaced
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="release tag, for example v1.2.0")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repository", help="owner/name the release refers to; default: the remote's")
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    version, remote = args.version, args.remote

    if not VERSION.fullmatch(version):
        raise SystemExit("The version must look like v1.2.0")
    if git("status", "--porcelain", cwd=root):
        raise SystemExit("The working tree has uncommitted changes")
    git("fetch", "--quiet", "--tags", remote, "main", cwd=root)
    head, released = git("rev-parse", "HEAD", cwd=root), git("rev-parse", "FETCH_HEAD", cwd=root)
    if head != released:
        raise SystemExit(f"HEAD must be {remote}/main ({released[:12]}): only merged, checked commits are released")
    if git("ls-remote", "--tags", remote, "refs/tags/" + version, cwd=root):
        raise SystemExit(f"{version} already exists; published releases never move")
    repository = args.repository or repository_of(root, remote)

    with tempfile.TemporaryDirectory() as parent:
        tree = Path(parent) / "release"
        git("worktree", "add", "--quiet", "--detach", str(tree), head, cwd=root)
        try:
            count = rewrite(tree, repository, version)
            if not count:
                raise SystemExit("No references to this repository found; nothing to release")
            git("commit", "--quiet", "--all", "-m", f"Release {version}\n\nReleased from {head}.", cwd=tree)
            git("tag", "--annotate", "-m", f"Release {version}", version, cwd=tree)
            check = subprocess.run([sys.executable, str(tree / "scripts/check_release_refs.py"), str(tree)],
                                   capture_output=True, text=True, env={**os.environ, "GITHUB_REPOSITORY": repository})
            if check.returncode:
                git("tag", "--delete", version, cwd=root)
                raise SystemExit("The release is not self-contained:\n" + check.stdout + check.stderr)
            git("push", "--quiet", remote, "refs/tags/" + version, cwd=root)
            commit = git("rev-parse", version + "^{commit}", cwd=root)
        finally:
            git("worktree", "remove", "--force", str(tree), cwd=root, check=False)
    print(f"Released {version}: {count} references name {repository}@{version}")
    print(f"Pin projects to {commit}")


if __name__ == "__main__":
    main()
