"""Parse Envoy prometheus stats and config_dump into triage-friendly traffic views."""

from __future__ import annotations

import re
from typing import Any

INGRESS_PREFIX = "ingress_http"
ADMIN_PREFIX = "admin"

LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
PROM_LINE_RE = re.compile(r"^([a-zA-Z0-9_:]+)(\{([^}]*)\})?\s+([^\s]+)$")

CODE_CLASS_MAP = {"1": "1xx", "2": "2xx", "3": "3xx", "4": "4xx", "5": "5xx"}


def parse_prometheus(text: str) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        name, _, labels_raw, value_raw = match.groups()
        labels: dict[str, str] = {}
        if labels_raw:
            labels = dict(LABEL_RE.findall(labels_raw))
        try:
            value: int | float = int(value_raw)
        except ValueError:
            try:
                value = float(value_raw)
            except ValueError:
                continue
        metrics.append({"name": name, "labels": labels, "value": value})
    return metrics


def _find_metric(metrics: list[dict[str, Any]], name: str, **labels: str) -> int | float | None:
    for metric in metrics:
        if metric["name"] != name:
            continue
        if all(metric["labels"].get(key) == value for key, value in labels.items()):
            return metric["value"]
    return None


def extract_ingress_summary(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    codes = {key: 0 for key in CODE_CLASS_MAP.values()}
    for metric in metrics:
        if metric["name"] != "envoy_http_downstream_rq_xx":
            continue
        if metric["labels"].get("envoy_http_conn_manager_prefix") != INGRESS_PREFIX:
            continue
        code_class = metric["labels"].get("envoy_response_code_class", "")
        label = CODE_CLASS_MAP.get(code_class)
        if label:
            codes[label] = int(metric["value"])

    total = _find_metric(
        metrics,
        "envoy_http_downstream_rq_total",
        envoy_http_conn_manager_prefix=INGRESS_PREFIX,
    )
    active = _find_metric(
        metrics,
        "envoy_http_downstream_rq_active",
        envoy_http_conn_manager_prefix=INGRESS_PREFIX,
    )
    no_route = _find_metric(
        metrics,
        "envoy_http_no_route",
        envoy_http_conn_manager_prefix=INGRESS_PREFIX,
    )
    rx_reset = _find_metric(
        metrics,
        "envoy_http_downstream_rq_rx_reset",
        envoy_http_conn_manager_prefix=INGRESS_PREFIX,
    )
    timeout = _find_metric(
        metrics,
        "envoy_http_downstream_rq_timeout",
        envoy_http_conn_manager_prefix=INGRESS_PREFIX,
    )

    return {
        "codes": codes,
        "total_requests": int(total or 0),
        "active_requests": int(active or 0),
        "no_route": int(no_route or 0),
        "rx_reset": int(rx_reset or 0),
        "timeout": int(timeout or 0),
        "error_rate": round(
            (codes["4xx"] + codes["5xx"]) / max(int(total or 0), 1) * 100,
            2,
        ),
    }


def extract_cluster_traffic(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}

    def ensure(cluster_name: str) -> dict[str, Any]:
        if cluster_name not in clusters:
            clusters[cluster_name] = {
                "cluster": cluster_name,
                "service": cluster_name.split("||")[0] if "||" in cluster_name else cluster_name,
                "upstream_codes": {},
                "upstream_code_classes": {key: 0 for key in CODE_CLASS_MAP.values()},
                "upstream_total": 0,
                "upstream_completed": 0,
                "connect_fail": 0,
                "connect_timeout": 0,
                "rq_timeout": 0,
                "rq_pending_overflow": 0,
                "membership_healthy": None,
                "membership_total": None,
            }
        return clusters[cluster_name]

    skip_clusters = {"xds_cluster", "local-cluster", "zipkin"}

    for metric in metrics:
        labels = metric["labels"]
        cluster_name = labels.get("envoy_cluster_name")
        if not cluster_name or cluster_name in skip_clusters:
            continue

        entry = ensure(cluster_name)
        name = metric["name"]
        value = int(metric["value"]) if isinstance(metric["value"], (int, float)) else 0

        if name in ("envoy_cluster_external_upstream_rq", "envoy_cluster_internal_upstream_rq"):
            code = labels.get("envoy_response_code", "unknown")
            entry["upstream_codes"][code] = entry["upstream_codes"].get(code, 0) + value
            entry["upstream_total"] += value
        elif name in (
            "envoy_cluster_external_upstream_rq_xx",
            "envoy_cluster_internal_upstream_rq_xx",
        ):
            code_class = labels.get("envoy_response_code_class", "")
            label = CODE_CLASS_MAP.get(code_class)
            if label:
                entry["upstream_code_classes"][label] += value
        elif name in (
            "envoy_cluster_external_upstream_rq_completed",
            "envoy_cluster_internal_upstream_rq_completed",
        ):
            entry["upstream_completed"] += value
        elif name == "envoy_cluster_upstream_cx_connect_fail":
            entry["connect_fail"] = value
        elif name == "envoy_cluster_upstream_cx_connect_timeout":
            entry["connect_timeout"] = value
        elif name == "envoy_cluster_upstream_rq_timeout":
            entry["rq_timeout"] = value
        elif name == "envoy_cluster_upstream_rq_pending_overflow":
            entry["rq_pending_overflow"] = value
        elif name == "envoy_cluster_membership_healthy":
            entry["membership_healthy"] = value
        elif name == "envoy_cluster_membership_total":
            entry["membership_total"] = value

    result = list(clusters.values())
    for entry in result:
        entry["has_issues"] = any(
            [
                entry["connect_fail"],
                entry["connect_timeout"],
                entry["rq_timeout"],
                entry["rq_pending_overflow"],
                entry["upstream_code_classes"]["5xx"],
                entry["membership_healthy"] == 0 and entry["membership_total"],
            ]
        )
    result.sort(key=lambda item: item["upstream_total"], reverse=True)
    return result


def extract_alerts(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[tuple[str, str, int]] = []

    def add(category: str, message: str, value: int | float | None) -> None:
        if value and int(value) > 0:
            checks.append((category, message, int(value)))

    ingress = extract_ingress_summary(metrics)
    add("routing", "Requests with no matching route", ingress["no_route"])
    add("client", "Downstream connection resets", ingress["rx_reset"])
    add("timeout", "Downstream request timeouts", ingress["timeout"])
    add("errors", "4xx responses on ingress", ingress["codes"]["4xx"])
    add("errors", "5xx responses on ingress", ingress["codes"]["5xx"])

    for metric in metrics:
        name = metric["name"]
        labels = metric["labels"]
        value = int(metric["value"])
        if value <= 0:
            continue

        if name == "envoy_http_rds_update_failure":
            route_config = labels.get("envoy_rds_route_config", "unknown")
            add("xds", f"Route config update failure ({route_config})", value)
        elif name == "envoy_http_rds_update_rejected":
            route_config = labels.get("envoy_rds_route_config", "unknown")
            add("xds", f"Route config update rejected ({route_config})", value)
        elif name == "envoy_http_rds_init_fetch_timeout":
            route_config = labels.get("envoy_rds_route_config", "unknown")
            add("xds", f"Route config init fetch timeout ({route_config})", value)
        elif name == "envoy_cluster_upstream_cx_connect_fail":
            cluster = labels.get("envoy_cluster_name", "unknown")
            if cluster not in {"xds_cluster", "local-cluster", "zipkin"}:
                add("upstream", f"Upstream connect failures ({cluster})", value)
        elif name == "envoy_cluster_upstream_rq_timeout":
            cluster = labels.get("envoy_cluster_name", "unknown")
            if cluster not in {"xds_cluster", "local-cluster", "zipkin"}:
                add("upstream", f"Upstream request timeouts ({cluster})", value)

    alerts = [
        {"category": category, "message": message, "count": count, "severity": _severity(category, count)}
        for category, message, count in checks
    ]
    alerts.sort(key=lambda item: (item["severity"] != "critical", -item["count"]))
    return alerts


def _severity(category: str, count: int) -> str:
    if category in {"xds", "upstream"} or count >= 100:
        return "critical"
    if category in {"errors", "timeout"} or count >= 10:
        return "warning"
    return "info"


def extract_routes_from_config(config_dump: Any) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    if not isinstance(config_dump, dict):
        return routes

    for config in config_dump.get("configs", []):
        dynamic = config.get("dynamic_route_configs") or []
        static = config.get("static_route_configs") or []
        for source in dynamic + static:
            route_config = source.get("route_config") or source
            for vhost in route_config.get("virtual_hosts", []):
                vhost_name = vhost.get("name", "")
                for route in vhost.get("routes", []):
                    match = route.get("match", {})
                    prefix = match.get("prefix") or match.get("path") or match.get("safe_regex", {}).get("regex", "")
                    if not prefix:
                        continue
                    route_action = route.get("route", {})
                    cluster = route_action.get("cluster", "")
                    routes.append(
                        {
                            "prefix": prefix,
                            "cluster": cluster,
                            "rewrite": route_action.get("prefix_rewrite") or route_action.get("regex_rewrite", ""),
                            "virtual_host": vhost_name,
                            "timeout": route_action.get("timeout", ""),
                        }
                    )

    routes.sort(key=lambda item: len(item["prefix"]), reverse=True)
    return routes


def _aggregate_paths_for_prefix(path_stats: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    matched = [item for item in path_stats if item.get("path", "").startswith(prefix)]
    if not matched:
        return {"total": 0, "codes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}, "no_route": 0}

    totals = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    total = 0
    no_route = 0
    for item in matched:
        total += item.get("total", 0)
        no_route += item.get("no_route", 0)
        for key in totals:
            totals[key] += item.get("codes", {}).get(key, 0)
    return {"total": total, "codes": totals, "no_route": no_route}


def build_route_traffic(
    routes: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    path_stats: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cluster_map = {item["cluster"]: item for item in clusters}
    path_stats = path_stats or []

    rows: list[dict[str, Any]] = []
    seen_clusters: set[str] = set()

    for route in routes:
        cluster_name = route["cluster"]
        cluster_stats = cluster_map.get(cluster_name, {})
        path_stat = _aggregate_paths_for_prefix(path_stats, route["prefix"])
        seen_clusters.add(cluster_name)

        upstream_classes = cluster_stats.get("upstream_code_classes", {})
        log_codes = path_stat.get("codes", {})
        rows.append(
            {
                "url_prefix": route["prefix"],
                "cluster": cluster_name,
                "service": cluster_stats.get("service", cluster_name.split("||")[0]),
                "rewrite": route.get("rewrite", ""),
                "requests": path_stat.get("total", cluster_stats.get("upstream_total", 0)),
                "codes": {
                    "2xx": log_codes.get("2xx", upstream_classes.get("2xx", 0)),
                    "3xx": log_codes.get("3xx", upstream_classes.get("3xx", 0)),
                    "4xx": log_codes.get("4xx", upstream_classes.get("4xx", 0)),
                    "5xx": log_codes.get("5xx", upstream_classes.get("5xx", 0)),
                },
                "upstream_codes": cluster_stats.get("upstream_codes", {}),
                "connect_fail": cluster_stats.get("connect_fail", 0),
                "rq_timeout": cluster_stats.get("rq_timeout", 0),
                "healthy": cluster_stats.get("membership_healthy"),
                "total_hosts": cluster_stats.get("membership_total"),
                "has_issues": bool(
                    cluster_stats.get("has_issues")
                    or log_codes.get("4xx")
                    or log_codes.get("5xx")
                    or path_stat.get("no_route")
                ),
            }
        )

    for cluster_name, cluster_stats in cluster_map.items():
        if cluster_name in seen_clusters:
            continue
        upstream_classes = cluster_stats.get("upstream_code_classes", {})
        rows.append(
            {
                "url_prefix": f"(cluster) {cluster_stats.get('service', cluster_name)}",
                "cluster": cluster_name,
                "service": cluster_stats.get("service", cluster_name),
                "rewrite": "",
                "requests": cluster_stats.get("upstream_total", 0),
                "codes": dict(upstream_classes),
                "upstream_codes": cluster_stats.get("upstream_codes", {}),
                "connect_fail": cluster_stats.get("connect_fail", 0),
                "rq_timeout": cluster_stats.get("rq_timeout", 0),
                "healthy": cluster_stats.get("membership_healthy"),
                "total_hosts": cluster_stats.get("membership_total"),
                "has_issues": cluster_stats.get("has_issues", False),
            }
        )

    rows.sort(key=lambda item: (not item["has_issues"], -item["requests"]))
    return rows


def build_traffic_report(
    prometheus_text: str,
    config_dump: Any,
    path_stats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metrics = parse_prometheus(prometheus_text)
    routes = extract_routes_from_config(config_dump)
    clusters = extract_cluster_traffic(metrics)
    return {
        "ingress": extract_ingress_summary(metrics),
        "alerts": extract_alerts(metrics),
        "clusters": clusters,
        "routes": build_route_traffic(routes, clusters, path_stats),
        "route_count": len(routes),
    }