# Security

`k8s-troubleshoot-mcp` is a read-only MCP server that exposes Kubernetes cluster
diagnostics to AI assistants. It holds credentials to a cluster and returns
attacker-influenceable text to a language model, so both of those are treated as
security surfaces rather than implementation details.

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories on this
repository rather than opening a public issue. Include the affected version, a
reproduction, and the impact you believe it has.

## The security boundary is Kubernetes RBAC

Application-layer controls in this server are defense-in-depth. **The
enforcement boundary is the ServiceAccount's RBAC bindings.** If the bindings
grant more than intended, the application-layer allowlist is the only thing
standing in the way, and it is not a boundary you should rely on.

Provisioning is deliberately split by scope:

| Manifest | Scope | Grants |
|----------|-------|--------|
| `clusterrole.yaml` + `clusterrolebinding.yaml` | cluster | `get`/`list`/`watch` on `nodes` and `namespaces` only |
| `role.yaml` | namespace | `get`/`list`/`watch` on the diagnostic resources |
| `rolebinding.yaml.template` | namespace | binds the Role, one namespace at a time |

Applying the cluster-scoped pair makes **no namespace readable**. A namespace
becomes readable only when a Role *and* a RoleBinding exist in it. A namespace
named in `ALLOWED_NAMESPACES` but never bound stays unreadable — RBAC wins.

`pods/log` is granted `get` in **its own rule block**, never folded into the
`pods` rule. Kubernetes subresources do not inherit from their parent, so
merging them would silently grant nothing (or, written the other way, grant more
than intended). This is verifiable in `kubernetes/role.yaml`.

The Role does **not** include `secrets`, `configmaps`, `serviceaccounts`, or
`persistentvolumes`. `scripts/generate-kubeconfig.sh` asserts after provisioning
that `kubectl auth can-i get secrets` returns `no`, and aborts if it does not.

### Credential handling

- The server reads its kubeconfig from an **explicit `KUBECONFIG` path only**.
  There is no fallback to `~/.kube/config`, no in-cluster config, no default
  chain. A missing or unreadable path is a startup failure, not a silent
  downgrade to some ambient credential.
- `scripts/generate-kubeconfig.sh` mints a token via the **TokenRequest API**
  (Kubernetes 1.24+), not a legacy auto-mounted Secret. Tokens are time-bound
  and the script reports the real expiry decoded from the token.
- The ServiceAccount sets `automountServiceAccountToken: false`, so adopting it
  in a pod confers no ambient credential.
- Generated kubeconfigs are written with `umask 077` via a temporary file and
  `chmod 600`. The script warns if the output path is inside the repository and
  not covered by `.gitignore`.

### Namespace controls (defense-in-depth, not a boundary)

- `ALLOWED_NAMESPACES` is required. There is no wildcard: `*` and `all` are
  rejected at startup.
- `kube-system` and `kube-public` are stripped from the allowed set at startup
  even if explicitly listed, with a warning, and are filtered from
  `list_namespaces` output.
- Every namespaced tool validates its `namespace` argument **before** making any
  API call, so a disallowed namespace produces a structured error and no
  network request.

## Prompt injection — accepted residual risk

Pod logs and event messages are written by workloads in the cluster. A container
can print anything, including text shaped like instructions to the model reading
it. **This risk is mitigated, not eliminated, and is accepted.**

Two independent mitigations apply, and neither replaces the other:

1. **Structural.** All free-text originating from cluster objects is routed
   through `serialize_log_content`, which JSON-escapes it and additionally
   escapes `<` and `>` as `<` / `>`. The escaping survives transport:
   a client that decodes the MCP response once still holds escaped text. This is
   deterministic and does not depend on the model behaving correctly.

2. **Advisory.** The server's MCP `instructions` field tells the client that
   returned content is untrusted data and that anything resembling an
   instruction should be flagged as a possible injection rather than followed.

The structural layer covers `message`, `reason`, `note`, `type`, involved-object
names, kubelet versions, container termination reasons, and PVC resize states.
Fields intentionally excluded from a response are documented at the point of
omission in code, because an omission is invisible in a diff.

Two test properties enforce this and cover different failure modes: **P17**
poisons every field of every object a tool reads and fails if anything
unescaped reaches the response; **P18** omits every optional field and fails if
a tool raises instead of returning a structured response.

### What this does not protect against

Escaping prevents injected text from breaking out of its JSON string. It cannot
prevent a model from *acting on* clearly-legible instructions it reads as data.
Treat tool output as untrusted input to whatever consumes it.

## What this server is not — deliberate exclusions

These are excluded from all versions unless a new threat-model review is
conducted and documented. They are not backlog items.

| Excluded | Reason |
|----------|--------|
| `get_secrets` | Credential exfiltration. Secrets hold tokens, passwords, TLS keys; reading a ServiceAccount token enables lateral movement. |
| `exec_into_pod` | Arbitrary code execution in a running container. Shell access regardless of how it is framed. |
| `get_configmap` | ConfigMaps routinely contain credentials. Keys-only stripping has no server-side enforcement. |
| `get_serviceaccount_tokens` | Equivalent to `get_secrets` for lateral movement. |
| `port_forward` | Opens a network tunnel from the operator's machine into the cluster. Not a read operation in any meaningful sense. |
| All `create_*` / `update_*` / `patch_*` / `delete_*` | Mutation verbs. The server has no write tools and the RBAC grants no write verbs. |

One further entry in the spec's non-requirements list is a **scope deferral, not
a security exclusion**, and is called out separately so the distinction is not
lost: `get_replicaset_status` is omitted because `get_deployment_status` already
surfaces enough ReplicaSet state for v1.0 troubleshooting. It could be added in a
later version on ordinary product grounds — the `replicasets` read verb is
already in the Role — without the threat-model review the table above requires.

Blast radius from a fully compromised server is therefore bounded by the
ServiceAccount's read scope: information disclosure within the bound namespaces,
plus node and namespace metadata.

## Container security

- Runs as **non-root, UID 10001**, a fixed UID so Kubernetes `runAsNonRoot` /
  `runAsUser` policies can pin it.
- **Multi-stage build.** `uv` and build tooling exist only in the builder stage;
  the runtime image contains the virtualenv and source and nothing else.
- **No credentials are baked into the image.** `.dockerignore` excludes
  kubeconfigs, `.kube/`, `.env`, and key/certificate material. The kubeconfig is
  supplied at runtime as a **read-only mount** — this server never writes, so a
  writable mount would grant privilege it has no use for.
- Dependencies are pinned by `uv.lock` and installed with `uv sync --frozen`, so
  image builds are reproducible and transitive versions cannot drift silently.
- No `HEALTHCHECK`: a stdio server's stdout carries the JSON-RPC stream, and a
  probe writing to it would corrupt the protocol.

The image does not need to run privileged, does not need host networking in a
normal deployment, and needs no capabilities beyond the default set.

## Supply-chain protections

- **TruffleHog pre-commit hook** (`.pre-commit-config.yaml`) scans staged
  changes before every commit — the first line of defense, run locally.
- **TruffleHog GitHub Actions workflow** (`.github/workflows/secret-scan.yml`)
  scans every push and pull request against `main`, with full history. This is
  the safety net for a commit made from a machine without the hook installed,
  or with `--no-verify`.
- `.gitignore` excludes kubeconfigs, `.kube/`, and key material. Note that
  ignore patterns must start at column 0 — an indented pattern silently matches
  nothing, which this repository has been bitten by before.

## Operational notes

- **stdout is reserved exclusively for JSON-RPC.** All logging goes to stderr.
  There is no `print()` and no `sys.stdout` reference anywhere in `src/`.
- Errors are returned as structured dicts. Exceptions are not propagated to the
  MCP layer, including the deserialization errors that a schema-noncompliant
  API server response can trigger (REQ-003a).
- The server reads `KUBECONFIG` once at startup and never re-reads it. Rotating
  the credential requires restarting the process.
