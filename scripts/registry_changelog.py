"""Send this version's CHANGELOG.md section to the Comfy Registry as its changelog.

Usage: REGISTRY_ACCESS_TOKEN=... python scripts/registry_changelog.py [--dry-run]
The version comes from pyproject.toml. The registry sets a version's changelog with a separate
call after it is published, so this runs after the publish step.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.comfy.org"


def project_version(text: str) -> str:
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)


def changelog_section(changelog: str, version: str) -> str | None:
    """The body under `## <version>`, up to the next `## ` heading, or None."""
    m = re.search(rf"^## {re.escape(version)}\s*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    return m.group(1).strip() if m else None


def request(method: str, url: str, token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main() -> int:
    version = project_version((ROOT / "pyproject.toml").read_text())
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(), version)
    if not section:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    if "--dry-run" in sys.argv:
        print(section)
        return 0
    node = "comfyui-reactor-api"
    versions = request("GET", f"{API}/nodes/{node}/versions")
    versions = versions if isinstance(versions, list) else versions.get("versions", [])
    version_id = next((v["id"] for v in versions if v["version"] == version), None)
    if not version_id:
        print(f"The registry has no version {version} yet", file=sys.stderr)
        return 1
    request("PUT", f"{API}/publishers/reactor/nodes/{node}/versions/{version_id}",
            os.environ["REGISTRY_ACCESS_TOKEN"], {"changelog": section})
    print(f"Set the changelog of {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
