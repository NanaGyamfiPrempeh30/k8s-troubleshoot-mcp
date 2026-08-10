# Project context
Read-only K8s diagnostics MCP server. Security-critical.
Spec: requirements.md (68 EARS requirements)
Design: design.md (18 correctness properties, P1-P18)

v0.2.0 backlog: migrate get_endpoints from core/v1 Endpoints to
discovery.k8s.io/v1 EndpointSlice. Endpoints is deprecated (warning
on every read as of 1.33) but still served and independently
populated — not urgent. Requires DiscoveryV1Api added to K8sClients
plus endpointslices RBAC rule — threat-model-touching change per
design.md:184-187, needs deliberate spec review, not a drive-by fix.

reason fields are treated as low-risk/enum-like by default; this is a per-tool tradeoff, not global — re-verify for each new resource type since provisioner/controller-authored reason fields (e.g., PVC, CRDs) may not hold the same guarantee as core K8s controllers.

# Do NOT trust .kiro/specs/tasks.md
It falsely claims work was completed that was never written. Ignore it
as a source of truth. It can be regenerated later once real progress exists.

# Non-negotiable rules
- Never propagate exceptions to MCP layer — all errors return structured dicts
- pods/log is a distinct RBAC rule, never folded under pods
- Events tools use EventsV1Api, never CoreV1Api
- No kubeconfig fallback — explicit path only, no in-cluster/default chain
- Run pytest tests/ after every file change
- Do not mark a task complete until its property test passes

# Escaping rule (added after get_pod_events gap)
Any field in design.md's data model containing free-text "message"
or "reason" that originates from cluster objects (not fixed K8s enums)
must route through serialize_log_content. This applies to node
conditions, event messages, and any future tool with similar fields.
Do not mark a tool complete without confirming this explicitly.

Two independent mechanisms enforce this rule and neither replaces the
other: Property 17 (tests/property/test_p17_escaping_applied.py) poisons
every field of every K8s object a tool reads and fails if anything
unescaped reaches the response — it catches leakage of fields the model
INCLUDES, but is blind to fields the model OMITS, since an omitted field
emits no poison and passes silently; the point-of-omission comment rule
below covers exactly that blind spot. P17 passing does not mean the
omission comments are unnecessary.

# Deliberate omissions must be documented at the point of omission
Where a data model or REQ intentionally excludes a free-text field
that exists in the underlying K8s API (e.g. StatefulSet/DaemonSet
conditions, pod condition.message), add a one-line comment at the
point in code where it's omitted, noting: "excluded per REQ-XXX,
would need serialize_log_content if added." Don't rely on catching
this at review time — the omission is invisible in a diff.

# Property test registry — mandatory per-file step
tests/property/strategies.py contains NAMESPACED_TOOLS, the shared
registry that P4/P6/P7 enumerate. Every new tool added to any
tools/*.py file MUST be added to this registry in the same change.
Before marking any tool complete, confirm it appears in
NAMESPACED_TOOLS — a tool missing from the registry causes P4/P6/P7
to silently stop covering it while still reporting green.

The same registry entry also carries api_models, the {method_name:
openapi_type} map that P17 and P18 use to generate fakes from the K8s
schema. It must name every API method the tool calls, or the fake
raises on the undeclared call. P18 additionally accepts the pseudo-type
"urllib3_response(bytes)" for endpoints read with _preload_content=False
(currently only read_namespaced_pod_log) — a tool declaring a bare "str"
there would be tested against a shape the real client never returns,
which is exactly how the get_pod_logs bytes-repr bug survived.

# Failure category: faithful-looking corruption
Distinct from leakage (P17's domain — raw dangerous chars reaching
the response) and omission (point-of-omission comments — a field
dropped entirely). This category is data present, schema-valid, and
confidently wrong in a way that structurally evades other checks.

Example: get_pod_logs originally returned str(bytes) instead of
bytes.decode() — the output looked like a plausible one-line log,
passed P17 because str() backslash-escapes control characters
(nothing "raw" leaked), and was simply corrupted data presented as
real. lines_returned said 1 for a 17-line log. Note that P17 did not
merely fail to catch it; the bug's own corruption is what made P17
pass, so a green escaping suite is evidence of nothing here.

When auditing a tool's output, check not just "is anything unescaped
or missing" but "does this value's type and shape match what the real
API actually sends" — verify against the live client, not just the
mock's assumed shape. Mocks are the specific hazard: every existing
test agreed with every other test on a response shape the real client
never produces. Adding one correct test alongside them is not enough;
remove the wrong shape from the suite.

# Plausible-wrong-answer class (found 3x: endpoints truncation,
# HPA metric pairing, event ordering)
Before trusting any K8s list/pagination/ordering parameter, verify
it means what the field name implies. "limit" may not mean "most
recent N" — check API docs, don't assume. A schema-valid response
can still answer the wrong question.
