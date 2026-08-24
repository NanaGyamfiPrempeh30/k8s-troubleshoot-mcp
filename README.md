# k8s-troubleshoot-mcp

A read-only [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server that lets an AI assistant diagnose a Kubernetes cluster. Ask why a pod is
crash-looping instead of running six `kubectl` commands and correlating the
output by hand.

**Read-only is a structural property, not a promise.** There are no write tools,
and the RBAC manifests grant no write verbs. See [Security model](#security-model).

## What It Does

The server exposes 16 diagnostic tools over stdio. Connect it to Claude Desktop,
VS Code, or Kiro, and you can ask things like:

- "Why is the `checkout` pod in `staging` not ready?"
- "Show me the last 50 lines of logs from the `api` container"
- "Which nodes are not Ready, and what are their taints?"
- "Is the `web` HPA scaling, and what does its current metric say?"
- "What events fired in `production` in the last few minutes?"

The assistant calls the tools, the server queries the Kubernetes API with a
scoped ServiceAccount token, and every response comes back as a structured dict —
including errors, which are never raised as exceptions into the MCP layer.

## Architecture

```
┌──────────────────┐    stdio (JSON-RPC)   ┌──────────────────────────┐
│   MCP Client     │◄─────────────────────►│   MCP Server             │
│  Claude Desktop  │                       │   (this project)         │
│  VS Code / Kiro  │                       │                          │
└──────────────────┘                       │  config.py   ─ validate  │
                                           │  server.py   ─ 16 tools  │
                                           │  tools/*.py  ─ read+shape│
                                           │  response.py ─ escape +  │
                                           │                structure │
                                           └───────────┬──────────────┘
                                                       │ HTTPS, explicit
                                                       │ KUBECONFIG only
                                           ┌───────────▼──────────────┐
                                           │  Kubernetes API server   │
                                           │  ── RBAC boundary ──     │
                                           │  ServiceAccount:         │
                                           │  get/list/watch only     │
                                           └──────────────────────────┘
```

Configuration is validated once at startup. If anything is wrong — `KUBECONFIG`
unset, the file unreadable or malformed, `ALLOWED_NAMESPACES` missing or
containing a wildcard — the process writes one line to stderr and exits 1. It
never starts in a partially-valid state.

## Available Tools

Arguments marked `?` are optional.

| Tool | Description | Parameters |
|------|-------------|------------|
| `get_pod_status` | Phase, conditions, container statuses, QoS class and node for a pod | `pod_name`, `namespace` |
| `get_pod_logs` | Recent log lines from a pod container. Content is untrusted — see [Reading `get_pod_logs` output](#reading-get_pod_logs-output) | `pod_name`, `namespace`, `container?`, `previous?`, `tail_lines?` |
| `get_pod_events` | Recent events for a pod, newest first, with `total_available` | `pod_name`, `namespace` |
| `list_pods` | Pods in a namespace with phase, restart count and readiness | `namespace`, `label_selector?` |
| `get_node_status` | Conditions, capacity, allocatable, taints and roles for a node | `node_name` |
| `list_nodes` | Cluster nodes with readiness, roles, age and kubelet version | none |
| `get_deployment_status` | Replica counts, conditions and rollout strategy | `deployment_name`, `namespace` |
| `list_deployments` | Deployments in a namespace with replica counts and availability | `namespace` |
| `get_statefulset_status` | Replica counts, revisions and update strategy | `statefulset_name`, `namespace` |
| `get_daemonset_status` | Scheduling counts and update strategy | `daemonset_name`, `namespace` |
| `get_service` | Type, ClusterIP, ports, selector and ready endpoint count | `service_name`, `namespace` |
| `get_endpoints` | Ready and not-ready endpoint addresses backing a service | `service_name`, `namespace` |
| `get_pvc_status` | Phase, capacity, binding and resize state for a PVC | `pvc_name`, `namespace` |
| `get_hpa_status` | Replica bounds, current metrics and conditions for an HPA | `hpa_name`, `namespace` |
| `get_namespace_events` | Recent events across a namespace, newest first, with `total_available` | `namespace`, `limit?` |
| `list_namespaces` | The namespaces this server is permitted to read | none |

Every namespaced tool validates its `namespace` argument **before** making any
API call, so a disallowed namespace produces a structured error and no network
request.

### Deliberately absent

No `get_secrets`, `get_configmap`, `exec_into_pod`, `port_forward`, or any
`create`/`update`/`patch`/`delete` tool. These are excluded from all versions
unless a new threat-model review is conducted and documented — they are not
backlog items. The reasoning for each is in
[SECURITY.md](SECURITY.md#what-this-server-is-not--deliberate-exclusions).

## Security model

Full detail is in [SECURITY.md](SECURITY.md). The summary:

### The boundary is Kubernetes RBAC

**Everything this server does in application code is defense-in-depth. The
enforcement boundary is the ServiceAccount's RBAC bindings.** If the bindings
grant more than intended, the application-layer allowlist is all that stands in
the way, and it is not a boundary you should rely on.

Provisioning is split by scope so that the cluster-scoped grant is minimal:

| Manifest | Scope | Grants |
|----------|-------|--------|
| `clusterrole.yaml` + `clusterrolebinding.yaml` | cluster | `get`/`list`/`watch` on `nodes` and `namespaces` only |
| `role.yaml` | namespace | `get`/`list`/`watch` on the diagnostic resources |
| `rolebinding.yaml.template` | namespace | binds the Role, one namespace at a time |

Applying the cluster-scoped pair makes **no namespace readable**. A namespace
becomes readable only when a Role *and* a RoleBinding exist in it. A namespace
listed in `ALLOWED_NAMESPACES` but never bound stays unreadable — RBAC wins.

`pods/log` is granted in its own rule block, never folded into the `pods` rule,
because Kubernetes subresources do not inherit from their parent.

### Defense-in-depth layers

| Layer | What it does | What it is not |
|-------|--------------|----------------|
| **RBAC** | Grants read verbs on diagnostic resources in bound namespaces only | — this *is* the boundary |
| **Explicit kubeconfig** | Reads `KUBECONFIG` from an exact path; no `~/.kube/config`, no in-cluster config, no fallback chain | Not a permission check — it prevents silently picking up an ambient credential |
| **Namespace allowlist** | Rejects wildcards, strips `kube-system`/`kube-public`, validates before every call | Advisory; a bug here is contained by RBAC |
| **Output escaping** | All cluster-authored free text routed through `serialize_log_content` | Prevents breaking out of a JSON string; cannot stop a model acting on legible instructions |
| **Structured errors** | Every failure returns a dict; no exception reaches the MCP layer | — |

### Prompt injection is mitigated, not eliminated

Pod logs and event messages are written by workloads in the cluster. A container
can print anything, including text shaped like instructions to the model reading
it. Escaping keeps injected text inside its JSON string; it cannot stop a model
from acting on instructions it reads as data. **Treat tool output as untrusted
input to whatever consumes it.** This residual risk is accepted and documented.

## Prerequisites

1. **A Kubernetes cluster** and a `kubectl` context with enough permission to
   create a ServiceAccount, Role, RoleBinding, ClusterRole and
   ClusterRoleBinding — you need this once, to provision. The server itself
   never uses your admin credential.
2. **Kubernetes 1.24+** — `scripts/generate-kubeconfig.sh` mints a token via the
   TokenRequest API, not a legacy auto-mounted Secret.
3. **Python 3.11+**
4. **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh` (or use Docker,
   which needs neither Python nor uv on the host)

## Setup

```bash
git clone https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp.git
cd k8s-troubleshoot-mcp

# Install dependencies (uv creates .venv automatically)
uv sync

# Run the test suite
uv run pytest tests/ -q
```

### Provision RBAC and mint a kubeconfig

```bash
scripts/generate-kubeconfig.sh /secure/path/k8s-mcp-kubeconfig.yaml staging production
```

The first argument is where to write the kubeconfig; the rest are the namespaces
the server may read. Pass the same set you intend to put in
`ALLOWED_NAMESPACES` — RBAC is the enforcement boundary, and a namespace bound
here but absent from the allowlist (or the reverse) is a mismatch between real
permission and configured capability.

The script applies the cluster-scoped manifests together, then applies
`role.yaml` with an explicit `-n <namespace>` and renders a RoleBinding per
namespace. On success it prints the kubeconfig path to stdout and nothing else;
all diagnostics go to stderr. It also asserts after provisioning that
`kubectl auth can-i get secrets` returns `no`, and aborts if it does not.

> **Do not run `kubectl apply -f kubernetes/`.** It does not fail — it reports
> success while creating `role.yaml` in the *current* namespace and skipping
> `rolebinding.yaml.template` entirely, because `kubectl apply -f <dir>` only
> reads `.yaml`/`.yml`/`.json`. The result is a server that looks provisioned
> and can read nothing. Verified against a v1.35 API server with
> `--dry-run=server`: 5 resources applied, not 6.

The generated kubeconfig is written with `umask 077` and `chmod 600`. Keep it
out of the repository — the script warns if the output path is inside a
repository and not covered by `.gitignore`.

### Run it

```bash
KUBECONFIG=/secure/path/k8s-mcp-kubeconfig.yaml \
ALLOWED_NAMESPACES=staging,production \
uv run k8s-troubleshoot-mcp
```

The server speaks JSON-RPC on stdin/stdout, so it will appear to hang — that is
correct. It is waiting for a client.

## Connecting to Claude Desktop

Add to `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "k8s-troubleshoot": {
      "command": "uv",
      "args": ["run", "k8s-troubleshoot-mcp"],
      "cwd": "/path/to/k8s-troubleshoot-mcp",
      "env": {
        "KUBECONFIG": "/secure/path/k8s-mcp-kubeconfig.yaml",
        "ALLOWED_NAMESPACES": "staging,production"
      }
    }
  }
}
```

Restart Claude Desktop fully (quit from the system tray, reopen), then check
Settings → Developer → `k8s-troubleshoot` shows **running**.

On Windows, if the server shows as disconnected, use a batch file wrapper —
Claude Desktop has working-directory issues with direct interpreter invocation:

```bat
@echo off
cd /d C:\Users\YourUsername\k8s-troubleshoot-mcp
uv run k8s-troubleshoot-mcp
```

```json
{
  "mcpServers": {
    "k8s-troubleshoot": {
      "command": "cmd.exe",
      "args": ["/c", "C:\\Users\\YourUsername\\k8s-troubleshoot-mcp\\run_mcp.bat"],
      "env": {
        "KUBECONFIG": "C:\\secure\\path\\k8s-mcp-kubeconfig.yaml",
        "ALLOWED_NAMESPACES": "staging,production"
      }
    }
  }
}
```

## Running with Docker

```bash
docker build -t k8s-troubleshoot-mcp .

docker run -i --rm \
  -v /secure/path/k8s-mcp-kubeconfig.yaml:/kubeconfig:ro \
  -e KUBECONFIG=/kubeconfig \
  -e ALLOWED_NAMESPACES=staging,production \
  k8s-troubleshoot-mcp
```

`-i` is required — JSON-RPC travels on stdin/stdout. `:ro` is not decoration:
this server performs no writes of any kind, so a writable mount would grant
privilege it has no use for.

The image runs as **non-root, UID 10001**, and contains no credentials.

Two things that will bite you:

- **The kubeconfig must be readable by UID 10001.** A file created mode `600`
  and owned by your host user is not, and bind mounts preserve host ownership.
  Either grant group/other read, or run with `--user "$(id -u)"`.
- **A cluster on the host's loopback** (minikube, kind) needs `--network host`
  on Linux. On Docker Desktop that is not enough — see
  [Local testing with minikube](#local-testing-with-minikube).

To use the container from Claude Desktop, set `"command": "docker"` and put the
whole `run -i --rm …` invocation in `"args"`.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KUBECONFIG` | **Yes** | — | Exact path to the kubeconfig. No fallback to `~/.kube/config` and no in-cluster config; a missing, unreadable or malformed file is a startup failure |
| `ALLOWED_NAMESPACES` | **Yes** | — | Comma-separated namespaces the server may read. Wildcards (`*`, `all`) are rejected; `kube-system` and `kube-public` are stripped with a warning even if listed |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. An unrecognized value falls back to `INFO` |
| `API_TIMEOUT_SECONDS` | No | `30` | Must be a positive integer |
| `MAX_LOG_LINES` | No | `200` | Must be a positive integer. Clamped to a hard ceiling of `1000` with a warning |

All logging goes to **stderr**. stdout carries the JSON-RPC stream exclusively —
a single stray `print()` would corrupt the protocol, so there are none in `src/`.

## Reading `get_pod_logs` output

The `content` field is **escaped log text, not log text**. One `json.loads` of
the MCP response is not enough to get printable output.

`serialize_log_content` escapes the log before the response envelope is
JSON-encoded, so the escaping survives transport. Decoding the response undoes
only the transport layer:

```python
resp = json.loads(raw_mcp_response)
content = resp["data"]["content"]

print(content)
# line one\nline two\tsaid \"hi\" \u003cb\u003e\n   <- one physical line

content.count("\n")   # 0  -- there are no real newlines in it
```

To recover the original text, decode the field a second time:

```python
text = json.loads('"' + content + '"')

print(text)
# line one
# line two    said "hi" <b>
```

This is intentional, not a bug. The escaping is the structural prompt-injection
mitigation (REQ-020, REQ-027) — if a single decode restored real control
characters and raw `<`, the mitigation would be gone before the content reached
a model. Decode the second time only where the output is going somewhere that
will not interpret it, such as a terminal or a log file.

In MCP Inspector's raw view you will see `\\n` (two backslashes). That is
correct: the transport layer escaping the backslash of an already-escaped `\n`.

**Do not compute a line count from `content`.** Splitting it on a newline
returns 1 for a log of any length. Use `lines_returned`, which is why it exists.

## Local testing with minikube

### Stale kubeconfig after a minikube restart

minikube exposes the API server through a proxy address whose port is assigned
per session. That address — visible via `kubectl cluster-info` — can change
across `minikube stop` / `minikube start` cycles, and across WSL2 restarts.

A kubeconfig minted by `scripts/generate-kubeconfig.sh` during a previous
session pins the old `host:port`. The server will start normally and then fail
every tool call with a `connection_error` naming an endpoint nothing is
listening on:

```
Kubernetes control plane is running at https://127.0.0.1:54489
                                                        ^^^^^ per-session
```

To recover:

1. Confirm the current endpoint:

   ```bash
   kubectl cluster-info
   ```

2. Mint a fresh kubeconfig against it:

   ```bash
   scripts/generate-kubeconfig.sh /path/to/kubeconfig.yaml <namespace> [namespace...]
   ```

3. **Fully restart the MCP server process.** The server reads `KUBECONFIG` once
   at startup (REQ-002) and never re-reads it, so overwriting the file
   underneath a running server changes nothing. There is also no fallback to
   `~/.kube/config` (REQ-003) — a working `kubectl` on the same machine will
   not rescue a stale kubeconfig.

The same failure looks identical whether the cause is a stale port, a revoked
token, or a genuinely unreachable cluster. `kubectl cluster-info` distinguishes
them: if it succeeds while the server reports `connection_error`, the
kubeconfig is stale.

### Reaching minikube from a container on Docker Desktop

`--network host` joins the **Docker VM's** network namespace, not the WSL
distribution's, so the published minikube port is not on that loopback and the
container gets `ConnectionRefused`. `host.docker.internal` is reachable, but
rewriting the kubeconfig's `server` to it fails TLS verification —
`host.docker.internal` is not among the minikube API server certificate's SANs.

Point the URL at `host.docker.internal` **and** add `tls-server-name` to the
cluster entry, which is the kubeconfig field that exists for exactly this:

```yaml
clusters:
- name: minikube
  cluster:
    server: https://host.docker.internal:54489
    tls-server-name: localhost
    certificate-authority-data: ...
```

## Project Structure

```
k8s-troubleshoot-mcp/
├── .github/workflows/
│   ├── build-and-push.yml       # Docker build + push to Docker Hub
│   └── secret-scan.yml          # TruffleHog secret scanning
├── kubernetes/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml      # automountServiceAccountToken: false
│   ├── clusterrole.yaml         # nodes + namespaces, read verbs only
│   ├── clusterrolebinding.yaml
│   ├── role.yaml                # pods/log in its own rule block
│   └── rolebinding.yaml.template
├── scripts/
│   ├── generate-kubeconfig.sh   # provisions RBAC, mints a scoped token
│   └── check-namespaces.py      # CI guard: GitHub vs Docker Hub handles
├── docs/
│   ├── PUBLISHING.md            # Docker Hub + MCP Registry release runbook
│   └── dockerhub-overview.md    # Docker Hub repository description
├── server.json                  # MCP Registry listing metadata
├── src/k8s_troubleshoot_mcp/
│   ├── __main__.py              # startup sequence, fail-closed
│   ├── config.py                # env validation (REQ-001..010, 069..071)
│   ├── k8s_client.py            # explicit-path client factory
│   ├── server.py                # FastMCP instance + 16 tool registrations
│   ├── response.py              # serialize_log_content + structured errors
│   ├── pagination.py            # total_available / continue-token detection
│   └── tools/                   # pods, nodes, workloads, services,
│                                #   storage, autoscaling, events, namespaces
├── tests/
│   ├── unit/
│   └── property/                # P1-P18, Hypothesis-driven
├── requirements.md              # EARS-format requirements
├── design.md                    # architecture + 18 correctness properties
├── SECURITY.md                  # threat model, RBAC boundary, exclusions
├── DEBUG_LOG.md                 # every error found, root cause, resolution
├── Dockerfile                   # multi-stage, non-root UID 10001
├── uv.lock                      # 67 pinned dependencies
└── README.md                    # this file
```

## Troubleshooting

**`KUBECONFIG environment variable is not set`** — the server refuses to start
without an explicit path and will not fall back to `~/.kube/config`. This is
deliberate (REQ-003). Run `scripts/generate-kubeconfig.sh` if you have not yet.

**`is not a valid kubeconfig`** — the file exists and is readable but is
malformed. The message names the path, line and column, never the file's
contents; a kubeconfig holds a bearer token.

**`connection_error` on every tool while `kubectl` works** — a stale kubeconfig.
See [Local testing with minikube](#local-testing-with-minikube).

**`namespace_not_allowed`** — the namespace is not in `ALLOWED_NAMESPACES`, or
it is `kube-system`/`kube-public`, which are stripped at startup even if listed.

**`kubernetes_api_error` with `http_status: 403` while the namespace *is*
allowed** — the allowlist and the RBAC bindings have diverged.
`ALLOWED_NAMESPACES` grants nothing; a namespace is only readable once a Role
and RoleBinding exist in it. Re-run `generate-kubeconfig.sh` with the full
namespace set.

Errors come back as one of three codes: `namespace_not_allowed` (rejected before
any API call), `kubernetes_api_error` (carries `http_status` and `reason` from
the API server), and `connection_error`.

**Permission denied reading the kubeconfig in Docker** — the container runs as
UID 10001 and bind mounts preserve host ownership. See
[Running with Docker](#running-with-docker).

**Log content looks like one long line with `\n` in it** — that is the escaping
working. See [Reading `get_pod_logs` output](#reading-get_pod_logs-output).

**MCP Inspector shows `Logging ✗`** — expected, not a defect. FastMCP does not
register a `set_logging_level` handler, so `get_capabilities()` omits the
capability. That MCP feature sends log records to the *client*; this server logs
to stderr, which is unrelated. Inspector shows Resources and Prompts as
supported for the mirror-image reason — FastMCP registers those handlers
unconditionally even though none are defined.

## How this was built

[DEBUG_LOG.md](DEBUG_LOG.md) records every error encountered during development
— root cause and resolution for each, including several found only by running
against a real cluster after the entire test suite was green.

It is worth reading if you are evaluating whether to trust this server with a
cluster credential, because a recurring theme runs through it: **the test mocks
agreed with each other rather than with the cluster.** Five separate defects
were found that way, each with a passing test asserting the opposite. The
verification steps taken in response are summarized in
[SECURITY.md](SECURITY.md#how-the-claims-in-this-document-were-verified).

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -q
```

The property tests (`tests/property/`) enumerate tools from a shared registry,
`NAMESPACED_TOOLS` in `tests/property/strategies.py`. Any new tool must be added
there in the same change — a tool missing from the registry causes P4/P6/P7 to
silently stop covering it while still reporting green.

Install the TruffleHog pre-commit hook before your first commit (`pre-commit` is
not a project dependency — it is a developer tool installed alongside):

```bash
pip install pre-commit
pre-commit install
```

## Roadmap

- [x] 16 read-only diagnostic tools
- [x] 18 correctness properties (P1-P18), Hypothesis-driven
- [x] RBAC manifests + scoped kubeconfig generation
- [x] Live-cluster validation
- [x] Docker packaging (multi-stage, non-root, no baked credentials)
- [x] TruffleHog secret scanning (pre-commit + GitHub Actions)
- [x] Docker Hub + MCP Registry listing prepared ([docs/PUBLISHING.md](docs/PUBLISHING.md))
- [ ] Migrate `get_endpoints` to `discovery.k8s.io/v1` EndpointSlice
- [ ] Publish to Smithery
- [ ] `get_ingress_status` and `get_networkpolicy` tools
- [ ] HTTP transport for network-based deployment

## Credits

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [FastMCP](https://github.com/jlowin/fastmcp)
- [Kubernetes Python client](https://github.com/kubernetes-client/python)
- Built by [Yaw Nana Gyamfi Prempeh](https://github.com/NanaGyamfiPrempeh30)

## License

MIT
