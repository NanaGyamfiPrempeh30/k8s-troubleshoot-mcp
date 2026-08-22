# Publishing

Two destinations, in a required order: **Docker Hub first, MCP Registry second.**
The registry verifies ownership by reading the
`io.modelcontextprotocol.server.name` annotation off the *published* image. If
the image is not on Docker Hub yet, the registry publish fails verification —
it is not a warning you can push past.

## One-time setup

### Docker Hub

1. Create the repository `nanagyamfiprempeh30/k8s-troubleshoot-mcp`.
2. Generate an access token (Account Settings → Personal access tokens) with
   **Read, Write, Delete** scope. Read-only cannot push; write-only cannot
   update the description.
3. Add two GitHub repository secrets (Settings → Secrets and variables →
   Actions):
   - `DOCKERHUB_USERNAME` — the Docker Hub account name
   - `DOCKERHUB_TOKEN` — the access token, **not** the account password

### MCP Registry

Install the publisher CLI:

```bash
# macOS / Linux
brew install mcp-publisher
# or download a release binary from
# https://github.com/modelcontextprotocol/registry/releases
```

The server name `io.github.nanagyamfiprempeh30/k8s-troubleshoot-mcp` claims the
`io.github.nanagyamfiprempeh30` namespace, so publishing requires authenticating
as that GitHub account. No other account can publish under it.

## The identity chain

Four places state the same two identities. They are cross-checked in CI
(`.github/workflows/build-and-push.yml`), so a mismatch fails the build rather
than the publish — but when you change one, change all of them:

| Identity | Stated in |
|----------|-----------|
| Server name `io.github.nanagyamfiprempeh30/k8s-troubleshoot-mcp` | `server.json` → `name`; `Dockerfile` → `LABEL io.modelcontextprotocol.server.name`; workflow → `MCP_SERVER_NAME` |
| Image `docker.io/nanagyamfiprempeh30/k8s-troubleshoot-mcp:<version>` | `server.json` → `packages[0].identifier`; workflow → `IMAGE_NAME` + the version read from `pyproject.toml` |

The version appears in `pyproject.toml` (source of truth), `server.json`'s
`version` **and** its pinned image tag. CI asserts the tag matches the version
being built; it does not assert `server.json`'s own `version` field, so check
that one by eye.

## Release

1. Bump `version` in `pyproject.toml`.
2. Update `server.json`: both `version` and the tag in
   `packages[0].identifier`.
3. Commit and push to `main`. The workflow builds, runs the smoke tests
   (imports, fails closed without a kubeconfig, runs as UID 10001, annotation
   and version pins agree, `server.json` validates against the live schema),
   then pushes `latest`, the version tag, and the short SHA for
   `linux/amd64` + `linux/arm64`, and refreshes the Docker Hub description from
   `docs/dockerhub-overview.md`.
4. Confirm the image is live and carries the annotation:

   ```bash
   docker buildx imagetools inspect \
     nanagyamfiprempeh30/k8s-troubleshoot-mcp:1.0.0 --format '{{ json .Manifest }}' \
     | grep io.modelcontextprotocol.server.name
   ```

5. Publish the registry listing:

   ```bash
   mcp-publisher login github
   mcp-publisher publish
   ```

   It reads `server.json` from the working directory, pulls the image
   annotation, and rejects the publish if it disagrees with `name`.

6. Confirm the listing resolves:

   ```bash
   curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=k8s-troubleshoot-mcp" | jq
   ```

**mcp.so needs no separate submission** — it mirrors the official MCP Registry.
Listings appear there after the registry publish propagates. Same for other
downstream directories that consume the registry feed.

## Verifying the published image is what you think it is

The image is the thing operators point a cluster credential at, so it is worth
one direct check rather than trusting the build log:

```bash
docker run --rm nanagyamfiprempeh30/k8s-troubleshoot-mcp:1.0.0; echo "exit=$?"
# expect: exit=1, a one-line KUBECONFIG diagnosis on stderr, nothing on stdout

docker run --rm --entrypoint id nanagyamfiprempeh30/k8s-troubleshoot-mcp:1.0.0 -u
# expect: 10001
```

The first check is the important one. It confirms the image fails closed rather
than falling back to an ambient credential, which is the property that would be
least visible if it broke.
