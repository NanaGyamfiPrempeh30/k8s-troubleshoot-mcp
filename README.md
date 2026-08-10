# k8s-troubleshoot-mcp

Read-only MCP server for Kubernetes cluster diagnostics.

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

## Local Testing with minikube

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
