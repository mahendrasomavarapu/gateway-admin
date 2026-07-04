#!/usr/bin/env python3
"""Gateway Admin UI backend – production triage for Envoy ingress gateways."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from k8s_logs import K8sLogClient, fetch_component_logs
from metrics_parser import (
    build_traffic_report,
    extract_alerts,
    extract_ingress_summary,
    parse_prometheus,
)

app = Flask(__name__, static_folder="static", static_url_path="")

GATEWAYS: dict[str, dict[str, Any]] = {
    "internal": {
        "id": "internal",
        "label": "Internal Gateway",
        "cluster": "internal-gateway-service",
        "admin_host": os.getenv("INTERNAL_ADMIN_HOST", "internal-gateway-admin"),
        "admin_port": int(os.getenv("INTERNAL_ADMIN_PORT", "9901")),
        "data_port": 8080,
        "color": "#0ea5e9",
        "pod_label": os.getenv("INTERNAL_POD_LABEL", "name=internal-gateway"),
        "container": os.getenv("INTERNAL_CONTAINER", "internal-gateway"),
    },
    "private": {
        "id": "private",
        "label": "Private Gateway",
        "cluster": "private-gateway-service",
        "admin_host": os.getenv("PRIVATE_ADMIN_HOST", "private-gateway-admin"),
        "admin_port": int(os.getenv("PRIVATE_ADMIN_PORT", "9901")),
        "data_port": 8080,
        "color": "#8b5cf6",
        "pod_label": os.getenv("PRIVATE_POD_LABEL", "name=private-frontend-gateway"),
        "container": os.getenv("PRIVATE_CONTAINER", "private-frontend-gateway"),
    },
    "public": {
        "id": "public",
        "label": "Public Gateway",
        "cluster": "public-gateway-service",
        "admin_host": os.getenv("PUBLIC_ADMIN_HOST", "public-gateway-admin"),
        "admin_port": int(os.getenv("PUBLIC_ADMIN_PORT", "9901")),
        "data_port": 8080,
        "color": "#10b981",
        "pod_label": os.getenv("PUBLIC_POD_LABEL", "name=public-frontend-gateway"),
        "container": os.getenv("PUBLIC_CONTAINER", "public-frontend-gateway"),
    },
}

CP_API_HOST = os.getenv("CP_API_HOST", "control-plane")
CP_API_PORT = int(os.getenv("CP_API_PORT", "8080"))
CP_POD_LABEL = os.getenv("CP_POD_LABEL", "name=control-plane")
CP_CONTAINER = os.getenv("CP_CONTAINER", "control-plane")

ENVOY_ADMIN_PATHS = {
    "ready": "/ready",
    "server_info": "/server_info",
    "stats": "/stats?format=json",
    "clusters": "/clusters?format=json",
    "listeners": "/listeners?format=json",
    "config_dump": "/config_dump",
    "prometheus": "/stats/prometheus",
}

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "5"))
_log_client = K8sLogClient()
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)
    return value


def _fetch(url: str, timeout: float = 8.0) -> tuple[int, Any, str | None]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json, text/plain, */*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type or body.startswith("{") or body.startswith("["):
                try:
                    return resp.status, json.loads(body), None
                except json.JSONDecodeError:
                    return resp.status, body, None
            return resp.status, body, None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return exc.code, None, detail or exc.reason
    except Exception as exc:  # noqa: BLE001
        return 0, None, str(exc)


def _gateway(gateway_id: str) -> dict[str, Any] | None:
    return GATEWAYS.get(gateway_id)


def _admin_url(gateway: dict[str, Any], path: str) -> str:
    return f"http://{gateway['admin_host']}:{gateway['admin_port']}{path}"


def _extract_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    if not stats:
        return {}

    def find_value(suffix: str) -> int | None:
        for key, value in stats.items():
            if key.endswith(suffix) and isinstance(value, (int, float)):
                return int(value)
        return None

    return {
        "total_connections": find_value("downstream_cx_total"),
        "active_connections": find_value("downstream_cx_active"),
        "total_requests": find_value("downstream_rq_total"),
        "active_requests": find_value("downstream_rq_active"),
        "membership_healthy": find_value("membership_healthy"),
        "membership_total": find_value("membership_total"),
    }


def _gateway_traffic(gateway: dict[str, Any], tail_lines: int = 400) -> dict[str, Any]:
    cache_key = f"traffic:{gateway['id']}:{tail_lines}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    prom_status, prom_body, prom_err = _fetch(_admin_url(gateway, "/stats/prometheus"), timeout=12)
    cfg_status, cfg_body, cfg_err = _fetch(_admin_url(gateway, "/config_dump"), timeout=15)

    logs = fetch_component_logs(
        _log_client,
        gateway["pod_label"],
        gateway.get("container"),
        tail_lines=tail_lines,
    )

    path_stats_for_routes = []
    if logs.get("path_stats"):
        path_stats_for_routes = logs["path_stats"]

    report: dict[str, Any] = {
        "gateway": gateway["id"],
        "errors": [],
        "logs_available": _log_client.in_cluster,
    }
    if prom_err:
        report["errors"].append(f"prometheus: {prom_err}")
    if cfg_err:
        report["errors"].append(f"config_dump: {cfg_err}")
    if logs.get("error"):
        report["errors"].append(logs["error"])

    if prom_status and isinstance(prom_body, str):
        report.update(
            build_traffic_report(
                prom_body,
                cfg_body if cfg_status == HTTPStatus.OK else {},
                path_stats=path_stats_for_routes,
            )
        )
    else:
        report.update(
            {
                "ingress": {},
                "alerts": [],
                "clusters": [],
                "routes": [],
                "route_count": 0,
            }
        )

    report["path_stats"] = logs.get("path_stats", [])
    report["impact_log_count"] = len(logs.get("impact_logs", []))
    report["pods"] = logs.get("pods", [])
    return _cache_set(cache_key, report)


@app.get("/")
def index() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz() -> Any:
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.get("/readyz")
def readyz() -> Any:
    checks: dict[str, Any] = {}
    healthy = True
    for gateway in GATEWAYS.values():
        status, _, error = _fetch(_admin_url(gateway, "/ready"), timeout=3)
        ok = status == HTTPStatus.OK
        checks[gateway["id"]] = {"ready": ok, "status_code": status, "error": error}
        healthy = healthy and ok

    cp_status, _, cp_error = _fetch(f"http://{CP_API_HOST}:{CP_API_PORT}/ready", timeout=3)
    checks["control_plane"] = {
        "ready": cp_status == HTTPStatus.OK,
        "status_code": cp_status,
        "error": cp_error,
    }

    code = HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE
    return jsonify({"ready": healthy, "checks": checks}), code


@app.get("/api/gateways")
def list_gateways() -> Any:
    items = []
    for gateway in GATEWAYS.values():
        status, payload, error = _fetch(_admin_url(gateway, "/ready"))
        ready = status == HTTPStatus.OK and (
            payload == "LIVE" or (isinstance(payload, str) and "READY" in payload.upper())
        )
        items.append(
            {
                **{k: v for k, v in gateway.items() if k not in {"pod_label", "container"}},
                "ready": ready,
                "status_code": status,
                "error": error,
            }
        )
    return jsonify({"gateways": items})


@app.get("/api/gateways/<gateway_id>/<endpoint>")
def gateway_admin(gateway_id: str, endpoint: str) -> Any:
    gateway = _gateway(gateway_id)
    if not gateway:
        return jsonify({"error": f"Unknown gateway: {gateway_id}"}), HTTPStatus.NOT_FOUND

    path = ENVOY_ADMIN_PATHS.get(endpoint)
    if not path:
        return jsonify({"error": f"Unknown endpoint: {endpoint}"}), HTTPStatus.BAD_REQUEST

    status, payload, error = _fetch(_admin_url(gateway, path))
    if error and payload is None:
        return jsonify({"gateway": gateway_id, "endpoint": endpoint, "error": error}), HTTPStatus.BAD_GATEWAY

    return jsonify(
        {
            "gateway": gateway_id,
            "endpoint": endpoint,
            "status_code": status,
            "data": payload,
            "error": error,
        }
    )


@app.get("/api/gateways/<gateway_id>/traffic")
def gateway_traffic(gateway_id: str) -> Any:
    gateway = _gateway(gateway_id)
    if not gateway:
        return jsonify({"error": f"Unknown gateway: {gateway_id}"}), HTTPStatus.NOT_FOUND

    tail_lines = min(int(request.args.get("tail", 400)), 2000)
    report = _gateway_traffic(gateway, tail_lines=tail_lines)
    return jsonify(report)


@app.get("/api/gateways/<gateway_id>/logs")
def gateway_logs(gateway_id: str) -> Any:
    gateway = _gateway(gateway_id)
    if not gateway:
        return jsonify({"error": f"Unknown gateway: {gateway_id}"}), HTTPStatus.NOT_FOUND

    tail_lines = min(int(request.args.get("tail", 300)), 2000)
    only_impact = request.args.get("impact", "true").lower() != "false"

    cache_key = f"logs:{gateway_id}:{tail_lines}:{only_impact}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    logs = fetch_component_logs(
        _log_client,
        gateway["pod_label"],
        gateway.get("container"),
        tail_lines=tail_lines,
    )
    payload = {
        "gateway": gateway_id,
        "pods": logs.get("pods", []),
        "impact_logs": logs.get("impact_logs", []),
        "path_stats": logs.get("path_stats", []),
        "access_entries": logs.get("access_entries", 0),
        "logs_available": _log_client.in_cluster,
        "error": logs.get("error"),
        "fetched_at": logs.get("fetched_at"),
    }
    if not only_impact:
        payload["raw_lines"] = logs.get("lines", [])[-tail_lines:]

    return jsonify(_cache_set(cache_key, payload))


def _collect_impact_logs(tail_lines: int = 200, limit: int = 150) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    components = [
        ("control-plane", CP_POD_LABEL, CP_CONTAINER, "Control Plane"),
        *[
            (gw["id"], gw["pod_label"], gw.get("container"), gw["label"])
            for gw in GATEWAYS.values()
        ],
    ]

    for component_id, label, container, label_name in components:
        logs = fetch_component_logs(_log_client, label, container, tail_lines=tail_lines)
        for item in logs.get("impact_logs", []):
            entries.append({**item, "component": component_id, "component_label": label_name})

    entries.sort(
        key=lambda item: (
            item["severity"] != "critical",
            item["severity"] != "warning",
            item.get("timestamp", ""),
        )
    )
    return {
        "logs": entries[:limit],
        "count": len(entries),
        "logs_available": _log_client.in_cluster,
    }


@app.get("/api/logs/impact")
def impact_logs_all() -> Any:
    tail_lines = min(int(request.args.get("tail", 200)), 1000)
    return jsonify(_collect_impact_logs(tail_lines=tail_lines))


@app.get("/api/control-plane/<endpoint>")
def control_plane(endpoint: str) -> Any:
    paths = {
        "ready": "/ready",
        "health": "/health",
        "migration": "/api/v1/control-plane/system/migration-done",
    }
    path = paths.get(endpoint)
    if not path:
        return jsonify({"error": f"Unknown control-plane endpoint: {endpoint}"}), HTTPStatus.BAD_REQUEST

    url = f"http://{CP_API_HOST}:{CP_API_PORT}{path}"
    status, payload, error = _fetch(url)
    if error and payload is None:
        return jsonify({"endpoint": endpoint, "error": error}), HTTPStatus.BAD_GATEWAY

    return jsonify({"endpoint": endpoint, "status_code": status, "data": payload, "error": error})


@app.get("/api/overview")
def overview() -> Any:
    gateways = []
    total_alerts = 0

    for gateway in GATEWAYS.values():
        ready_status, ready_body, ready_err = _fetch(_admin_url(gateway, "/ready"))
        info_status, info_body, info_err = _fetch(_admin_url(gateway, "/server_info"))
        stats_status, stats_body, stats_err = _fetch(_admin_url(gateway, "/stats?format=json"))
        prom_status, prom_body, prom_err = _fetch(_admin_url(gateway, "/stats/prometheus"))

        key_metrics = _extract_metrics(stats_body if isinstance(stats_body, dict) else {})
        ingress_summary: dict[str, Any] = {}
        alerts: list[dict[str, Any]] = []

        if prom_status and isinstance(prom_body, str):
            metrics = parse_prometheus(prom_body)
            ingress_summary = extract_ingress_summary(metrics)
            alerts = extract_alerts(metrics)
            total_alerts += len(alerts)

        traffic_preview = _gateway_traffic(gateway, tail_lines=200)
        top_routes = (traffic_preview.get("routes") or [])[:5]
        top_paths = (traffic_preview.get("path_stats") or [])[:5]

        gateways.append(
            {
                "id": gateway["id"],
                "label": gateway["label"],
                "cluster": gateway["cluster"],
                "color": gateway["color"],
                "ready": ready_status == HTTPStatus.OK,
                "ready_detail": ready_body,
                "ready_error": ready_err,
                "server_info": info_body if info_status == HTTPStatus.OK else None,
                "server_info_error": info_err,
                "metrics": key_metrics,
                "ingress": ingress_summary,
                "alerts": alerts,
                "alert_count": len(alerts),
                "top_routes": top_routes,
                "top_paths": top_paths,
                "impact_log_count": traffic_preview.get("impact_log_count", 0),
                "stats_error": stats_err or prom_err,
            }
        )

    cp_status, cp_body, cp_err = _fetch(f"http://{CP_API_HOST}:{CP_API_PORT}/ready")

    impact = _collect_impact_logs(tail_lines=150, limit=20)["logs"] if _log_client.in_cluster else []

    return jsonify(
        {
            "gateways": gateways,
            "control_plane": {
                "ready": cp_status == HTTPStatus.OK,
                "data": cp_body,
                "error": cp_err,
            },
            "total_alerts": total_alerts,
            "impact_logs": impact,
            "logs_available": _log_client.in_cluster,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)