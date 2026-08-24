# Publishing

Two destinations, in a required order: **Docker Hub first, MCP Registry second.**
The registry verifies ownership by reading the
`io.modelcontextprotocol.server.name` annotation off the *published* image. If
the image is not on Docker Hub yet, the registry publish fails verification —
it is not a warning you can push past.

## One-time setup

### Docker Hub

1. Create the repository `yawgyamfiprem32/k8s-troubleshoot-mcp`.
2. Generate an access token (Account Settings → Personal access tokens) with
   **Read, Write, Delete** scope. Read-only cannot push; write-only cannot
   update the description.
3. Add two GitHub repository secrets (Settings → Secrets and variables →
   Actions):
   - `DOCKERHUB_USERNAME` — `yawgyamfiprem32` (the Docker Hub account, **not**
     the GitHub one)
   - `DOCKERHUB_TOKEN` — the access token, **not** the account password

### MCP Registry

Submission is a **CLI tool** — `mcp-publisher`. It is not a pull request to a
registry repository, and you do not hand-write an API call. Install it with
Homebrew, or fetch the release binary:

```bash
brew install mcp-publisher
```

```bash
# macOS / Linux, no Homebrew
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" \
  | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/
```

Confirm it is on PATH — the subcommands are `init`, `login`, `logout`,
`publish`:

```bash
mcp-publisher --help
```

`mcp-publisher init` generates a `server.json` template. **Do not run it here**
— `server.json` already exists, is hand-written for the OCI package type, and
is schema-validated in CI. `init` would overwrite it with an npm-shaped default.

The server name `io.github.NanaGyamfiPrempeh30/k8s-troubleshoot-mcp` claims the
`io.github.NanaGyamfiPrempeh30` namespace, so publishing requires authenticating
as that GitHub account. No other account can publish under it — attempting it
returns "You do not have permission to publish this server".

**Match the login's casing exactly.** On login the registry mints a JWT
granting `io.github.<login>/*`, where `<login>` is the login string the GitHub
API returns — `NanaGyamfiPrempeh30`, not a lowercased form. The publish handler
then tests `server.json`'s `name` against that pattern with a case-sensitive
prefix match, so a lowercased name is schema-valid (the schema's pattern is
`^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$`, which permits either) and still returns the
same 403 as an unauthorized account. `scripts/check-namespaces.py` pins the
canonical casing repo-wide for this reason.

## The identity chain

> **Two different accounts, and they are not interchangeable.** The GitHub
> handle is `NanaGyamfiPrempeh30`; the Docker Hub handle is `yawgyamfiprem32`.
> The rule is mechanical: anything under `io.github.*` or a `github.com` URL
> takes the **GitHub** handle, and anything under `docker.io/*` or naming a
> Docker Hub repository path takes the **Docker Hub** handle. Substituting one
> for the other produces a value that looks entirely plausible and fails only
> at push or publish time.
>
> `scripts/check-namespaces.py` enforces this mechanically and runs as the
> first step of the build workflow. It pins the literal namespaces rather than
> comparing the declared values to each other, because two values that are both
> wrong in the same direction still agree — that is exactly how the GitHub
> handle occupied every `docker.io/` path here without any check noticing. It
> also scans the whole tree, so a bare `docker run <handle>/…` in prose is
> caught too. Run it locally any time: `python3 scripts/check-namespaces.py`.

Four places state the same two identities. They are cross-checked in CI
(`.github/workflows/build-and-push.yml`), so a mismatch fails the build rather
than the publish — but when you change one, change all of them:

| Identity | Stated in |
|----------|-----------|
| Server name `io.github.NanaGyamfiPrempeh30/k8s-troubleshoot-mcp` | `server.json` → `name`; `Dockerfile` → `LABEL io.modelcontextprotocol.server.name`; workflow → `MCP_SERVER_NAME` |
| Image `docker.io/yawgyamfiprem32/k8s-troubleshoot-mcp:<version>` | `server.json` → `packages[0].identifier`; workflow → `IMAGE_NAME` + the version read from `pyproject.toml` |

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
     yawgyamfiprem32/k8s-troubleshoot-mcp:1.0.0 --format '{{ json .Manifest }}' \
     | grep io.modelcontextprotocol.server.name
   ```

5. Authenticate. This is a **GitHub device flow and is interactive** — it
   prints a code, you enter it in a browser. It cannot run unattended, which is
   why this step is not in the workflow:

   ```bash
   mcp-publisher login github
   ```

   ```text
   To authenticate, please:
   1. Go to: https://github.com/login/device
   2. Enter code: ABCD-1234
   3. Authorize this application
   ```

6. Publish the listing, from the repository root so `server.json` is found:

   ```bash
   mcp-publisher publish
   ```

   The registry hosts **metadata only, never artifacts**. It resolves the image
   named in `packages[0].identifier`, reads its
   `io.modelcontextprotocol.server.name` annotation, and rejects the publish if
   that disagrees with `name` — which is why the Docker Hub push must already
   have happened. A failure here reads
   "Registry validation failed for package".

7. Confirm the listing resolves:

   ```bash
   curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.NanaGyamfiPrempeh30/k8s-troubleshoot-mcp" | jq
   ```

## About mcp.so and other directories

The official registry is explicitly designed as a source that sub-registries and
directories consume, and it is open source so anyone can build a compatible one.
That is the mechanism by which a listing propagates outward.

**Whether mcp.so specifically auto-ingests from it, or wants its own submission,
is not confirmed here** — the site blocks automated fetches, so this was not
verified. Treat it as: publish to the official registry first, then check
mcp.so after a few days, and submit manually there only if the listing has not
appeared. Do not assume it is automatic.

## Automating the registry publish

There is an official GitHub Action for `mcp-publisher`, documented at
<https://modelcontextprotocol.io/registry/github-actions>. It exists because the
device flow above cannot run unattended. This repository does **not** use it —
the registry publish is deliberately a manual step so that pushing an image and
announcing it to the world stay separate decisions. Worth revisiting if releases
become frequent.

## Verifying the published image is what you think it is

The image is the thing operators point a cluster credential at, so it is worth
one direct check rather than trusting the build log:

```bash
docker run --rm yawgyamfiprem32/k8s-troubleshoot-mcp:1.0.0; echo "exit=$?"
# expect: exit=1, a one-line KUBECONFIG diagnosis on stderr, nothing on stdout

docker run --rm --entrypoint id yawgyamfiprem32/k8s-troubleshoot-mcp:1.0.0 -u
# expect: 10001
```

The first check is the important one. It confirms the image fails closed rather
than falling back to an ambient credential, which is the property that would be
least visible if it broke.
