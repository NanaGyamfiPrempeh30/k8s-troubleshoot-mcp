# Design: k8s-troubleshoot-mcp

## Overview

`k8s-troubleshoot-mcp` is a read-only MCP (Model Context Protocol) server that
exposes Kubernetes cluster diagnostics as structured tools for AI assistants.
It is implemented in Python using the FastMCP framework and the official
`kubernetes` client library.

The server communicates over **stdio transport** only (v1.0). It presents 16
diagnostic tools covering pods, workloads, nodes, services, storage, and events.
Every tool returns a structured JSON-serializable dict. The server never mutates
cluster state.

Security is enforced at two layers:
1. **Kubernetes RBAC** (enforcement boundary) — the service account's bindings
   define the maximum read scope.
2. **Application-layer controls** (defense-in-depth) — namespace allowlist,
   system-namespace exclusion, and log sanitization reduce blast radius and
   mitigate prompt-injection risk.

---

## Architecture

### High-level component diagram

```
┌─────────────────────────────────────────────────────────────┐
│  MCP Client (Claude Desktop / VS Code Copilot / Kiro)       │
└─────────────────────┬───────────────────────────────────────┘
                      │  stdio (JSON-RPC 2.0)
┌─────────────────────▼───────────────────────────────────────┐
│  FastMCP Server                                             │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  server.py  — mcp instance, tool registrations,       │ │
│  │               MCP instructions field                   │ │
│  └───────────────────────┬────────────────────────────────┘ │
│                          │                                  │
│  ┌───────────────────────▼────────────────────────────────┐ │
│  │  config.py  — startup validation, env var parsing,    │ │
│  │               ServerConfig dataclass                   │ │
│  └───────────────────────┬────────────────────────────────┘ │
│                          │  ServerConfig                    │
│  ┌───────────────────────▼────────────────────────────────┐ │
│  │  tools/                                                │ │
│  │    pods.py        — get_pod_status, get_pod_logs,      │ │
│  │                     get_pod_events, list_pods          │ │
│  │    nodes.py       — get_node_status, list_nodes        │ │
│  │    workloads.py   — get_deployment_status,             │ │
│  │                     list_deployments,                  │ │
│  │                     get_statefulset_status,            │ │
│  │                     get_daemonset_status               │ │
│  │    services.py    — get_service, get_endpoints         │ │
│  │    storage.py     — get_pvc_status                     │ │
│  │    autoscaling.py — get_hpa_status                     │ │
│  │    events.py      — get_namespace_events               │ │
│  │    namespaces.py  — list_namespaces                    │ │
│  └───────────────────────┬────────────────────────────────┘ │
│                          │                                  │
│  ┌───────────────────────▼────────────────────────────────┐ │
│  │  k8s_client.py  — kubernetes client factory,          │ │
│  │                   shared API group instances           │ │
│  └───────────────────────┬────────────────────────────────┘ │
│                          │  kubernetes Python SDK          │
└──────────────────────────┼──────────────────────────────────┘
                           │  kubeconfig (file on disk)
┌──────────────────────────▼──────────────────────────────────┐
│  Kubernetes API Server                                      │
└─────────────────────────────────────────────────────────────┘
```

### Startup sequence

```
__main__.py:main()
  │
  ├─► config.validate_env()
  │     ├─ check KUBECONFIG set → exit(1) if missing
  │     ├─ check KUBECONFIG file exists/readable → exit(1) if not
  │     ├─ check ALLOWED_NAMESPACES set → exit(1) if missing
  │     ├─ check no wildcard tokens → exit(1) if present
  │     ├─ parse namespace list, remove kube-system/kube-public
  │     └─ return ServerConfig
  │
  ├─► k8s_client.build_client(config.kubeconfig_path)
  │     └─ return CoreV1Api, AppsV1Api, AutoscalingV2Api,
  │          EventsV1Api clients
  │
  └─► server.create_app(config, k8s_clients)
        ├─ register all tools with FastMCP
        ├─ set MCP instructions (anti-injection statement)
        └─ mcp.run(transport="stdio")
```

### Request lifecycle (per tool call)

```
MCP client → JSON-RPC call
  │
  ├─► [namespace-scoped tools] validate namespace in allowed set
  │     └─ return structured error if not allowed (no K8s API call)
  │
  ├─► call Kubernetes API via client
  │     ├─ ApiException → catch → return structured error dict
  │     └─ ConnectionError/Timeout → catch → return structured error dict
  │
  ├─► extract fields, serialize log content (json.dumps escaping)
  │
  └─► return structured dict: {tool, status, data} or {tool, status, error, message}
```

---

## Components and Interfaces

### `src/k8s_troubleshoot_mcp/__main__.py`

Entry point. Calls `config.validate_env()`, builds K8s clients, creates the
FastMCP app, and runs it. Writes all logs to stderr.

```python
def main() -> None:
    """Entry point registered as console script in pyproject.toml."""
```

### `src/k8s_troubleshoot_mcp/config.py`

Responsible for all environment-variable validation and producing a `ServerConfig`.
Calls `sys.exit(1)` on any validation failure after writing to `sys.stderr`.

```python
@dataclass(frozen=True)
class ServerConfig:
    kubeconfig_path: str          # validated path to kubeconfig file
    allowed_namespaces: frozenset[str]  # immutable, kube-system/kube-public excluded
    log_level: str                # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    api_timeout_seconds: int      # default 30; passed as _request_timeout on every K8s call
    max_log_lines: int            # default 200, hard ceiling 1000; cap for get_pod_logs

def validate_env() -> ServerConfig:
    """
    Reads and validates KUBECONFIG, ALLOWED_NAMESPACES, LOG_LEVEL,
    API_TIMEOUT_SECONDS, MAX_LOG_LINES.
    Writes error messages to stderr and calls sys.exit(1) on failure.
    Returns a frozen ServerConfig on success.
    """
```

Design rationale: `frozenset` makes the allowed namespace set immutable after
construction, satisfying REQ-006's "immutable set for the lifetime of the server
process" requirement and preventing accidental mutation.

### `src/k8s_troubleshoot_mcp/k8s_client.py`

Builds and returns Kubernetes API client group instances from the kubeconfig path.
Never falls back to in-cluster config or `~/.kube/config`.

```python
@dataclass(frozen=True)
class K8sClients:
    core_v1: kubernetes.client.CoreV1Api
    apps_v1: kubernetes.client.AppsV1Api
    autoscaling_v2: kubernetes.client.AutoscalingV2Api
    events_v1: kubernetes.client.EventsV1Api   # events.k8s.io/v1 — used by get_pod_events and get_namespace_events

def build_clients(kubeconfig_path: str) -> K8sClients:
    """
    Loads kubeconfig from the explicit path only.
    Never calls config.load_incluster_config() or config.load_kube_config()
    without an explicit config_file argument.
    """
```

Design rationale: Explicit path-only loading is the technical enforcement of
REQ-003. The `kubernetes.config.load_kube_config(config_file=kubeconfig_path)`
call with an explicit `config_file` argument prevents any fallback to the default
path chain.

`EventsV1Api` (events.k8s.io/v1) is the current, non-deprecated events API.
`CoreV1Api` events (`/api/v1/namespaces/{ns}/events`) are deprecated since
Kubernetes 1.19 and removed in 1.32. All event tools MUST use `events_v1`.

`NetworkingV1Api` was removed from `K8sClients`. No v1.0 tool uses it —
`get_service` and `get_endpoints` both use `CoreV1Api`. If Ingress inspection is
added in a future version it will be introduced alongside a new threat model
review. Keeping unused API clients open is unnecessary attack surface.

`get_endpoints` and the endpoint count in `get_service` read the legacy core/v1
`Endpoints` resource rather than `EndpointSlice`. This is deliberate for v1.0 and
safe to defer; see **v0.2.0 Backlog, item 1** for why, and for the known ceiling
it carries.

### `src/k8s_troubleshoot_mcp/server.py`

Creates the FastMCP instance, registers all tools as decorated functions, and
sets the `instructions` field with the anti-injection statement (REQ-063).

```python
def create_app(config: ServerConfig, clients: K8sClients) -> FastMCP:
    """
    Returns a configured FastMCP instance with all 16 tools registered.
    The mcp.instructions field is set verbatim per REQ-063.
    """
```

Tool registration uses FastMCP's `@mcp.tool()` decorator. Each tool function
is a thin wrapper that:
1. Validates the namespace (if applicable)
2. Delegates to the appropriate module in `tools/`
3. Returns the result dict directly

### `src/k8s_troubleshoot_mcp/response.py`

Shared response construction helpers. All tools use these to ensure uniform
structure (REQ-015, REQ-016).

```python
def success(tool_name: str, data: dict) -> dict:
    return {"tool": tool_name, "status": "success", "data": data}

def error(tool_name: str, error_code: str, message: str, **extra) -> dict:
    return {"tool": tool_name, "status": "error", "error": error_code,
            "message": message, **extra}

def namespace_not_allowed(tool_name: str, namespace: str,
                          allowed: frozenset[str]) -> dict:
    return error(tool_name, "namespace_not_allowed",
                 f"Namespace '{namespace}' is not in the allowed list for "
                 "this deployment.",
                 allowed_namespaces=sorted(allowed))

def serialize_log_content(raw: str) -> str:
    """
    JSON-encodes the raw log string so all special characters
    (<, >, ", \\, Unicode control chars) are escaped.
    Uses json.dumps with ensure_ascii=False then strips outer quotes.
    """
```

### `src/k8s_troubleshoot_mcp/tools/`

Each module in this package contains functions for a logical grouping of tools.
All functions share the same signature pattern:

```python
def get_<resource>(
    clients: K8sClients,
    *args,
    **kwargs,
) -> dict:
    """
    Calls the K8s API, handles ApiException and connection errors,
    extracts required fields, and returns a structured dict via response.py.
    Never propagates exceptions.
    """
```

The tool functions are pure in the sense that given the same K8s API responses,
they always produce the same output dict. This makes them straightforwardly
testable with mocked clients.

#### Event tool API routing

Both event tools use `EventsV1Api` (events.k8s.io/v1), never `CoreV1Api`:

- **`get_pod_events`** → `clients.events_v1.list_namespaced_event(namespace, field_selector="regarding.name=<pod_name>,regarding.kind=Pod")` — filters to events for a specific pod by `regarding.name` and `regarding.kind`. Results are sorted by `event_time` / `last_timestamp` descending, capped at 50. Implementation docstring must state: `"Uses EventsV1Api.list_namespaced_event() filtered by regarding.name/regarding.kind. Never uses CoreV1Api events (deprecated)."`

- **`get_namespace_events`** → `clients.events_v1.list_namespaced_event(namespace)` — no field selector, returns all event types for the namespace. Results are sorted by `event_time` / `last_timestamp` descending, capped at `min(limit, 50)` where limit comes from the caller (REQ-056). Implementation docstring must state: `"Uses EventsV1Api.list_namespaced_event() with no field selector. Never uses CoreV1Api events (deprecated). Cap: min(caller_limit, 50)."`

---

## Data Models

### ServerConfig (frozen dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `kubeconfig_path` | `str` | Absolute path to kubeconfig file; validated at startup |
| `allowed_namespaces` | `frozenset[str]` | Parsed from `ALLOWED_NAMESPACES`; kube-system/kube-public removed |
| `log_level` | `str` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`; default `INFO` |
| `api_timeout_seconds` | `int` | From `API_TIMEOUT_SECONDS`; default `30`; passed as `_request_timeout` on every K8s API call |
| `max_log_lines` | `int` | From `MAX_LOG_LINES`; default `200`; hard ceiling `1000`; cap for `get_pod_logs` `tail_lines` |

### Standard tool response envelope

**Success:**
```json
{
  "tool": "<tool_name>",
  "status": "success",
  "data": { ... }
}
```

**Error:**
```json
{
  "tool": "<tool_name>",
  "status": "error",
  "error": "<error_code>",
  "message": "<human-readable description>"
}
```

**Namespace-not-allowed error:**
```json
{
  "tool": "<tool_name>",
  "status": "error",
  "error": "namespace_not_allowed",
  "message": "Namespace '<ns>' is not in the allowed list for this deployment.",
  "allowed_namespaces": ["<ns1>", "<ns2>"]
}
```

**ApiException error:**
```json
{
  "tool": "<tool_name>",
  "status": "error",
  "error": "kubernetes_api_error",
  "message": "<human-readable>",
  "http_status": 404,
  "reason": "Not Found"
}
```

### Tool-specific response data shapes

#### `get_pod_status` data
```json
{
  "pod_name": "string",
  "namespace": "string",
  "phase": "Running|Pending|Succeeded|Failed|Unknown",
  "conditions": [{"type": "string", "status": "string", "reason": "string"}],
  "container_statuses": [{
    "name": "string", "ready": true,
    "restart_count": 0, "last_exit_code": null,
    "last_exit_reason": null, "last_finished_time": null
  }],
  "qos_class": "Guaranteed|Burstable|BestEffort",
  "node_name": "string"
}
```

#### `get_pod_logs` data
```json
{
  "pod_name": "string", "namespace": "string",
  "container": "string", "lines_returned": 42,
  "truncated": false, "previous": false,
  "content": "<json-escaped log string>"
}
```

**`content` is a representation of the log, not the log.** It is escaped once by
`serialize_log_content` before the envelope is JSON-encoded, so it survives
transport still escaped. A client that calls `json.loads` on the MCP response
undoes the *transport* layer only and gets back a string in which a newline is
still the two characters `\` and `n`, a quote is still `\"`, and `<` is still
the six characters `\u003c`. There is no real newline anywhere in it.

Concretely, for a container that wrote `line one\nline two\n`:

| stage | value |
|-------|-------|
| container output | `line one` ⏎ `line two` ⏎ |
| `content` in the response dict | `line one\nline two\n` (as literal characters) |
| on the wire | `"content": "line one\\nline two\\n"` |
| after the client's `json.loads` | `line one\nline two\n` (as literal characters) |

Recovering the original text requires a **second** decode of the field itself:
`json.loads('"' + content + '"')`. This is deliberate. The escaping is the
structural prompt-injection mitigation (REQ-020, REQ-027); if one decode restored
real control characters and a raw `<`, the mitigation would be gone before the
model ever saw the content. A consumer that wants human-readable text is opting
out of that mitigation and should do so knowingly.

This is also why `lines_returned` is a separate field rather than something the
client derives: splitting `content` on a newline yields 1 regardless of the log's
real length, because `content` contains no newlines. Any consumer computing a
line count from `content` is computing the wrong number — the same wrong number
the `str(bytes)` defect produced (Property 18 / "faithful-looking corruption").

#### `get_pod_events` data
```json
{
  "pod_name": "string", "namespace": "string",
  "events": [{
    "reason": "string", "message": "string",
    "count": 3, "first_timestamp": "ISO8601",
    "last_timestamp": "ISO8601", "type": "Warning|Normal"
  }]
}
```

#### `list_pods` data
```json
{
  "namespace": "string", "total": 5,
  "pods": [{
    "name": "string", "phase": "string",
    "node_name": "string", "restart_count": 0,
    "age_seconds": 86400,
    "ready_containers": 2, "total_containers": 2
  }]
}
```

#### `get_node_status` data
```json
{
  "name": "string",
  "conditions": [{"type": "Ready", "status": "True", "reason": "string", "message": "string"}],
  "capacity": {"cpu": "4", "memory": "16Gi"},
  "allocatable": {"cpu": "3900m", "memory": "15Gi"},
  "taints": [{"key": "string", "value": "string", "effect": "NoSchedule"}],
  "unschedulable": false,
  "roles": ["control-plane"],
  "kubelet_version": "v1.29.0"
}
```

#### `get_deployment_status` data
```json
{
  "name": "string", "namespace": "string",
  "desired_replicas": "3|null", "ready_replicas": 3,
  "available_replicas": 3, "updated_replicas": 3,
  "conditions": [{"type": "Available", "status": "True", "reason": "string", "message": "string"}],
  "rollout_strategy": "RollingUpdate",
  "active_replicaset": "string"
}
```

#### `get_endpoints` data
```json
{
  "service_name": "string", "namespace": "string",
  "ready": [{"ip": "string", "pod_name": "string", "node_name": "string"}],
  "not_ready": [{"ip": "string", "pod_name": "string"}],
  "ports": [{"port": 8080, "protocol": "TCP"}],
  "truncated": false
}
```

#### `get_service` data
```json
{
  "name": "string", "namespace": "string",
  "type": "ClusterIP|null", "cluster_ip": "string|null",
  "external_ips": ["string"],
  "ports": [{"port": 80, "target_port": 8080, "protocol": "TCP", "node_port": null}],
  "selector": {"app": "string"},
  "ready_endpoints": 3,
  "truncated": false
}
```

`ready_endpoints` and `truncated` are both `null` when the Endpoints object
cannot be read (REQ-047a) — an unknown must not be reported as a known zero.

`type` and `cluster_ip` are annotated `|null` under the unreachable-but-possible
rule below, not because a real Service omits them.

#### `get_hpa_status` data
```json
{
  "name": "string", "namespace": "string",
  "current_replicas": 2, "desired_replicas": 3,
  "min_replicas": "1|null", "max_replicas": 10,
  "metrics": [{"type": "Resource", "name": "cpu", "current_value": "80%", "target_value": "70%"}],
  "conditions": [{"type": "AbleToScale", "status": "True", "reason": "string", "message": "string"}],
  "last_scale_time": "ISO8601|null"
}
```

`conditions[].message` and `metrics[].name` both route through
`serialize_log_content` (REQ-051a, REQ-051b). The HPA condition message is the
one condition message in this spec that is not purely control-plane authored —
on a metrics failure the HPA controller embeds the metrics adapter's error text
verbatim, and that adapter is frequently a third-party component. `reason` stays
unescaped on the same terms as pod/node/deployment conditions, because HPA
reasons are authored by kube-controller-manager from a fixed vocabulary.

`current_replicas` is **not** nullable. `status.currentReplicas` is optional in
the v2 schema, but the tool reports `0` for both "no status yet" and "status
present, field absent" — those are the same fact and previously got two
different answers. `status.desiredReplicas` is required by the schema and needs
no such handling.

#### Unreachable-but-possible nulls

Four fields are annotated `|null` above — `get_deployment_status.desired_replicas`,
`get_service.type`, `get_service.cluster_ip`, and `get_hpa_status.min_replicas` —
on a narrower basis than the other nullable fields in this document.

Each is optional in the OpenAPI schema, so the Python client can present it as
`None`, and the tools pass it through. But each is also **defaulted by the API
server on write**, verified with `kubectl create --dry-run=server` against a
v1.35.1 API server, which runs defaulting and admission without persisting:

| field | server-supplied default |
|-------|-------------------------|
| `Deployment.spec.replicas` | `1` |
| `Service.spec.type` | `ClusterIP` |
| `Service.spec.clusterIP` | allocated from the service CIDR |
| `HorizontalPodAutoscaler.spec.minReplicas` | `1` |

So on a conformant cluster these nulls do not occur. The annotation records that
the *type contract* permits them, not that the cluster produces them. This
matters for two reasons: a client must not assume the field is always an integer
or a string, and a future change that starts relying on the value being present
is relying on defaulting behavior, which is a server-version-dependent
assumption rather than a schema guarantee.

The code is deliberately unchanged for these four. Normalizing them would mean
asserting the API's defaults inside the tool, which duplicates a guarantee the
server already provides and would silently diverge if that guarantee ever
changed. This is the opposite call from `unschedulable` in `list_nodes`, where
the absent state is not merely possible but is what the API sends for *every*
schedulable node, and from `current_replicas` above, where the tool contradicted
itself. Existing tests that assert `None` for these fields are correct as written.

**Gap:** `get_statefulset_status.replicas` has the same property
(`StatefulSet.spec.replicas` defaults to `1`) but cannot be annotated here —
this document has no data model for `get_statefulset_status`, `list_deployments`,
`get_daemonset_status`, `get_namespace_events`, `list_namespaces` or
`list_nodes`. Those models should be added, at which point the same annotation
applies.

#### `get_pvc_status` data
```json
{
  "name": "string", "namespace": "string",
  "phase": "Bound|Pending|Lost",
  "storage_class": "standard",
  "access_modes": ["ReadWriteOnce"],
  "requested_storage": "10Gi",
  "actual_capacity": "10Gi|null",
  "bound_pv": "string|null",
  "volume_mode": "Filesystem",
  "resize_status": "NodeResizePending|null"
}
```

`resize_status` is `null` when no expansion is in progress (REQ-053a). It is the
only field that explains a legitimate disagreement between `requested_storage`
and `actual_capacity`: during an expansion the request updates immediately while
the capacity lags, and without this field a mid-resize PVC is indistinguishable
from a stalled one. Note also that `actual_capacity` may exceed
`requested_storage` on a fully healthy PVC, because provisioners round up to
their own minimum granularity — that is not an expansion and carries no
`resize_status`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

---

### Property 1: No kubeconfig fallback

*For any* server startup invocation where `KUBECONFIG` is not set in the
environment, the config loader must never read from `~/.kube/config` or any
default path; it must fail fast with exit code 1.

**Validates: Requirements 4.1 (REQ-003)**

---

### Property 2: Wildcard namespace rejection

*For any* `ALLOWED_NAMESPACES` string that contains the token `*` or the token
`all` (as a comma-separated element or standalone), the startup validation must
reject it and return a non-zero exit code before the server reaches the tool
registration phase.

**Validates: Requirements 4.2 (REQ-005)**

---

### Property 3: Namespace parse idempotence

*For any* valid comma-separated namespace string (no wildcards, no
kube-system/kube-public), parsing it once and parsing it twice must produce
identical `frozenset` values. Additionally, every element in the resulting set
must be a stripped token from the original string with no leading/trailing whitespace.

**Validates: Requirements 4.2 (REQ-006)**

---

### Property 4: Namespace allowlist gate — no API call on disallowed namespace

*For any* namespace string not present in `allowed_namespaces`, and *for any*
namespace-scoped tool, calling that tool with the disallowed namespace must
return a structured `namespace_not_allowed` error dict and must never invoke any
method on the Kubernetes API client.

**Validates: Requirements 4.2 (REQ-007, REQ-019)**

---

### Property 5: System-namespace exclusion

*For any* `ALLOWED_NAMESPACES` input containing `kube-system` or `kube-public`,
those values must never appear in the parsed `allowed_namespaces` frozenset,
regardless of what other namespaces are present in the input.

**Validates: Requirements 4.2 (REQ-008)**

---

### Property 6: All tool responses are JSON-serializable with required envelope fields

*For any* tool and *for any* valid input (with mocked Kubernetes API returning
arbitrary response objects), the return value of every tool must:
- Be serializable by `json.dumps` without raising an exception
- Contain a `"tool"` key whose value is a non-empty string
- Contain a `"status"` key whose value is either `"success"` or `"error"`
- On success: contain a `"data"` key
- On error: contain `"error"` and `"message"` keys

**Validates: Requirements 6.0 (REQ-015, REQ-016)**

---

### Property 7: ApiException always produces structured error, never propagates

*For any* tool and *for any* `ApiException` (arbitrary HTTP status code, arbitrary
reason string, arbitrary body), calling that tool with a mocked client that raises
the exception must return a structured error dict — never raise an exception to
the MCP layer. The error dict must include the HTTP status code and reason, except
for 404 responses, which tools translate to a domain-specific error code (e.g.
`pod_not_found`) and MAY omit `http_status` and `reason`.

The never-raises half of this property is unconditional and admits no carve-out:
every status, including 404, must still produce a structured error dict.

**Validates: Requirements 6.0 (REQ-017)**

---

### Property 8: Log content special-character escaping

*For any* string containing `<`, `>`, `"`, `\`, or Unicode control characters
(U+0000 through U+001F), passing it through `serialize_log_content` must produce
a string in which all those characters are properly escaped such that embedding
it in a JSON object field does not alter the semantics of the surrounding JSON.

**Validates: Requirements 6.0 (REQ-020, REQ-027)**

---

### Property 9: get_pod_status excludes credential-bearing fields

*For any* mock pod object — regardless of what values are present in its
`spec.containers[*].env`, `spec.volumes`, or `spec.containers[*].volume_mounts`
fields — the `get_pod_status` response dict must never contain the keys
`env`, `env_vars`, `environment`, `volumes`, `volume_mounts`, or any key whose
value could expose raw credential data.

**Validates: Requirements 6.1 (REQ-023)**

---

### Property 10: tail_lines cap at config.max_log_lines

*For any* integer value of `tail_lines` greater than `config.max_log_lines`, the
`get_pod_logs` response must have `truncated: true` and `lines_returned` must be
at most `config.max_log_lines`. *For any* value of `tail_lines` ≤ `config.max_log_lines`,
the response must have `truncated: false` (assuming the log has at least
`tail_lines` lines). `config.max_log_lines` is itself bounded at startup: default
200, hard ceiling 1000 (REQ-071).

**Validates: Requirements 6.2 (REQ-025, REQ-071)**

---

### Property 11: Pod events sorted descending by last_timestamp, max 50 returned

*For any* list of events of arbitrary length and arbitrary timestamp ordering,
the `get_pod_events` response must return at most 50 events, and the
`last_timestamp` of each successive event in the list must be less than or equal
to the one before it (descending order).

**Validates: Requirements 6.3 (REQ-029)**

---

### Property 12: list_pods restart_count equals sum of container restart counts

*For any* pod object with N containers each having an arbitrary non-negative
restart count, the `restart_count` field in the `list_pods` response for that
pod must equal the arithmetic sum of all per-container restart counts.

**Validates: Requirements 6.4 (REQ-032)**

---

### Property 13: label_selector passed to Kubernetes API unchanged

*For any* non-empty string passed as `label_selector` to `list_pods`, the value
forwarded to the `kubernetes.client.CoreV1Api.list_namespaced_pod` call must be
identical (byte-for-byte) to the input string.

**Validates: Requirements 6.4 (REQ-033)**

---

### Property 14: get_endpoints correctly partitions ready and not-ready addresses

*For any* endpoints object with N ready addresses and M not-ready addresses
(arbitrary N, M ≥ 0), the `get_endpoints` response must have exactly N items in
`data.ready` and exactly M items in `data.not_ready`.

**Known limitation of this property — read before relying on it.** This property
validates *faithful reflection of the Endpoints object's contents only*. It says
nothing about completeness against the Service's actual backend count, and it
cannot: the legacy core/v1 endpoints controller truncates an Endpoints object at
1000 addresses, so for a Service with more than 1000 backends the object itself
is already incomplete before this tool reads it. A response can satisfy Property
14 exactly and still under-report the real backend set. This is a documented gap
in what the property covers, not an implicit one — the `truncated` flag
(REQ-047a, REQ-049a) is the mechanism that surfaces it to the caller, and closing
the gap properly requires the EndpointSlice migration deferred to v0.2.0.

**Validates: Requirements 6.12 (REQ-049, REQ-049a, REQ-050)**

---

### Property 15: namespace_events limit cap at 50

*For any* integer value of `limit` greater than 50, the `get_namespace_events`
response must have `capped: true` and the `events` list length must be at most 50.

**Validates: Requirements 6.15 (REQ-056)**

---

### Property 16: list_namespaces response is the allowlist–cluster intersection

*For any* set of namespaces returned by the Kubernetes cluster API, the
`list_namespaces` response must be **exactly the intersection of
`config.allowed_namespaces` and the namespaces that actually exist on the
cluster — a subset of `allowed_namespaces` is necessary but not sufficient.**

Both directions carry a distinct failure, and each is enforced by its own test:

- **Not a superset of the intersection on the allowlist side.** No namespace
  outside `allowed_namespaces` may appear, regardless of what the cluster API
  returns. Violating this leaks the cluster's namespace topology to a client
  restricted to a subset of it.
- **Not a superset of the intersection on the cluster side.** No namespace absent
  from the cluster's response may appear, even if it is in `allowed_namespaces`.
  Violating this reports namespaces that do not exist as though they do.

The subset-of-allowed formulation alone is satisfied trivially by an
implementation that returns `config.allowed_namespaces` verbatim without ever
calling the cluster. That implementation is wrong in a way the earlier wording
could not detect: schema-valid, property-passing, and asserting the existence of
namespaces that may never have existed. The intersection formulation is what
makes the property discriminating.

**Validates: Requirements 6.16 (REQ-058, REQ-059, REQ-072)**

---

### Property 17: Escaping is applied, not merely available

*For any* tool and *for any* payload containing `<`, `>` or a Unicode control
character injected into every string field of the Kubernetes object that tool
reads, no dangerous character may survive into the response — except at field
paths explicitly allowlisted in `tests/property/escaping_allowlist.py` with a
written justification.

Property 8 proves `serialize_log_content` is *correct*. Property 17 proves it is
*reached*. Without it, the escaping rule is enforced only by convention and code
review, which is the same class of invisible-in-a-diff risk that motivated the
point-of-omission comment rule.

The test fakes are generated from each Kubernetes model's own `openapi_types`
schema rather than hand-built per tool, so a tool that reads a field nobody
remembered to mock still receives poisoned input. This makes the property an
*omission detector* rather than only a regression guard: a newly added unescaped
field has no allowlist entry and therefore fails.

The allowlist is deny-by-default. Each entry records a `justification_type`:
`enum_constrained` (fixed vocabulary authored by a control-plane component),
`api_validated` (API server rejects dangerous characters — DNS-1123 names, IPs,
qualified names), or `not_yet_escaped` (a real gap). `not_yet_escaped` is **not a
valid steady state**; its presence is a stop-ship signal and the property asserts
the list is empty of them.

**Known limitation.** A field the data model *omits* produces no poison and
passes silently. Omission is safe from a leakage standpoint, so this is correct
behavior — but it means Property 17 does not subsume the point-of-omission
comment rule. The two mechanisms cover different failure modes and both are
required.

**Validates: Requirements 6.0, 6.1, 6.3, 6.4 (REQ-020, REQ-021a, REQ-027,
REQ-030a, REQ-035a, REQ-051a, REQ-051b)**

---

### Property 18: Optional-field omission safety

*For any* tool and *for any* Kubernetes object in which every non-required field
has been omitted, the tool must return a valid, JSON-serializable response
envelope on its success path rather than raising.

Kubernetes omits unset optional fields from its JSON, so the client leaves them
`None`. Hand-written mocks do the opposite — they populate whatever the author
thought of. Both bugs found by real-cluster validation came from that mismatch:
`list_nodes` returned `"unschedulable": null` because the API omits the field on
every schedulable node, and `list_pods` raised `AttributeError` on
`pod.metadata.name` because `metadata` is optional in the model.

Required-ness is read from the generated setter rather than from documentation:
`kubernetes-client` emits a ``must not be `None` `` guard for required properties
only. The fakes are then generated from each model's `openapi_types`, the same
schema-driven approach as Property 17, so a field nobody remembered to mock is
still exercised.

Two input shapes are generated, and both are necessary:

- **SPARSE** — every optional field is `None`, including nested objects. This is
  a freshly created or partially reconciled object.
- **DEEP** — every nested object is built, but every optional *scalar* is `None`.

SPARSE cannot reach inner scalars, because a `None` parent hides its children
behind whatever fallback the tool applies to the parent. Probing `get_hpa_status`
with SPARSE reported 2 nulls; DEEP reported 7. One of the 7 was
`current_replicas`, which defaulted to `0` when `status` was absent but passed
`None` through when only the field was absent — a defect SPARSE could not see and
DEEP found on its first run (since fixed). The test asserts the two shapes
differ, so neither can silently collapse into the other.

**Deliberately out of scope.** This property does not assert that any given
response field is *non-null*. Which fields may legitimately be null is a
per-field contract question — design.md marks nullable fields as `"X|null"` —
and encoding it requires a per-field allowlist in the shape of Property 17's.
Property 18 covers crash-safety only; a null that is off-contract but harmless
passes.

**Validates: Requirements 5.1 (REQ-015, REQ-016) and REQ-017's final clause —
tools SHALL NOT propagate unhandled exceptions to the MCP layer. REQ-017 names
`ApiException` as the source; this property covers the other one, a well-formed
API response whose optional fields are absent.**

---

## Error Handling

### Startup errors (fatal)

All startup errors write to `sys.stderr` and call `sys.exit(1)`. They occur
before FastMCP is initialized, so no MCP-layer error handling applies.

| Condition | Error message |
|-----------|---------------|
| `KUBECONFIG` not set | See REQ-001 exact text |
| `KUBECONFIG` file not readable | "KUBECONFIG path 'PATH' does not exist or is not readable." |
| `ALLOWED_NAMESPACES` not set | See REQ-004 exact text |
| `ALLOWED_NAMESPACES` contains `*` or `all` | See REQ-005 exact text |
| `API_TIMEOUT_SECONDS` non-integer or non-positive | See REQ-070 exact text |
| `MAX_LOG_LINES` non-integer or non-positive | Descriptive error; exit 1 (REQ-071) |

### Runtime errors (per tool call, non-fatal)

All tool-level errors return the standard error envelope. The server process
continues running after any runtime error.

| Condition | `error` code | Notes |
|-----------|-------------|-------|
| Namespace not in allowed set | `namespace_not_allowed` | Includes `allowed_namespaces` list |
| Resource not found (HTTP 404) | `<resource>_not_found` | e.g., `pod_not_found`, `node_not_found` |
| K8s API error (non-404) | `kubernetes_api_error` | Includes `http_status` and `reason` |
| Connection error / timeout | `connection_error` | No retry |
| Unexpected Python exception | `internal_error` | Logged to stderr; structured error returned |

### Log output rules

- All logging uses the Python `logging` module.
- All log handlers write to `sys.stderr` only (FileHandler to stderr, no StreamHandler to stdout).
- `LOG_LEVEL` env var controls verbosity; default `INFO`.
- DEBUG logs include the tool name and namespace for each call.
- No log output ever writes to stdout (reserved for MCP JSON-RPC).

---

## Security Design

### RBAC boundary

The service account `k8s-mcp-server` (namespace `k8s-mcp`) is bound to:
- **ClusterRole `k8s-mcp-readonly`** — grants `get`, `list`, `watch` on `nodes`
  and `namespaces` cluster-wide.
- **Role `k8s-mcp-readonly`** — grants `get`, `list`, `watch` on a specific set
  of namespaced resources (pods, pods/status, events, deployments, statefulsets,
  daemonsets, replicasets, services, endpoints, PVCs, HPAs). Explicitly excludes
  secrets, configmaps, and serviceaccounts. **`pods/log` is granted `get` only,
  in a separate rule block** — it is a distinct subresource and must not be folded
  into the `pods` rule.

The operator applies a `RoleBinding` per target namespace using the
`kubernetes/rolebinding.yaml.template`. This is the enforcement boundary;
application-layer controls are defense-in-depth only.

### Namespace allowlist (application layer)

`ALLOWED_NAMESPACES` is parsed at startup into an immutable `frozenset`. Every
namespace-scoped tool validates against this set before making any API call.
This provides defense-in-depth against misconfigured RBAC that accidentally
grants access to unintended namespaces.

### System-namespace exclusion

`kube-system` and `kube-public` are unconditionally removed from the allowlist
at startup. This prevents accidental exposure of control-plane workload details.

### Log sanitization (prompt-injection mitigation)

Log content is passed through `json.dumps` before inclusion in tool responses.
This escapes `<`, `>`, `"`, `\`, and Unicode control characters, which are the
vectors most commonly used in prompt-injection payloads embedded in log lines.

This is a structural mitigation, not a complete defense. The MCP `instructions`
field carries an explicit anti-injection statement directing the AI client to
treat all returned content as data only.

### Excluded capabilities

The following capabilities are permanently excluded (see requirements Section 3):
`get_secrets`, `exec_into_pod`, `get_configmap`, `get_serviceaccount_tokens`,
`port_forward`, all mutation verbs. Any future addition requires a documented
threat model review.

`NetworkingV1Api` (Ingress, NetworkPolicy) is not included in `K8sClients` for
v1.0. No current tool requires it — `get_service` and `get_endpoints` use
`CoreV1Api`. If Ingress inspection is added in a future version, it must be
introduced alongside a new threat model review and explicit scope expansion.
Keeping unused API clients open is unnecessary dead attack surface.

### Container security

The Docker container runs as UID 10001 (non-root) using a multi-stage build.
The kubeconfig file is mounted read-only. No credentials are baked into the image.

### No credential fields in responses

`get_pod_status` explicitly omits `spec.containers[*].env`, `spec.volumes`, and
`spec.containers[*].volume_mounts`. No tool returns raw secret or configmap data.

---

## v0.2.0 Backlog

Deliberate v1.0 scope boundaries that are known ceilings rather than permanent
choices. Each is a case where the v1.0 response is *honest* about its limits but
does not remove them. Both items below are the same tier: neither blocks v1.0,
both require an API-surface change that touches the threat model, and both should
be taken together in v0.2.0 rather than piecemeal.

### 1. Migrate `get_endpoints` to `DiscoveryV1Api` (EndpointSlice)

`get_endpoints` and the endpoint count in `get_service` read the legacy core/v1
`Endpoints` resource.

Why deferring is safe: core/v1 Endpoints is still populated on every current
Kubernetes version by its own dedicated endpoints controller in
kube-controller-manager. It is *not* downstream of EndpointSlice — the
`EndpointSliceMirroring` controller runs the opposite direction
(Endpoints → EndpointSlice, for selector-less Services only, and it explicitly
skips any Service with a non-nil selector). So Endpoints cannot go empty while
EndpointSlice holds real data for a selector-based Service. The API was formally
deprecated in Kubernetes 1.33 and the API server now emits a warning on every
read, but it is still served with no removal date.

Why it is a ceiling: the endpoints controller truncates at 1000 addresses
(annotating `endpoints.kubernetes.io/over-capacity`), while EndpointSlice shards
across multiple slices at 100 each and stays complete. For Services above 1000
backends the legacy object is lossy at the source. v1.0 surfaces this honestly
via the `truncated` flag (REQ-047a, REQ-049a) rather than silently under-reporting;
it does not fix it.

Migrating requires adding `DiscoveryV1Api` to `K8sClients` plus an RBAC rule for
`endpointslices` in `discovery.k8s.io`. Both are threat-model-touching changes and
must follow the same documented-addition process as any other API surface, so
they are out of scope for v1.0. EndpointSlice would additionally give per-endpoint
`serving`/`terminating` conditions that core/v1 Endpoints cannot express, which is
the main diagnostic upside beyond correctness at scale.

### 2. Expose PVC `status.conditions` in `get_pvc_status`

REQ-053 returns phase, capacity, binding and volume-mode fields, and REQ-053a adds
`resize_status`. It does not return `status.conditions`.

Why it is a ceiling: for a PersistentVolumeClaim, conditions are where the
actionable diagnosis usually lives. `ProvisioningFailed` explains a PVC stuck in
`Pending` far better than the phase alone does, and `FileSystemResizePending`
tells an operator that a resize needs a pod restart to complete — neither is
derivable from the REQ-053 field set. `resize_status` covers the expansion state
machine specifically, so the v1.0 gap is narrower than it was before REQ-053a, but
provisioning and mount failures remain invisible.

Why deferring is right anyway: this is additive diagnostic surface, not a
correctness defect. Nothing v1.0 returns is wrong without it — it is simply less
complete than it could be, and v1.0 is scope-constrained to ship.

`V1PersistentVolumeClaimCondition` carries free-text `message` **and** `reason`.
Adding this field therefore triggers the escaping rule: both must route through
`serialize_log_content`, and the `reason`-is-low-risk tradeoff accepted elsewhere
in v1.0 should be re-decided deliberately here rather than inherited, because PVC
condition reasons are provisioner-authored and not drawn from a fixed enum.

## Testing Strategy

### Dual approach

Two complementary test layers are required:

1. **Unit / example-based tests** — verify specific behaviors, error paths, and
   edge cases using mocked Kubernetes clients.
2. **Property-based tests** — verify universal correctness properties (P1–P18
   above) across many generated inputs using `hypothesis`.

### Property-based testing library

Use **`hypothesis`** (Python PBT library). Each property test runs a minimum of
100 examples. Tests are tagged with the property they validate:

```python
# Feature: k8s-troubleshoot-mcp, Property 6: all tool responses are JSON-serializable
@given(st.builds(...))
@settings(max_examples=100)
def test_property_6_tool_responses_json_serializable(...):
    ...
```

### Test file layout

```
tests/
  unit/
    test_config.py          — startup validation, namespace parsing
    test_response.py        — response helper functions, log serialization
    test_pods.py            — get_pod_status, get_pod_logs, get_pod_events, list_pods
    test_nodes.py           — get_node_status, list_nodes
    test_workloads.py       — deployment, statefulset, daemonset tools
    test_services.py        — get_service, get_endpoints
    test_storage.py         — get_pvc_status
    test_autoscaling.py     — get_hpa_status
    test_events.py          — get_namespace_events
    test_namespaces.py      — list_namespaces
  property/
    test_p1_no_kubeconfig_fallback.py
    test_p2_wildcard_rejection.py
    test_p3_namespace_parse_idempotence.py
    test_p4_namespace_allowlist_gate.py
    test_p5_system_namespace_exclusion.py
    test_p6_tool_response_envelope.py
    test_p7_apiexception_structured_error.py
    test_p8_log_serialization.py
    test_p9_no_credential_fields.py
    test_p10_tail_lines_cap.py
    test_p11_events_sorted.py
    test_p12_restart_count_sum.py
    test_p13_label_selector_passthrough.py
    test_p14_endpoints_partition.py
    test_p15_namespace_events_cap.py
    test_p16_list_namespaces_allowlist.py
    test_p17_escaping_applied.py
```

### Mocking strategy

All Kubernetes API calls are mocked using `unittest.mock.MagicMock` or
`pytest-mock`. Property tests use `hypothesis` strategies to generate:
- Arbitrary pod/node/deployment/event objects via `st.builds()`
- Arbitrary `ApiException` instances with random status codes and reasons
- Arbitrary log strings including Unicode, control characters, and injection payloads
- Arbitrary namespace strings for allowlist testing

No tests make real Kubernetes API calls. Integration tests against a live cluster
are out of scope for this spec (they are the operator's responsibility).

### Test framework

- **`pytest`** — test runner
- **`hypothesis`** — property-based testing
- **`pytest-mock`** — mock fixtures
- **`pytest-cov`** — coverage reporting

Target: ≥90% line coverage across `src/k8s_troubleshoot_mcp/`.
