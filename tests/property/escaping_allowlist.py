"""Deny-by-default allowlist of intentionally-unescaped response fields.

Property 17 poisons every string in every Kubernetes object a tool reads, then
asserts that no dangerous character survives into the response. Fields listed
here are the deliberate exceptions.

The list is deny-by-default and that is the whole point: a new tool that adds an
unescaped free-text field has no entry here, so P17 fails. Getting a field past
P17 requires either escaping it or writing down, in this file, why it is safe.

Each entry states a claim that is *not* about our code:

  enum_constrained  The field is drawn from a fixed vocabulary authored by a
                    Kubernetes control-plane component. Relies on that component
                    behaving as documented.
  api_validated     The API server rejects values containing dangerous
                    characters (DNS-1123 names, IPs, qualified names, quantity
                    strings). Relies on API server validation.
  not_yet_escaped   A real gap, temporary. NOT a valid steady state — its
                    presence is a stop-ship signal, and P17 asserts the list is
                    empty of these.

Note what P17 does NOT cover: a field the data model *omits* produces no poison
and therefore passes silently. Omission is safe from a leakage standpoint, but it
means P17 cannot replace the point-of-omission comment rule in CLAUDE.md. The two
mechanisms cover different failure modes.
"""

from __future__ import annotations

from typing import NamedTuple


class AllowlistEntry(NamedTuple):
    """One intentionally-unescaped response field."""

    field_path: str
    justification: str
    justification_type: str


ENUM_CONSTRAINED = "enum_constrained"
API_VALIDATED = "api_validated"
NOT_YET_ESCAPED = "not_yet_escaped"

VALID_JUSTIFICATION_TYPES = frozenset(
    {ENUM_CONSTRAINED, API_VALIDATED, NOT_YET_ESCAPED}
)

# Justification shared by every core-controller condition. Kept as a constant so
# that if the tradeoff is ever revisited, every site that depends on it is found
# by one search.
_CONTROLLER_CONDITION = (
    "Authored by a control-plane controller in kube-controller-manager from a "
    "fixed vocabulary; the accepted reason/type tradeoff rests on exactly this "
    "guarantee. Re-verify per resource type — it does not hold for runtime-, "
    "operator- or provisioner-authored fields (see REQ-021a, REQ-030a)."
)
_DNS1123 = (
    "Kubernetes object name; the API server enforces DNS-1123, which excludes "
    "every dangerous character."
)

ALLOWLIST: dict[str, tuple[AllowlistEntry, ...]] = {
    "get_pod_status": (
        AllowlistEntry("$.data.phase", "PodPhase enum; the API server writes one of Pending, Running, Succeeded, Failed, Unknown.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.qos_class", "PodQOSClass enum; kubelet writes one of Guaranteed, Burstable, BestEffort.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.node_name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.conditions[*].type", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].status", "ConditionStatus enum; the API server writes one of True, False, Unknown.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].reason", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry("$.data.container_statuses[*].name", _DNS1123, API_VALIDATED),
    ),
    "get_pod_logs": (),
    "get_pod_events": (),
    # Every field is escaped per REQ-057a, including the involved-object
    # reference that get_pod_events does not return.
    "get_namespace_events": (),
    "list_pods": (
        AllowlistEntry("$.data.pods[*].name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.pods[*].node_name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.pods[*].phase", "PodPhase enum; the API server writes one of Pending, Running, Succeeded, Failed, Unknown.", ENUM_CONSTRAINED),
    ),
    "get_deployment_status": (
        AllowlistEntry("$.data.conditions[*].type", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].status", "ConditionStatus enum; the API server writes one of True, False, Unknown.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].reason", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.rollout_strategy",
            "DeploymentStrategyType enum (Recreate, RollingUpdate).",
            ENUM_CONSTRAINED,
        ),
    ),
    "list_deployments": (
        AllowlistEntry("$.data.deployments[*].name", _DNS1123, API_VALIDATED),
    ),
    "get_statefulset_status": (
        AllowlistEntry(
            "$.data.update_strategy",
            "StatefulSetUpdateStrategyType enum (OnDelete, RollingUpdate).",
            ENUM_CONSTRAINED,
        ),
        AllowlistEntry(
            "$.data.current_revision",
            "ControllerRevision object name written by the StatefulSet controller; DNS-1123 enforced.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.update_revision",
            "ControllerRevision object name written by the StatefulSet controller; DNS-1123 enforced.",
            API_VALIDATED,
        ),
    ),
    "get_daemonset_status": (
        AllowlistEntry(
            "$.data.update_strategy",
            "DaemonSetUpdateStrategyType enum (OnDelete, RollingUpdate).",
            ENUM_CONSTRAINED,
        ),
    ),
    "get_service": (
        AllowlistEntry("$.data.type", "ServiceType enum; one of ClusterIP, NodePort, LoadBalancer, ExternalName.", ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.cluster_ip",
            "IP address or the literal 'None'; API server validates the format.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.external_ips[*]",
            "IP addresses; API server validates the format.",
            API_VALIDATED,
        ),
        AllowlistEntry("$.data.ports[*].protocol", "Protocol enum; the API server writes one of TCP, UDP, SCTP.", ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.ports[*].target_port",
            "Port number or IANA_SVC_NAME; both API-validated.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.selector.{key}",
            "Label key; API server enforces the qualified-name format.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.selector.*",
            "Label value; API server enforces the label-value format.",
            API_VALIDATED,
        ),
    ),
    "get_endpoints": (
        AllowlistEntry("$.data.ready[*].ip", "IP address written by the endpoints controller; the API server validates the address format.", API_VALIDATED),
        AllowlistEntry("$.data.ready[*].pod_name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.ready[*].node_name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.not_ready[*].ip", "IP address written by the endpoints controller; the API server validates the address format.", API_VALIDATED),
        AllowlistEntry("$.data.not_ready[*].pod_name", _DNS1123, API_VALIDATED),
        AllowlistEntry("$.data.ports[*].protocol", "Protocol enum; the API server writes one of TCP, UDP, SCTP.", ENUM_CONSTRAINED),
    ),
    "get_pvc_status": (
        AllowlistEntry("$.data.phase", "PersistentVolumeClaimPhase enum; one of Pending, Bound, Lost.", ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.access_modes[*]",
            "PersistentVolumeAccessMode enum; one of ReadWriteOnce, ReadOnlyMany, ReadWriteMany, ReadWriteOncePod.",
            ENUM_CONSTRAINED,
        ),
        AllowlistEntry(
            "$.data.volume_mode",
            "PersistentVolumeMode enum (Filesystem, Block).",
            ENUM_CONSTRAINED,
        ),
        AllowlistEntry("$.data.storage_class", "StorageClass object name, or empty string meaning no class; DNS-1123 enforced.", API_VALIDATED),
        AllowlistEntry("$.data.bound_pv", "PersistentVolume object name; the API server enforces DNS-1123.", API_VALIDATED),
    ),
    "get_hpa_status": (
        AllowlistEntry("$.data.conditions[*].type", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].status", "ConditionStatus enum; the API server writes one of True, False, Unknown.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].reason", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.metrics[*].type",
            "MetricSourceType enum (Resource, Pods, Object, External, "
            "ContainerResource).",
            ENUM_CONSTRAINED,
        ),
    ),
    "get_node_status": (
        AllowlistEntry("$.data.conditions[*].type", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].status", "ConditionStatus enum; the API server writes one of True, False, Unknown.", ENUM_CONSTRAINED),
        AllowlistEntry("$.data.conditions[*].reason", _CONTROLLER_CONDITION, ENUM_CONSTRAINED),
        AllowlistEntry(
            "$.data.taints[*].key",
            "Taint key; API server enforces the qualified-name format.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.taints[*].value",
            "Taint value; API server enforces the label-value format.",
            API_VALIDATED,
        ),
        AllowlistEntry(
            "$.data.taints[*].effect",
            "TaintEffect enum (NoSchedule, PreferNoSchedule, NoExecute).",
            ENUM_CONSTRAINED,
        ),
    ),
    # Empty by construction, not by escaping: a cluster-supplied namespace name
    # only reaches the response if it matches an operator-supplied allowlist
    # entry, so poisoned names are filtered out before output. P17 is therefore
    # vacuous for this tool — Property 16 is what actually constrains it.
    "list_namespaces": (),
    "list_nodes": (
        AllowlistEntry("$.data.nodes[*].name", _DNS1123, API_VALIDATED),
    ),
}


def allowed_paths(tool_name: str) -> set[str]:
    """Return the set of intentionally-unescaped paths for a tool."""
    return {entry.field_path for entry in ALLOWLIST.get(tool_name, ())}


def all_entries() -> list[tuple[str, AllowlistEntry]]:
    """Return every (tool_name, entry) pair across the allowlist."""
    return [
        (tool_name, entry)
        for tool_name, entries in ALLOWLIST.items()
        for entry in entries
    ]
