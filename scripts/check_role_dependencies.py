"""Report role pins against stable releases; never change deployment defaults."""

import argparse
import json
from pathlib import Path
import sys

import yaml

from role_dependency_sources import Sources, version

ROOT = Path(__file__).resolve().parents[1]


def check(root, dependencies, sources):
    rows = []
    for dependency in sorted(dependencies, key=lambda item: item["name"]):
        row = {"name": dependency["name"], "path": dependency["path"],
               "source": dependency["url"], "current": "?"}
        try:
            data = yaml.safe_load((root / dependency["path"]).read_text())
            if "collection" in dependency:
                pins = [item["version"] for item in data["collections"]
                        if item["name"] == dependency["collection"]]
                if len(pins) != 1:
                    raise ValueError("Expected exactly one collection pin")
                current = str(pins[0])
            else:
                current = str(data[dependency["key"]])
            row["current"] = current
            current_version = version(current)
            if current_version is None:
                raise ValueError(f"Expected a stable numeric pin, found {current!r}")
            candidates = sources.versions(dependency)
            latest = candidates[-1]
            if current_version > version(latest):
                raise ValueError("Pin is newer than the published stable source")
            series = dependency["series"]
            matching = [tag for tag in candidates
                        if version(tag)[:series] == current_version[:series]]
            row.update(latest=latest, same_series=matching[-1] if matching else None,
                       status="update" if version(latest) > current_version else "current")
        except Exception as error:
            row.update(status="error", error=str(error))
        rows.append(row)
    return rows


def markdown(rows):
    def escape(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["<!-- deploy-role-dependency-audit -->", "# Role dependency audit", "",
             "Stable upstream releases compared with repository defaults. "
             "Same series is a version boundary, not a compatibility guarantee.", "",
             "| Dependency | Pinned | Same series | Latest stable | Result |",
             "| --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [row["name"], row["current"], row.get("same_series") or "—",
                  row.get("latest", "—"), row.get("error", row["status"])]
        lines.append("| " + " | ".join(map(escape, values)) + " |")
    lines.extend(["", "## Sources", ""])
    lines.extend(f'- {row["name"]}: `{row["path"]}`; [upstream]({row["source"]})'
                 for row in rows)
    lines.extend(["", "Review release notes and Kubernetes compatibility before updating. "
                  "PostgreSQL major upgrades need a migration; Kubernetes Python client "
                  "majors track Kubernetes releases. No pin, server or cluster was changed.", "",
                  "Scope: explicit role version pins and Ansible collections. "
                  "Distribution packages, the Tailscale installer, Ubuntu image selection "
                  "and components bundled with k3s (including Traefik) require separate review. "
                  "This is a version audit, not a vulnerability or support-lifecycle scan.", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=ROOT / "scripts/role-dependencies.json")
    parser.add_argument("--snapshot", type=Path, help="Replay upstream data offline")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/role-dependencies")
    args = parser.parse_args()
    dependencies = json.loads(args.manifest.read_text())
    sources = Sources(json.loads(args.snapshot.read_text()) if args.snapshot else None)
    rows = check(args.root, dependencies, sources)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in (("report.json", rows), ("sources.json", sources.cache)):
        (args.output / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    report = markdown(rows)
    (args.output / "report.md").write_text(report)
    print(report)
    return int(any(row["status"] == "error" for row in rows))


if __name__ == "__main__":
    sys.exit(main())
