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
- **Startup errors never quote the kubeconfig's contents.** A file that exists
  and is readable but is malformed is reported as a single line naming the path
  and the parser's reason, never the offending source line (REQ-002a).
  Including the reason at all was only made safe by first establishing that
  PyYAML omits its source snippet when reading from a file handle —
  `MarkedYAMLError` *can* carry one, and in a kubeconfig the offending line may
  be the bearer token.
  `tests/unit/test_main.py::TestReq002aMalformedKubeconfig::test_message_never_quotes_the_file`
  plants a JWT-shaped secret on the malformed line across five malformation
  types and asserts it is absent from stderr, so a library change that starts
  quoting lines fails the build instead of leaking a credential.

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

### What a green escaping suite does not prove

P17 is treated as evidence for one property, not as proof of correctness. It has
two known blind spots and both have produced real defects in this repository:

- **Omission.** A field the response model leaves out emits no poison and so
  passes silently — P17 cannot distinguish "escaped correctly" from "never
  included". This is why every deliberate exclusion carries a comment at the
  point of omission in code, naming the REQ that excludes it. The two mechanisms
  do not substitute for each other.
- **Faithful-looking corruption.** `get_pod_logs` once returned `str(bytes)`
  rather than decoded text. P17 passed both before and after the fix, because
  `str()` backslash-escapes control characters — nothing raw reached the
  response. The corruption itself is what made the property pass.

P17's first run also found four fields reaching responses unescaped that had
each been assumed to carry a control-plane-authored enum, and do not: container
termination reasons (written by the CRI runtime), event `reason` and `type`
(written by any controller in the cluster, including third-party operators), and
`kubelet_version` (self-reported by the node). REQ-021a, REQ-030a and REQ-035a
record why each is not enum-safe. A `reason` field is judged low-risk **per
tool, never globally** — a provisioner- or CRD-authored `reason` carries none of
the guarantees a core controller's does.

The `not_yet_escaped` allowlist category exists so a gap can be recorded, but
the design treats any entry in it as a stop-ship signal rather than a steady
state, and a test enforces that. There are currently no entries.

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

## How the claims in this document were verified

The controls above are asserted here and enforced in code, but neither of those
is evidence they work. Each was checked against something that could contradict
it, and `DEBUG_LOG.md` records every error found in the process — root cause and
resolution — including errors made while drafting this document.

- **Against a live cluster, not against mocks.** Five defects were found only by
  running against a real API server or a schema-driven fake, and every one had a
  passing test asserting the opposite. The recurring cause is that the mocks
  agreed with each other rather than with the cluster: a `MagicMock` never
  invokes the generated model setters, so a test built on one can assert a
  response shape the real client cannot produce. One of these bypassed the
  no-exceptions-to-the-MCP-layer rule entirely, raising inside the client library
  during deserialization before any tool code ran. Where a defect of this class
  was fixed, the wrong shape was **removed** from the suite rather than
  supplemented with a correct case.
- **Against the API server's real behaviour.** RBAC provisioning was checked
  with `kubectl --dry-run=server`, which runs real defaulting and admission
  without persisting. That is how a blanket `kubectl apply -f kubernetes/` was
  found to report success while creating no RoleBinding at all, leaving a server
  that looks provisioned and can read nothing. The startup error message had
  been instructing operators to run exactly that command.
- **Against the source list, mechanically.** The exclusion table above was
  cross-checked entry by entry against section 3 of `requirements.md`, which
  found `get_replicaset_status` missing. The omission was invisible on a
  read-through because the document was internally coherent without it.
- **Against vacuous passes.** Each spec-text contract test was confirmed to fail
  under three perturbations: rewording the requirement, weakening the message in
  code, and breaking the test's own regex. A test that cannot fail is not a
  control. One test here would have started passing for the wrong reason after a
  refactor moved its output to a different stream — it asserted stderr was
  empty, and stderr had become unconditionally empty. It was caught and
  re-anchored.

Reviewers are encouraged to read `DEBUG_LOG.md` alongside this file. The near
miss most relevant to a security review is Issue #17: a cluster bearer token
that a plausible implementation of the malformed-kubeconfig error path would
have written to stderr.
