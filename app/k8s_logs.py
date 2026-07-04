"""Fetch and parse Kubernetes pod logs for production triage."""

from __future__ import annotations

import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

IMPACT_PATTERNS = [
    (re.compile(r"\b(ERROR|FATAL|CRITICAL)\b", re.I), "error"),
    (re.compile(r"\b(WARN|WARNING)\b", re.I), "warning"),
    (re.compile(r"Response:\s*\"(\d{3})\s+(NR|UF|UH|UO|DC|UH|UC|UT|LR|UR)", re.I), "warning"),
    (re.compile(r"Response:\s*\"([45]\d{2})\b"), "warning"),
    (re.compile(r"\b(connect fail|connection refused|upstream reset|no healthy upstream|timeout|xds|cds|lds|rds)\b", re.I), "warning"),
    (re.compile(r"\b(503|502|504|500)\b"), "critical"),
]

REQUEST_LINE_RE = re.compile(
    r'Forward Request:\s*"(?P<method>[A-Z]+)\s+(?P<path>[^\s?"]+)',
    re.I,
)
RESPONSE_LINE_RE = re.compile(
    r'Response:\s*"(?P<code>\d{3})\s+(?P<flags>[A-Z]*)\s+(?P<body>\d+)\s+(?P<upstream>\d+)\s+(?P<duration>[^"]+)"',
    re.I,
)
TIMESTAMP_RE = re.compile(r"\[(?P<ts>[^\]]+)\]")
REQUEST_ID_RE = re.compile(r"request_id=(?P<id>[^\]]+)")

CODE_CLASS = {
    "1": "1xx", "2": "2xx", "3": "3xx", "4": "4xx", "5": "5xx",
}


class K8sLogClient:
    def __init__(self) -> None:
        self.namespace = os.getenv("POD_NAMESPACE", "qubership-mesh")
        self.host = os.getenv("KUBERNETES_SERVICE_HOST", "")
        self.port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        self.token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        self.ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self._token: str | None = None
        self._ssl_context: ssl.SSLContext | None = None

    @property
    def in_cluster(self) -> bool:
        return bool(self.host and os.path.exists(self.token_path))

    def _load_credentials(self) -> None:
        if self._token is None and os.path.exists(self.token_path):
            with open(self.token_path, encoding="utf-8") as handle:
                self._token = handle.read().strip()
        if self._ssl_context is None and os.path.exists(self.ca_path):
            self._ssl_context = ssl.create_default_context(cafile=self.ca_path)

    def _request(self, path: str, params: dict[str, str] | None = None) -> tuple[int, str | None, str | None]:
        if not self.in_cluster:
            return 0, None, "Kubernetes API not available (not running in cluster)"

        self._load_credentials()
        query = ""
        if params:
            query = "?" + "&".join(f"{key}={value}" for key, value in params.items())

        url = f"https://{self.host}:{self.port}{path}{query}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_context, timeout=12) as response:
                body = response.read().decode("utf-8", errors="replace")
                if path.endswith("/log"):
                    return response.status, body, None
                return response.status, body, None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return exc.code, None, detail or exc.reason
        except Exception as exc:  # noqa: BLE001
            return 0, None, str(exc)

    def list_pods(self, label_selector: str) -> list[dict[str, Any]]:
        status, body, error = self._request(
            f"/api/v1/namespaces/{self.namespace}/pods",
            {"labelSelector": label_selector},
        )
        if error or not body:
            return []
        import json

        payload = json.loads(body)
        pods = []
        for item in payload.get("items", []):
            pods.append(
                {
                    "name": item["metadata"]["name"],
                    "phase": item["status"].get("phase", "Unknown"),
                    "containers": [c["name"] for c in item["spec"].get("containers", [])],
                }
            )
        return pods

    def get_pod_logs(
        self,
        pod_name: str,
        container: str | None = None,
        tail_lines: int = 300,
    ) -> tuple[list[str], str | None]:
        params = {"tailLines": str(tail_lines)}
        if container:
            params["container"] = container
        status, body, error = self._request(
            f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}/log",
            params,
        )
        if error:
            return [], error
        if not body:
            return [], None
        return body.splitlines(), None


def classify_log_line(line: str) -> str | None:
    for pattern, severity in IMPACT_PATTERNS:
        if pattern.search(line):
            return severity
    return None


def parse_envoy_access_blocks(lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    for line in lines:
        ts_match = TIMESTAMP_RE.search(line)
        if ts_match:
            current["timestamp"] = ts_match.group("ts")
        rid_match = REQUEST_ID_RE.search(line)
        if rid_match:
            current["request_id"] = rid_match.group("id")

        req_match = REQUEST_LINE_RE.search(line)
        if req_match:
            if current.get("method"):
                entries.append(current)
            current = {
                "timestamp": current.get("timestamp", ""),
                "request_id": current.get("request_id", ""),
                "method": req_match.group("method"),
                "path": req_match.group("path"),
            }
            continue

        resp_match = RESPONSE_LINE_RE.search(line)
        if resp_match and current.get("method"):
            current.update(
                {
                    "code": int(resp_match.group("code")),
                    "flags": resp_match.group("flags"),
                    "duration": resp_match.group("duration"),
                    "body_bytes": int(resp_match.group("body")),
                }
            )
            entries.append(current)
            current = {}

    return entries


def aggregate_path_stats(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}

    for entry in entries:
        path = entry.get("path", "")
        if not path:
            continue
        if path not in stats:
            stats[path] = {
                "path": path,
                "total": 0,
                "codes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0},
                "no_route": 0,
                "upstream_fail": 0,
                "last_code": None,
                "last_seen": entry.get("timestamp", ""),
            }
        row = stats[path]
        row["total"] += 1
        code = entry.get("code")
        if code:
            row["last_code"] = code
            code_class = CODE_CLASS.get(str(code)[0], "other")
            if code_class in row["codes"]:
                row["codes"][code_class] += 1
        flags = entry.get("flags", "")
        if "NR" in flags:
            row["no_route"] += 1
        if any(flag in flags for flag in ("UF", "UH", "UO", "UC", "UT")):
            row["upstream_fail"] += 1
        if entry.get("timestamp"):
            row["last_seen"] = entry["timestamp"]

    result = list(stats.values())
    result.sort(key=lambda item: (item["codes"]["5xx"] + item["codes"]["4xx"], item["total"]), reverse=True)
    return result


def prefix_match_stats(path_stats: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    matched = [item for item in path_stats if item["path"].startswith(prefix)]
    if not matched:
        return {"total": 0, "codes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}, "no_route": 0}

    totals = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    total = 0
    no_route = 0
    for item in matched:
        total += item["total"]
        no_route += item.get("no_route", 0)
        for key in totals:
            totals[key] += item["codes"].get(key, 0)
    return {"total": total, "codes": totals, "no_route": no_route}


def filter_impact_logs(lines: list[str], limit: int = 100) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for line in lines:
        severity = classify_log_line(line)
        if not severity:
            continue
        ts_match = TIMESTAMP_RE.search(line)
        req_match = REQUEST_LINE_RE.search(line)
        resp_match = RESPONSE_LINE_RE.search(line)
        results.append(
            {
                "severity": severity,
                "timestamp": ts_match.group("ts") if ts_match else "",
                "message": line.strip()[:500],
                "path": req_match.group("path") if req_match else "",
                "code": int(resp_match.group("code")) if resp_match else None,
            }
        )
    results.sort(key=lambda item: (item["severity"] != "critical", item["severity"] != "warning"))
    return results[:limit]


def fetch_component_logs(
    client: K8sLogClient,
    label_selector: str,
    container: str | None = None,
    tail_lines: int = 400,
) -> dict[str, Any]:
    pods = client.list_pods(label_selector)
    if not pods:
        return {
            "pods": [],
            "lines": [],
            "impact_logs": [],
            "path_stats": [],
            "error": f"No pods found for selector: {label_selector}",
        }

    all_lines: list[str] = []
    pod_names: list[str] = []
    errors: list[str] = []

    for pod in pods:
        if pod["phase"] != "Running":
            continue
        container_name = container or (pod["containers"][0] if pod["containers"] else None)
        lines, error = client.get_pod_logs(pod["name"], container_name, tail_lines)
        if error:
            errors.append(f"{pod['name']}: {error}")
            continue
        pod_names.append(pod["name"])
        all_lines.extend(lines)

    access_entries = parse_envoy_access_blocks(all_lines)
    path_stats = aggregate_path_stats(access_entries)
    impact_logs = filter_impact_logs(all_lines)

    return {
        "pods": pod_names,
        "lines": all_lines[-tail_lines:],
        "impact_logs": impact_logs,
        "path_stats": path_stats,
        "access_entries": len(access_entries),
        "error": "; ".join(errors) if errors else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }