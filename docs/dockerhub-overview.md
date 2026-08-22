# k8s-troubleshoot-mcp

A **read-only** [MCP](https://modelcontextprotocol.io/) server that lets an AI
assistant diagnose a Kubernetes cluster. 16 tools covering pods, logs, events,
workloads, services, endpoints, PVCs, HPAs and nodes.

Read-only is structural, not a promise: there are no write tools, and the
supplied RBAC manifests grant no write verbs.

- **Source:** https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp
- **Security model:** [SECURITY.md](https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp/blob/main/SECURITY.md)

## Quick start

Provision a scoped ServiceAccount and mint a kubeconfig — **do not mount an
admin kubeconfig**:

```bash
git clone https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp.git
cd k8s-troubleshoot-mcp
scripts/generate-kubeconfig.sh /secure/path/k8s-mcp-kubeconfig.yaml staging production
```

Then run:

```bash
docker run -i --rm \
  -v /secure/path/k8s-mcp-kubeconfig.yaml:/kubeconfig:ro \
  -e KUBECONFIG=/kubeconfig \
  -e ALLOWED_NAMESPACES=staging,production \
  yawgyamfiprem32/k8s-troubleshoot-mcp:latest
```

`-i` is required — JSON-RPC travels on stdin/stdout, so the container will
appear to hang. That is correct; it is waiting for a client.

`:ro` is not decoration. This server performs no writes of any kind, so a
writable mount grants privilege it has no use for.

## Claude Desktop

```json
{
  "mcpServers": {
    "k8s-troubleshoot": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/secure/path/k8s-mcp-kubeconfig.yaml:/kubeconfig:ro",
        "-e", "KUBECONFIG=/kubeconfig",
        "-e", "ALLOWED_NAMESPACES=staging,production",
        "yawgyamfiprem32/k8s-troubleshoot-mcp:latest"
      ]
    }
  }
}
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KUBECONFIG` | **Yes** | — | Path to the kubeconfig inside the container. No fallback to `~/.kube/config`, no in-cluster config |
| `ALLOWED_NAMESPACES` | **Yes** | — | Comma-separated namespaces. Wildcards rejected; `kube-system`/`kube-public` stripped |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` — all to stderr |
| `API_TIMEOUT_SECONDS` | No | `30` | Positive integer |
| `MAX_LOG_LINES` | No | `200` | Positive integer, hard ceiling `1000` |

Without `KUBECONFIG` the container exits 1 with a diagnosis on stderr. It fails
closed rather than picking up an ambient credential.

## Image properties

- **Non-root, UID 10001** (fixed, so Kubernetes `runAsNonRoot`/`runAsUser`
  policies can pin it)
- **Multi-stage build** — no `uv`, no build tooling, no apt cache in the runtime
  image
- **No credentials baked in.** The image is code only
- Dependencies pinned by `uv.lock`, installed with `uv sync --frozen`
- No `HEALTHCHECK` — a probe writing to stdout would corrupt the JSON-RPC stream

## Two things that will bite you

**The kubeconfig must be readable by UID 10001.** `generate-kubeconfig.sh`
writes mode `600` owned by your host user, which is correct for a credential,
and bind mounts preserve host ownership. Either grant group/other read, or run
with `--user "$(id -u)"`.

**A cluster on the host's loopback** (minikube, kind) needs `--network host` on
Linux. On Docker Desktop that is not enough — the container joins the Docker
VM's network namespace, not the host's. Point the kubeconfig's `server` at
`host.docker.internal` and add `tls-server-name: localhost` to the cluster
entry, since `host.docker.internal` is not in the API server certificate's SANs.

## Security

The enforcement boundary is **Kubernetes RBAC**, not anything in this
application. Everything the server does in code — the namespace allowlist,
output escaping, structured errors — is defense-in-depth.

There is no `get_secrets`, `get_configmap`, `exec_into_pod`, `port_forward`, or
any mutation tool, and those are excluded from all versions rather than
backlogged.

**Prompt injection is mitigated, not eliminated.** Pod logs and event messages
are written by workloads in the cluster and can contain text shaped like
instructions. Escaping keeps injected text inside its JSON string; it cannot
stop a model acting on instructions it reads as data. Treat tool output as
untrusted input.

Full threat model, exclusion rationale and verification notes:
[SECURITY.md](https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp/blob/main/SECURITY.md).

## License

MIT
