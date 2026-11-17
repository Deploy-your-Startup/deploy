"""Read published version metadata without executing upstream code."""

import json
import os
import re
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml


def version(value):
    # k3s uses a numeric build revision that must sort after the patch number.
    match = re.fullmatch(r"v?(\d+(?:\.\d+){1,2})(?:\+k3s(\d+))?", str(value))
    if not match:
        return None
    parts = tuple(map(int, match[1].split(".")))
    return (*parts, *([0] * (3 - len(parts))), int(match[2] or 0))


class Sources:
    def __init__(self, snapshot=None):
        self.offline = snapshot is not None
        self.cache = {} if snapshot is None else snapshot

    def get(self, url):
        if url in self.cache:
            cached = self.cache[url]
            if "error" in cached:
                raise ValueError(cached["error"])
            return cached
        if self.offline:
            raise ValueError(f"Snapshot is missing {url}")
        headers = {"User-Agent": "deploy-role-dependency-audit"}
        if urlparse(url).netloc == "api.github.com":
            if token := os.environ.get("GH_TOKEN"):
                headers["Authorization"] = f"Bearer {token}"
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                text = response.read().decode()
                data = yaml.safe_load(text) if url.endswith(".yaml") else json.loads(text)
                link = response.headers.get("Link", "")
        except Exception as error:
            # Preserve failures too: replay must reproduce the diagnostic rather
            # than changing it to a missing-response error or retrying online.
            self.cache[url] = {"error": str(error)}
            raise ValueError(str(error)) from error
        self.cache[url] = {"data": data, "link": link}
        return self.cache[url]

    def versions(self, dependency):
        kind, url = dependency["kind"], dependency["url"]
        result, seen = [], set()
        while url:
            if url in seen:
                raise ValueError("Upstream pagination loop")
            if urlparse(url).netloc != urlparse(dependency["url"]).netloc:
                raise ValueError("Upstream pagination changed host")
            seen.add(url)
            page = self.get(url)
            data = page["data"]
            following = None
            if kind == "github":
                result.extend(row["tag_name"] for row in data
                              if not row["draft"] and not row["prerelease"])
                match = re.search(r'<([^>]+)>; rel="next"', page["link"])
                following = match[1] if match else None
            elif kind == "helm":
                result.extend(row["version"] for row in data["entries"][dependency["chart"]]
                              if not row.get("deprecated", False))
            elif kind == "pypi":
                result.extend(tag for tag, files in data["releases"].items()
                              if files and any(not f.get("yanked", False) for f in files))
            elif kind == "postgres":
                result.extend(f'{row["major"]}.{row["latestMinor"]}' for row in data
                              if row["supported"])
            elif kind == "k3s":
                result.extend(row["latest"] for row in data["data"]
                              if row["id"] == "stable" or re.fullmatch(r"v\d+\.\d+", row["id"]))
            elif kind == "galaxy":
                result.extend(row["version"] for row in data["data"])
                if data["links"]["next"]:
                    following = urljoin(url, data["links"]["next"])
            else:
                raise ValueError(f"Unknown source kind: {kind}")
            url = following
        stable = sorted({str(tag) for tag in result if version(tag)},
                        key=lambda tag: (version(tag), tag))
        if not stable:
            raise ValueError("Source contains no stable versions")
        return stable
