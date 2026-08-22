#!/usr/bin/env python3
"""Fail if the GitHub and Docker Hub namespaces have been transposed.

Two different accounts own the two publishing identities:

    GitHub     nanagyamfiprempeh30   -> io.github.* values, github.com URLs
    Docker Hub yawgyamfiprem32       -> docker.io/* paths, Docker Hub repo paths

Substituting one for the other produces a value that is syntactically valid,
reads correctly, and fails only at `docker push` or `mcp-publisher publish`.

The workflow's other cross-checks compare these values to *each other* —
server.json's identifier against IMAGE_NAME, the Dockerfile label against
MCP_SERVER_NAME. Those pass when both sides are wrong in the same direction,
which is exactly how this mixup survived until it was found by hand. This
script pins the literal namespaces instead, so agreement is not sufficient.

Run directly (`python3 scripts/check-namespaces.py`) or via CI. Exit 0 clean,
exit 1 with one `::error::` line per problem.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

GITHUB_NS = "nanagyamfiprempeh30"
DOCKERHUB_NS = "yawgyamfiprem32"

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github/workflows/build-and-push.yml"

SKIP_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", ".hypothesis",
             "node_modules", ".mypy_cache", ".ruff_cache"}

problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def require_prefix(label: str, value: str, prefix: str) -> None:
    if not value.startswith(prefix):
        fail(f"{label} must start with '{prefix}' — got '{value}'")


# --------------------------------------------------------------- declared values

server = json.loads((REPO / "server.json").read_text(encoding="utf-8"))

require_prefix(
    "server.json name",
    server["name"],
    f"io.github.{GITHUB_NS}/",
)
require_prefix(
    "server.json packages[0].identifier",
    server["packages"][0]["identifier"],
    f"docker.io/{DOCKERHUB_NS}/",
)

workflow_text = WORKFLOW.read_text(encoding="utf-8")


def workflow_value(key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$", workflow_text, re.M)
    if not m:
        fail(f"could not find '{key}' in {WORKFLOW.relative_to(REPO)}")
        return ""
    return m.group(1)


require_prefix("workflow IMAGE_NAME", workflow_value("IMAGE_NAME"),
               f"docker.io/{DOCKERHUB_NS}/")
require_prefix("workflow MCP_SERVER_NAME", workflow_value("MCP_SERVER_NAME"),
               f"io.github.{GITHUB_NS}/")
require_prefix("workflow dockerhub-description repository",
               workflow_value("repository"), f"{DOCKERHUB_NS}/")

label = re.search(r'io\.modelcontextprotocol\.server\.name="([^"]*)"',
                  (REPO / "Dockerfile").read_text(encoding="utf-8"))
if label is None:
    fail("Dockerfile is missing the io.modelcontextprotocol.server.name label")
else:
    require_prefix("Dockerfile io.modelcontextprotocol.server.name",
                   label.group(1), f"io.github.{GITHUB_NS}/")

# ------------------------------------------------------- repo-wide transposition

# The GitHub handle appears in exactly two shapes: mixed-case inside github.com
# URLs, and lowercase inside `io.github.*`. A lowercase occurrence followed by
# `/` and NOT preceded by `io.github.` is therefore always a Docker Hub position
# holding the wrong handle — which is what `docker run nanagyamfiprempeh30/...`
# in the docs looked like.
GH_IN_DOCKER_POSITION = re.compile(rf"(?<!io\.github\.){re.escape(GITHUB_NS)}/")
DH_IN_GITHUB_POSITION = re.compile(
    rf"io\.github\.{re.escape(DOCKERHUB_NS)}|github\.com/{re.escape(DOCKERHUB_NS)}",
    re.I,
)

SELF = Path(__file__).resolve()


def walk_files():
    """Yield repo files, pruning SKIP_DIRS during traversal.

    Pruning matters rather than filtering afterwards: rglob descends into a
    directory before any filter sees it, and .venv on a /mnt/c mount takes
    minutes to walk.
    """
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


for path in walk_files():
    if path.resolve() == SELF:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue

    rel = path.relative_to(REPO)
    for lineno, line in enumerate(text.splitlines(), 1):
        if GH_IN_DOCKER_POSITION.search(line):
            fail(f"{rel}:{lineno}: GitHub handle '{GITHUB_NS}' in a Docker Hub "
                 f"position (expected '{DOCKERHUB_NS}'): {line.strip()[:120]}")
        if DH_IN_GITHUB_POSITION.search(line):
            fail(f"{rel}:{lineno}: Docker Hub handle '{DOCKERHUB_NS}' in a GitHub "
                 f"position (expected '{GITHUB_NS}'): {line.strip()[:120]}")

# ---------------------------------------------------------------------- verdict

if problems:
    for p in problems:
        print(f"::error::{p}", file=sys.stderr)
    print(f"\n{len(problems)} namespace problem(s). "
          f"GitHub={GITHUB_NS}, Docker Hub={DOCKERHUB_NS}.", file=sys.stderr)
    sys.exit(1)

print(f"OK — GitHub namespace '{GITHUB_NS}' and Docker Hub namespace "
      f"'{DOCKERHUB_NS}' are used in the right positions everywhere.")
