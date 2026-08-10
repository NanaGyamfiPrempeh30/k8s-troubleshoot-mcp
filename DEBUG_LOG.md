# Debugging Log: k8s-troubleshoot-mcp

Tracks every error encountered, root cause, and resolution.

A recurring theme runs through most of what follows, and is worth stating once
at the top: **the test mocks agreed with each other rather than with the
cluster.** Issues #10, #12, #13, #16 and #21 are all the same failure — a
hand-written mock asserting a response shape the real Kubernetes client never
produces — and each was found by a live cluster or a schema-driven fake, never
by the suite as written. Where a bug of that class was fixed, the *wrong shape
was removed from the suite*, not merely supplemented with a correct case.

---

## Issue #1: `.gitignore` was entirely inert
**Date:** 2026-08-08
**Error:** 52 `.pyc` files found committed to the repository despite an apparently correct `.gitignore`.
**Root cause:** All 38 lines of the file were indented by four spaces. Git strips *trailing* whitespace from a pattern but treats *leading* whitespace as part of the pattern, so every rule matched a path beginning with spaces — i.e. nothing. The file looked correct in every editor and did nothing.
**Fix:** Rewrote `.gitignore` with patterns at column 0 and a header comment explaining the rule so it is not reintroduced. Rebased the four unpushed commits to purge the `.pyc` files from history, with a recoverable backup ref (`backup-pre-rebase-20260808`).
**Lesson:** A config file being present and plausible is not evidence it is in effect. Verify with `git check-ignore -v <path>`.

## Issue #2: `git rev-list --all` gave a false clean result
**Date:** 2026-08-08
**Error:** Verification that `.pyc` files were gone from history reported success while they were still reachable.
**Root cause:** `--all` includes every ref, and at that moment that meant the pre-rebase backup branch plus `refs/original/`. The check was measuring the branches that intentionally still contained the artefacts.
**Fix:** Re-ran the check scoped to `main` only.
**Lesson:** A verification command's default scope is part of the assertion. "No results" only means something once you know what was searched.

## Issue #3: Hypothesis `FailedHealthCheck` on `monkeypatch`
**Date:** 2026-08-08
**Error:** `FailedHealthCheck: function-scoped fixture ... used inside @given` in the P1/P2/P3/P5 property tests.
**Root cause:** pytest's `monkeypatch` fixture is function-scoped and is not reset between the many examples Hypothesis generates within one test function, so environment mutations leaked across examples.
**Fix:** Replaced it with an explicit `env()` context manager built on `mock.patch.dict(os.environ, ...)`, applied per example. The health check was **not** suppressed — suppressing it would have kept the leak and hidden it.

## Issue #4: `ValueError: embedded null byte` from generated env vars
**Date:** 2026-08-08
**Error:** Property 1 failed with `ValueError: embedded null byte` when setting a generated value into `os.environ`.
**Root cause:** The text strategy could emit `\x00`, which the OS rejects in an environment variable. This is a limit of the test harness, not of the property under test.
**Fix:** Constrained the alphabet with `exclude_characters="\x00"` and documented at the strategy why the exclusion is a harness limitation rather than a narrowing of the property.

## Issue #5: `re.DOTALL` swallowed the end of a spec blockquote
**Date:** 2026-08-08
**Error:** The REQ-063 verbatim-match test compared the instructions field against a captured blockquote that ran past the quote into the following section.
**Root cause:** The capture used `.` with `re.DOTALL`, so `.` matched newlines and the group ran on until the next match far below.
**Fix:** Rewrote the pattern with `[^\n]*` for the line body and `[\s\S]*?` for the lazy span between the requirement and its quote.
**Lesson:** A test that reads a spec file is itself parsing code and can be wrong in the direction of passing.

## Issue #6: Property 17 found four real escaping gaps on first run
**Date:** 2026-08-08
**Error:** The newly written Property 17 (escaping-is-applied) failed immediately on four field paths.
**Root cause:** Genuine gaps, not test error. `last_exit_reason`, `events[].reason`, `events[].type` and `kubelet_version` reached the response unescaped. Each had been assumed to be a control-plane-authored enum; none is. Container termination reasons come from the CRI runtime, event reasons from any controller in the cluster including third-party operators, and `kubelet_version` is self-reported by the node.
**Fix:** Escaped all four and added REQ-021a, REQ-030a and REQ-035a recording *why* each is not enum-safe. Deliberately did not add `not_yet_escaped` allowlist entries, which the design treats as a stop-ship signal rather than a steady state.

## Issue #7: `mcp[cli]` declared but not installed
**Date:** 2026-08-08
**Error:** `ModuleNotFoundError` for the FastMCP import when running the server module.
**Root cause:** `mcp[cli]` was listed in `pyproject.toml` but had never been installed into the working virtualenv.
**Fix:** Installed it. Later addressed structurally by pinning all 67 transitive dependencies in `uv.lock` (see Issue #22).

## Issue #8: `kubectl apply -f kubernetes/` half-succeeds silently
**Date:** 2026-08-09
**Error:** I asserted that a blanket `kubectl apply -f kubernetes/` "will fail". Verified against a live v1.35.1 API server with `--dry-run=server`, it does not.
**Root cause:** It reports **5 of 6** manifests applied. `role.yaml` is created in whatever namespace is current — `default`, not the target — and `rolebinding.yaml.template` is never read at all, because `kubectl apply -f <dir>` only picks up `.yaml`, `.yml` and `.json`. No RoleBinding is created anywhere. Provisioning *looks* successful and the server can read nothing.
**Fix:** `scripts/generate-kubeconfig.sh` applies the cluster-scoped set together, then `role.yaml` with `-n <ns>` and the rendered RoleBinding per namespace. REQ-014 was rewritten to specify that ordering and to forbid the blanket apply.
**Impact:** REQ-001's error message had been instructing operators to run exactly this command. Corrected (Issue #9).
**Lesson:** "It will fail" is a prediction. `--dry-run=server` runs real defaulting and admission without persisting, and was cheap enough that there was no excuse for guessing.

## Issue #9: REQ-001's error message instructed a broken command
**Date:** 2026-08-10
**Error:** The KUBECONFIG-not-set message told operators to provision with `kubectl apply -f kubernetes/`, which silently half-provisions (Issue #8).
**Root cause:** The message text was specified in `requirements.md` and hand-copied into `config.py`, and **nothing asserted the two matched or that either was correct**. Only `exit(1)` was tested, so both could drift indefinitely.
**Fix:** Rewrote the message to point at `scripts/generate-kubeconfig.sh` and to warn against the blanket apply. Built `_ExactMessageContract`, a four-test contract that parses the blockquote out of `requirements.md` and compares it to what the validator actually writes to stderr.
**Prevention:** The contract now covers REQ-001, REQ-004, REQ-005, REQ-008 and REQ-071. Each perturbation was verified to fail: rewording the spec, weakening the code message, and breaking the regex all produce failures rather than a silently vacuous pass.

## Issue #10: `get_pod_logs` returned a Python bytes repr
**Date:** 2026-08-10
**Error:** On a real cluster, `content` came back as `b'line one\nline two\n'` and `lines_returned` reported **1** for a 17-line log.
**Root cause:** Upstream, in `kubernetes/client/api_client.py:202`: the decode step is skipped when `response_type` is `"str"`, which is exactly what `read_namespaced_pod_log` declares. `deserialize()` then receives `bytes`, `json.loads` rejects plain log text, and `__deserialize_primitive` falls through to `str(bytes)` — the Python repr. A second-order hazard: a log that *is* valid JSON takes the `json.loads` success branch and produces yet another shape.
**Fix:** Pass `_preload_content=False` and decode the raw body in `_decode_log_body`, using `errors="replace"` because container output carries no encoding guarantee. Verified that status handling is unaffected — `rest.py` raises `ApiException` outside the `_preload_content` branch — so the 404 path and Property 7 are untouched.
**Prevention:** Every stub of this endpoint now goes through one `pod_log_response()` helper whose body is bytes. No test feeds it a bare `str` except the one asserting the defensive passthrough. A tripwire test asserts the *upstream bug still exists*, so a future client bump retires the workaround deliberately.
**Lesson:** Property 17 passed both before and after this fix. `str(bytes)` backslash-escapes control characters, so the corruption itself is what made the escaping property pass. A green escaping suite was evidence of nothing here. This is recorded in CLAUDE.md as the **faithful-looking corruption** category: data present, schema-valid, and confidently wrong.

## Issue #11: `unschedulable: null` where the contract says boolean
**Date:** 2026-08-10
**Error:** `list_nodes` returned `"unschedulable": null` on a real cluster; design.md specifies a boolean.
**Root cause:** `spec.unschedulable` is typed `bool` in `openapi_types` but is **omitted by the API server on every schedulable node**, so the client leaves it `None`. The existing guard covered a missing `spec` object but not an unset field.
**Fix:** `_is_unschedulable()`, used by both node tools — `get_node_status` had the identical bug even though it was only observed in `list_nodes`.
**Prevention:** The six existing mocks were changed from `unschedulable = False` to `None`, since `False` is a value the API never sends for a schedulable node. Their pre-existing assertions now exercise the normalization instead of asserting a value straight back.
**Note:** This is the opposite call from `storage_class_name`, where `None` and `""` are genuinely distinct states and both must survive. Absence here has a defined meaning; there it does not.

## Issue #12: `list_pods` raised `AttributeError` on omitted metadata
**Date:** 2026-08-10
**Error:** `AttributeError: 'NoneType' object has no attribute 'name'` at `pods.py:468`, reaching the MCP layer as an unhandled exception.
**Root cause:** `V1Pod.metadata` is optional in the model and `pod.metadata.name` was unguarded — while the *very next line* already guarded the same object for `creation_timestamp`, and `list_nodes` had always used the `"unknown"` fallback.
**Fix:** Matched the existing `list_nodes` pattern.
**Prevention:** Property 18 (optional-field omission safety) now generates, from each model's own schema, two shapes — every optional field `None`, and all objects present with only optional *scalars* `None` — and asserts no tool raises. Reverting this fix fails it.

## Issue #13: `get_hpa_status.current_replicas` contradicted itself
**Date:** 2026-08-10
**Error:** `current_replicas` was `0` when the whole `status` object was absent but `null` when only the field was absent — two different answers to the same question.
**Root cause:** `status.current_replicas if status else 0` guards the parent but passes the field straight through.
**Fix:** Normalized both absence shapes to `0`. `desired_replicas` is schema-required and needs no such handling.
**Lesson:** Found only by Property 18's *deep* shape. The sparse shape reported 2 nulls for this tool; the deep shape reported 7, because a `None` parent hides its children behind whatever fallback the tool applies to the parent. Both shapes are necessary and the property asserts they differ.

## Issue #14: my own Property 18 guard asserted the bug it was guarding
**Date:** 2026-08-10
**Error:** `test_property_18_both_shapes_are_actually_different` failed on the first full run after Issue #13 was fixed.
**Root cause:** The guard distinguished the sparse and deep shapes using `deep["current_replicas"] is None` — the defect itself. Fixing the defect made the guard fail.
**Fix:** Re-anchored to `conditions[]`, which separates the two shapes for a structural reason rather than a bug-specific one, with a comment saying why not to use `current_replicas` there.
**Lesson:** A guard anchored to a bug expires when the bug is fixed. Anchor to the structure being guarded.

## Issue #15: `capped` answered a narrower question than it appeared to
**Date:** 2026-08-10
**Error:** `get_namespace_events` returned `{"total": 50, "capped": false}` both for a namespace holding exactly 50 events and one holding 500. The two responses were byte-identical.
**Root cause:** `capped` reports only whether the *caller's* `limit` exceeded the hard maximum of 50. It says nothing about how much data was left behind. The true count was already in memory — the tool lists without a `limit` and sorts the full set before slicing — and was being discarded.
**Fix:** Added `total_available` (REQ-056a), and the same for `get_pod_events` (REQ-029a), which was worse off: it had no `total` *and* no `capped`, so nothing in the payload could distinguish the two cases at all.
**Prevention:** `total_available` is `null`, with a stderr warning, when the list response carries a `continue` token — a page-one count reported as a namespace total would recreate the exact ambiguity the field exists to remove. The detection now lives in one place (`pagination.py`) rather than a third hand-rolled copy.

## Issue #16: both event tools failed against a real cluster
**Date:** 2026-08-10
**Error:** Found by running the packaged container against minikube:
`Error executing tool get_pod_events: Invalid value for 'event_time', must not be 'None'`, returned as `isError: true` with an unstructured message.
**Root cause:** `events.k8s.io/v1` marks `eventTime` **required**, so the generated model's setter raises `ValueError` when it is `None`. A live v1.35.1 API server returns `eventTime: null` for every event mirrored from the legacy core/v1 path — which is most of what a kubelet emits; all five events in the test namespace had it. The raise happens **inside `kubernetes.client` during deserialization**, before any tool code runs, so it bypassed every `ApiException` handler and violated the rule that no exception reaches the MCP layer.
**Fix:** REQ-003a — build all four clients from one `ApiClient` with `client_side_validation = False`. This server never writes, so validation protects no outgoing payload; for incoming data the API server is the authority, and a schema disagreement must not make a readable object unreadable.
**Prevention:** A test deserializes the exact `eventTime: null` payload through the real `ApiClient`; another pins *why* the workaround exists, so a client release that stops marking the field required surfaces as a failure rather than leaving dead configuration behind.
**Lesson:** No test caught this because every event mock is a `MagicMock`, which never invokes the generated setters. This is the single strongest argument for the live-cluster phase existing at all.

## Issue #17: malformed kubeconfig produced a nine-frame traceback
**Date:** 2026-08-10
**Error:** A kubeconfig that exists and is readable but is malformed — truncated, a stray tab, or valid YAML missing `current-context` — exited with a raw traceback ending in `kubernetes.config.config_exception.ConfigException`.
**Root cause:** REQ-002 covers only "does not exist or is not readable". Nothing covered "present but unparseable", so the exception took an unhandled path.
**Fix:** REQ-002a — catch `ConfigException` and `YAMLError` in `main()`, emit one line naming the path and the reason, exit 1.
**Near miss:** The obvious implementation interpolates the exception message, and PyYAML's `MarkedYAMLError` *can* include a snippet of the offending source line — which, in a kubeconfig, may be the bearer token. Tested first with a JWT-shaped secret placed on the malformed line across five malformation types: PyYAML omits the snippet when reading from a file handle, because `Mark.get_snippet()` needs a buffer `load_kube_config` never provides. Including the reason is therefore safe. REQ-002a states the rule explicitly and a test asserts the secret is absent from stderr in all five cases, so a library change that starts quoting lines fails the build instead of leaking a credential.

## Issue #18: REQ-005's error message printed a Python list repr
**Date:** 2026-08-10
**Error:** Operators saw `ALLOWED_NAMESPACES contains wildcard token(s): ['*'].`
**Root cause:** `sorted(wildcards_found)` interpolated directly into an f-string renders as a list repr.
**Fix:** `", ".join(sorted(...))`. REQ-005 previously specified behaviour only ("an error message stating that…"), with no verbatim text; the blockquote was added so the contract test has something to compare against.

## Issue #19: two startup warnings bypassed the logging framework
**Date:** 2026-08-10
**Error:** REQ-008 (kube-system stripped) and REQ-071 (`MAX_LOG_LINES` clamped) used `sys.stderr.write` directly, so they ignored `LOG_LEVEL`, carried no formatter, and were invisible to every logging-based consumer.
**Root cause:** Written before the logging setup existed and never revisited.
**Fix:** Both migrated to `logger.warning`. Two subtleties had to be checked first: these fire inside `validate_env()`, which runs *before* `configure_logging()`, so Python routes them through `logging.lastResort` — verified out-of-process to reach stderr and never stdout, satisfying REQ-010. And `logging.lastResort` has **no formatter**, so the literal `"WARNING: "` prefix stays in the message text; without it an operator would see a bare sentence with no severity marker.
**Impact:** Switching to the logger silently emptied `sys.stderr`, which broke two pre-existing tests **and would have made a third pass vacuously** — it asserted stderr was empty, and stderr is now always empty. All three moved to `caplog`, plus new tests asserting the record's logger name and level so a revert fails loudly instead of degrading into a test that checks nothing.
**Lesson:** Changing *where* output goes silently invalidates every test that asserts on the old destination. Some of those tests fail; the dangerous ones start passing for the wrong reason.

## Issue #20: `git checkout --` destroyed uncommitted spec edits
**Date:** 2026-08-10
**Error:** Reverting a deliberate one-word perturbation with `git checkout -- requirements.md` wiped both of that session's uncommitted edits to the file.
**Root cause:** `git checkout --` restores from the index, discarding *all* working-tree changes to that path, not just the most recent one. The file had uncommitted work.
**Fix:** Reapplied both edits from their exact text and confirmed against the suite.
**Prevention:** Perturbation verification now copies the file to the scratchpad and restores from that copy, never from git, while any part of the tree is uncommitted.

## Issue #21: six of sixteen tools had no documented response contract
**Date:** 2026-08-10
**Error:** `get_statefulset_status`, `list_deployments`, `get_daemonset_status`, `get_namespace_events`, `list_namespaces` and `list_nodes` had no data model in design.md at all — discovered while trying to annotate a nullable field in one of them.
**Root cause:** The models were written for the first ten tools and never backfilled.
**Fix:** All six written, derived by executing each tool against a populated fake and dumping the actual response, not from reading the code or the REQ text.
**Findings surfaced by doing so:** `list_nodes.ready` is a **string** (`"True"`/`"False"`/`"Unknown"`), not a boolean — `"Unknown"` is a real diagnostic state a boolean would erase, so the implementation is right and REQ-038 is underspecified. `fully_available` is derived and reports `false` for a scaled-to-zero Deployment, which is defensible and was invisible in the REQ. `get_namespace_events.message` is the `note` field renamed for continuity with `get_pod_events`.

## Issue #22: `uv lock` failed twice before producing a lockfile
**Date:** 2026-08-10
**Error:** `error: unrecognized subcommand 'uv'`, then `Failed to discover managed Python installations`.
**Root cause:** The `ghcr.io/astral-sh/uv` image's entrypoint *is* `uv`, so `docker run … uv lock` passes `uv` as a subcommand. Correcting that exposed the second problem: the image is minimal and has no Python interpreter for `uv lock` to resolve against.
**Fix:** Ran `uv lock` inside `python:3.11-slim` in an isolated directory containing only `pyproject.toml` and `README.md`, then copied the result back — which also avoided the repo's existing `.venv` confusing resolution. 67 packages pinned.
**Why it was needed:** The precedent Dockerfile uses `uv sync --frozen`, which requires a lockfile this repository did not have.

## Issue #23: container could not read the mounted kubeconfig
**Date:** 2026-08-10
**Error:** `head: cannot open '/kubeconfig' for reading: Permission denied` as UID 10001.
**Root cause:** `scripts/generate-kubeconfig.sh` writes mode `600` owned by the invoking host user — correct for a credential — and bind mounts preserve host ownership, so the container's non-root user cannot read it.
**Fix:** Documented in the Dockerfile header: the file must be readable by UID 10001, or the container run with `--user "$(id -u)"`. Not "fixed" in the script, because loosening permissions on a cluster credential by default would be the wrong trade.

## Issue #24: `--network host` does not reach the cluster on Docker Desktop
**Date:** 2026-08-10
**Error:** `ConnectionRefusedError` to `127.0.0.1:54489` from inside the container even with `--network host`.
**Root cause:** On Docker Desktop the container joins the Docker VM's network namespace, not the WSL distribution's, so the published minikube port is not on that loopback. `host.docker.internal:54489` *is* reachable.
**Second problem:** Simply rewriting the kubeconfig server to `host.docker.internal` fails TLS verification — the minikube API server certificate's SANs are `minikubeCA, control-plane.minikube.internal, minikube, kubernetes*, localhost, 10.96.0.1, 127.0.0.1, 10.0.0.1, 192.168.49.2`. `host.docker.internal` is not among them.
**Fix:** Point the URL at `host.docker.internal` and add `tls-server-name: localhost` to the cluster entry — the kubeconfig field that exists for exactly this, honoured by the Python client via urllib3's `server_hostname`. Verified end-to-end with real tool calls.

## Issue #25: SECURITY.md omitted an entry from the spec's exclusion list
**Date:** 2026-08-10
**Error:** A cross-check comparing SECURITY.md against section 3 of `requirements.md` found `get_replicaset_status` missing.
**Root cause:** The list was written from the security-relevant exclusions and this one is a *scope deferral*, not a security exclusion — so it did not fit the framing and was dropped rather than reframed.
**Fix:** Added it, explicitly separated from the table with a note that it could be added later on ordinary product grounds without the threat-model review the other entries require.
**Lesson:** Cross-check generated prose against its source list mechanically. The omission was invisible on a read-through because the document was internally coherent without it.

## Issue #26: MCP Inspector reports Logging capability as unsupported
**Date:** 2026-08-10
**Error:** `Server Capabilities: Logging ✗` in MCP Inspector.
**Root cause:** Not a defect. `Server.get_capabilities()` sets the logging capability only if a `SetLevelRequest` handler is registered, and FastMCP's `_setup_handlers()` registers six handlers, none of them `set_logging_level`. There is no public FastMCP API to register one. Reproduced on a bare four-line FastMCP server, so nothing about this project's registration causes it.
**Resolution:** No action. The MCP logging capability sends log records to the *client*; this server logs to stderr, which is unrelated and, for a server whose warnings interpolate cluster-derived strings, the stronger position. Noted that Inspector also shows Resources and Prompts as supported despite none being registered, for the same reason — FastMCP registers those handlers unconditionally.
