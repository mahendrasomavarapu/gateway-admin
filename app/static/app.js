const state = {
  gateways: [],
  overview: null,
  selectedId: null,
  refreshTimer: null,
  trafficFilter: "",
};

const els = {
  gatewayNav: document.getElementById("gateway-nav"),
  gatewayCards: document.getElementById("gateway-cards"),
  summaryBar: document.getElementById("summary-bar"),
  impactLogsPanel: document.getElementById("impact-logs-panel"),
  impactMeta: document.getElementById("impact-meta"),
  dashboardView: document.getElementById("dashboard-view"),
  detailView: document.getElementById("detail-view"),
  pageTitle: document.getElementById("page-title"),
  pageSubtitle: document.getElementById("page-subtitle"),
  lastUpdated: document.getElementById("last-updated"),
  refreshBtn: document.getElementById("refresh-btn"),
  autoRefresh: document.getElementById("auto-refresh"),
  cpStatus: document.getElementById("cp-status"),
  detailBadge: document.getElementById("detail-badge"),
  detailName: document.getElementById("detail-name"),
  detailCluster: document.getElementById("detail-cluster"),
  detailStatus: document.getElementById("detail-status"),
  detailMetrics: document.getElementById("detail-metrics"),
  detailAlerts: document.getElementById("detail-alerts"),
  panels: {
    traffic: document.getElementById("panel-traffic"),
    logs: document.getElementById("panel-logs"),
    overview: document.getElementById("panel-overview"),
    stats: document.getElementById("panel-stats"),
    clusters: document.getElementById("panel-clusters"),
    listeners: document.getElementById("panel-listeners"),
    config: document.getElementById("panel-config"),
  },
};

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return response.json();
}

function formatNumber(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString();
}

function metricBlock(label, value, tone) {
  const cls = tone ? ` metric-${tone}` : "";
  return `<div class="metric${cls}"><label>${label}</label><strong>${formatNumber(value)}</strong></div>`;
}

function codePill(label, value, tone) {
  const cls = value > 0 ? `code-pill ${tone}` : "code-pill muted";
  return `<span class="${cls}">${label}: ${formatNumber(value)}</span>`;
}

function severityClass(severity) {
  if (severity === "critical") return "sev-critical";
  if (severity === "warning") return "sev-warning";
  return "sev-info";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderNav() {
  const dashboardBtn = `
    <button class="nav-item ${state.selectedId ? "" : "active"}" data-view="dashboard" type="button">
      <span>Dashboard</span>
    </button>`;

  const gatewayButtons = state.gateways
    .map((gw) => {
      const overview = (state.overview?.gateways || []).find((g) => g.id === gw.id);
      const alertCount = overview?.alert_count || 0;
      const alertDot = alertCount > 0 ? `<span class="nav-alert">${alertCount}</span>` : "";
      return `
      <button class="nav-item ${state.selectedId === gw.id ? "active" : ""}" data-view="gateway" data-id="${gw.id}" type="button">
        <span><span class="nav-dot" style="background:${gw.color}"></span>${gw.label}${alertDot}</span>
        <span class="dot ${gw.ready ? "ok" : "bad"}"></span>
      </button>`;
    })
    .join("");

  els.gatewayNav.innerHTML = dashboardBtn + gatewayButtons;
}

function renderSummaryBar() {
  const gateways = state.overview?.gateways || [];
  let totalReq = 0;
  let total4xx = 0;
  let total5xx = 0;
  let totalAlerts = state.overview?.total_alerts || 0;

  gateways.forEach((gw) => {
    const ingress = gw.ingress || {};
    totalReq += ingress.total_requests || 0;
    total4xx += ingress.codes?.["4xx"] || 0;
    total5xx += ingress.codes?.["5xx"] || 0;
  });

  els.summaryBar.innerHTML = [
    metricBlock("Total ingress requests", totalReq),
    metricBlock("4xx responses", total4xx, total4xx > 0 ? "warn" : ""),
    metricBlock("5xx responses", total5xx, total5xx > 0 ? "bad" : ""),
    metricBlock("Active alerts", totalAlerts, totalAlerts > 0 ? "warn" : ""),
    metricBlock("Gateways ready", `${gateways.filter((g) => g.ready).length}/${gateways.length}`),
  ].join("");
}

function renderImpactLogs(logs, target) {
  if (!logs || logs.length === 0) {
    target.innerHTML = `<div class="empty">No impact logs detected in recent pod output.</div>`;
    return;
  }

  const rows = logs
    .map(
      (log) => `
      <tr class="${severityClass(log.severity)}">
        <td><span class="severity-tag ${severityClass(log.severity)}">${escapeHtml(log.severity)}</span></td>
        <td>${escapeHtml(log.component_label || log.component || "—")}</td>
        <td><code>${escapeHtml(log.path || "—")}</code></td>
        <td>${log.code ? `<span class="code-pill bad">${log.code}</span>` : "—"}</td>
        <td class="log-msg">${escapeHtml(log.message)}</td>
      </tr>`
    )
    .join("");

  target.innerHTML = `
    <div class="table-wrap">
      <table class="logs-table">
        <thead>
          <tr><th>Severity</th><th>Component</th><th>Path</th><th>Code</th><th>Message</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderCards() {
  const template = document.getElementById("card-template");
  els.gatewayCards.innerHTML = "";

  const overviewMap = new Map((state.overview?.gateways || []).map((g) => [g.id, g]));

  state.gateways.forEach((gw) => {
    const node = template.content.cloneNode(true);
    const overview = overviewMap.get(gw.id);
    const metrics = overview?.metrics || {};
    const ingress = overview?.ingress || {};
    const codes = ingress.codes || {};

    node.querySelector(".card-dot").style.background = gw.color;
    node.querySelector(".card-title").textContent = gw.label;
    node.querySelector(".card-cluster").textContent = gw.cluster;

    const alertBadge = node.querySelector(".alert-badge");
    if (overview?.alert_count > 0) {
      alertBadge.textContent = `${overview.alert_count} alert${overview.alert_count > 1 ? "s" : ""}`;
      alertBadge.classList.remove("hidden");
    }

    node.querySelector(".card-codes").innerHTML = [
      codePill("2xx", codes["2xx"] || 0, "ok"),
      codePill("3xx", codes["3xx"] || 0, "info"),
      codePill("4xx", codes["4xx"] || 0, "warn"),
      codePill("5xx", codes["5xx"] || 0, "bad"),
    ].join("");

    node.querySelector(".card-metrics").innerHTML = [
      metricBlock("Ingress requests", ingress.total_requests),
      metricBlock("No route", ingress.no_route, ingress.no_route > 0 ? "warn" : ""),
      metricBlock("Impact logs", overview?.impact_log_count || 0),
      metricBlock("Healthy upstreams", metrics.membership_healthy),
    ].join("");

    node.querySelector(".card-link").addEventListener("click", () => selectGateway(gw.id));
    els.gatewayCards.appendChild(node);
  });
}

function renderControlPlaneStatus() {
  const cp = state.overview?.control_plane;
  const dot = els.cpStatus.querySelector(".dot");
  const ready = cp?.ready;
  dot.className = `dot ${ready ? "ok" : "bad"}`;
  els.cpStatus.lastChild.textContent = ready ? "Control Plane: Ready" : "Control Plane: Unavailable";
}

function showDashboard() {
  state.selectedId = null;
  els.dashboardView.classList.remove("hidden");
  els.detailView.classList.add("hidden");
  els.pageTitle.textContent = "Dashboard";
  els.pageSubtitle.textContent = "Production triage for Envoy ingress gateways";
  renderNav();
}

async function selectGateway(id) {
  state.selectedId = id;
  const gateway = state.gateways.find((g) => g.id === id);
  if (!gateway) return;

  els.dashboardView.classList.add("hidden");
  els.detailView.classList.remove("hidden");
  els.pageTitle.textContent = gateway.label;
  els.pageSubtitle.textContent = "Per-URL traffic, HTTP codes, and impact logs";
  els.detailBadge.style.background = gateway.color;
  els.detailName.textContent = gateway.label;
  els.detailCluster.textContent = gateway.cluster;
  renderNav();
  await loadGatewayDetail(id);
}

function renderAlerts(alerts, target) {
  if (!alerts || alerts.length === 0) {
    target.innerHTML = `<div class="alert-clear">No active alerts — gateway looks healthy.</div>`;
    return;
  }

  target.innerHTML = alerts
    .map(
      (alert) => `
      <div class="alert-chip ${severityClass(alert.severity)}">
        <span class="severity-tag ${severityClass(alert.severity)}">${escapeHtml(alert.severity)}</span>
        <span>${escapeHtml(alert.category)}: ${escapeHtml(alert.message)}</span>
        <strong>${formatNumber(alert.count)}</strong>
      </div>`
    )
    .join("");
}

function renderTrafficPanel(traffic) {
  const panel = els.panels.traffic;
  if (!traffic) {
    panel.innerHTML = `<div class="empty">Loading traffic data…</div>`;
    return;
  }

  const ingress = traffic.ingress || {};
  const codes = ingress.codes || {};
  const filter = state.trafficFilter.toLowerCase();

  const routes = (traffic.routes || []).filter((row) => {
    if (!filter) return true;
    return (
      row.url_prefix.toLowerCase().includes(filter) ||
      row.service.toLowerCase().includes(filter) ||
      row.cluster.toLowerCase().includes(filter)
    );
  });

  const pathStats = (traffic.path_stats || []).filter((row) => {
    if (!filter) return true;
    return row.path.toLowerCase().includes(filter);
  });

  const routeRows = routes
    .map((row) => {
      const rowCodes = row.codes || {};
      const issue = row.has_issues ? "row-issue" : "";
      return `
      <tr class="${issue}">
        <td><code>${escapeHtml(row.url_prefix)}</code></td>
        <td>${escapeHtml(row.service)}</td>
        <td>${formatNumber(row.requests)}</td>
        <td class="code-cell">${codePill("2xx", rowCodes["2xx"] || 0, "ok")}</td>
        <td class="code-cell">${codePill("3xx", rowCodes["3xx"] || 0, "info")}</td>
        <td class="code-cell">${codePill("4xx", rowCodes["4xx"] || 0, "warn")}</td>
        <td class="code-cell">${codePill("5xx", rowCodes["5xx"] || 0, "bad")}</td>
        <td>${row.connect_fail ? `<span class="code-pill bad">fail ${row.connect_fail}</span>` : "—"}</td>
        <td>${row.healthy !== null && row.healthy !== undefined ? `${row.healthy}/${row.total_hosts ?? "?"}` : "—"}</td>
      </tr>`;
    })
    .join("");

  const pathRows = pathStats
    .slice(0, 50)
    .map((row) => {
      const rowCodes = row.codes || {};
      return `
      <tr>
        <td><code>${escapeHtml(row.path)}</code></td>
        <td>${formatNumber(row.total)}</td>
        <td class="code-cell">${codePill("2xx", rowCodes["2xx"] || 0, "ok")}</td>
        <td class="code-cell">${codePill("4xx", rowCodes["4xx"] || 0, "warn")}</td>
        <td class="code-cell">${codePill("5xx", rowCodes["5xx"] || 0, "bad")}</td>
        <td>${row.no_route ? `<span class="code-pill warn">NR ${row.no_route}</span>` : "—"}</td>
        <td>${row.last_code ? `<span class="code-pill">${row.last_code}</span>` : "—"}</td>
      </tr>`;
    })
    .join("");

  panel.innerHTML = `
    <div class="traffic-summary">
      ${metricBlock("Ingress total", ingress.total_requests)}
      ${metricBlock("Active", ingress.active_requests)}
      ${metricBlock("Error rate", ingress.error_rate !== undefined ? `${ingress.error_rate}%` : "—", ingress.error_rate > 5 ? "bad" : "")}
      ${metricBlock("No route", ingress.no_route, ingress.no_route > 0 ? "warn" : "")}
      ${metricBlock("Routes", traffic.route_count)}
    </div>
    <div class="code-bar">
      ${codePill("2xx", codes["2xx"] || 0, "ok")}
      ${codePill("3xx", codes["3xx"] || 0, "info")}
      ${codePill("4xx", codes["4xx"] || 0, "warn")}
      ${codePill("5xx", codes["5xx"] || 0, "bad")}
    </div>
    <div class="filter-row">
      <input type="search" id="traffic-filter" placeholder="Filter by URL prefix, service, or cluster…" value="${escapeHtml(state.trafficFilter)}" />
      <span class="filter-meta">${routes.length} routes · ${pathStats.length} observed paths · pods: ${(traffic.pods || []).join(", ") || "—"}</span>
    </div>
    <h4 class="panel-subtitle">Configured routes (prefix → upstream)</h4>
    <div class="table-wrap">
      <table class="traffic-table">
        <thead>
          <tr>
            <th>URL Prefix</th><th>Service</th><th>Requests</th>
            <th>2xx</th><th>3xx</th><th>4xx</th><th>5xx</th><th>Upstream</th><th>Health</th>
          </tr>
        </thead>
        <tbody>${routeRows || `<tr><td colspan="9" class="empty">No routes match filter.</td></tr>`}</tbody>
      </table>
    </div>
    <h4 class="panel-subtitle">Observed request paths (from access logs)</h4>
    <div class="table-wrap">
      <table class="traffic-table">
        <thead>
          <tr><th>Path</th><th>Requests</th><th>2xx</th><th>4xx</th><th>5xx</th><th>No Route</th><th>Last Code</th></tr>
        </thead>
        <tbody>${pathRows || `<tr><td colspan="7" class="empty">No path stats from recent logs.</td></tr>`}</tbody>
      </table>
    </div>`;

  const filterInput = panel.querySelector("#traffic-filter");
  filterInput?.addEventListener("input", (event) => {
    state.trafficFilter = event.target.value;
    renderTrafficPanel(traffic);
  });
}

async function loadGatewayDetail(id) {
  const overview = (state.overview?.gateways || []).find((g) => g.id === id);
  const ready = overview?.ready;
  els.detailStatus.textContent = ready ? "Ready" : "Not Ready";
  els.detailStatus.className = `status-pill ${ready ? "ok" : "bad"}`;

  const metrics = overview?.metrics || {};
  const ingress = overview?.ingress || {};
  els.detailMetrics.innerHTML = [
    metricBlock("Ingress requests", ingress.total_requests),
    metricBlock("4xx", ingress.codes?.["4xx"], ingress.codes?.["4xx"] > 0 ? "warn" : ""),
    metricBlock("5xx", ingress.codes?.["5xx"], ingress.codes?.["5xx"] > 0 ? "bad" : ""),
    metricBlock("No route", ingress.no_route, ingress.no_route > 0 ? "warn" : ""),
    metricBlock("Active connections", metrics.active_connections),
    metricBlock("Healthy upstreams", metrics.membership_healthy),
  ].join("");

  renderAlerts(overview?.alerts || [], els.detailAlerts);

  const [traffic, logs] = await Promise.all([
    api(`/api/gateways/${id}/traffic`).catch((err) => ({ error: err.message })),
    api(`/api/gateways/${id}/logs?tail=300`).catch((err) => ({ error: err.message, impact_logs: [] })),
    loadOverviewPanel(id, overview),
    loadEndpointPanel(id, "stats", "stats"),
    loadEndpointPanel(id, "clusters", "clusters"),
    loadEndpointPanel(id, "listeners", "listeners"),
    loadEndpointPanel(id, "config_dump", "config"),
  ]);

  if (traffic.error) {
    els.panels.traffic.innerHTML = `<div class="error-box">${escapeHtml(traffic.error)}</div>`;
  } else {
    renderTrafficPanel(traffic);
  }

  if (logs.error && !logs.impact_logs?.length) {
    els.panels.logs.innerHTML = `<div class="error-box">${escapeHtml(logs.error)}</div>`;
  } else {
    renderImpactLogs(logs.impact_logs || [], els.panels.logs);
  }
}

async function loadOverviewPanel(id, overview) {
  const panel = els.panels.overview;
  const info = overview?.server_info || {};
  const version = info?.version || info?.node?.version || "—";
  const uptime = info?.uptime_current_epoch || info?.uptime_all_epochs || "—";
  const nodeId = info?.node?.id || info?.node?.cluster || "—";

  panel.innerHTML = `
    <div class="kv-grid">
      <div class="kv"><label>Ready</label><span>${overview?.ready ? "Yes" : "No"}</span></div>
      <div class="kv"><label>Envoy version</label><span>${version}</span></div>
      <div class="kv"><label>Node ID</label><span>${nodeId}</span></div>
      <div class="kv"><label>Uptime (epoch)</label><span>${uptime}</span></div>
      <div class="kv"><label>Admin cluster</label><code>${overview?.cluster || id}</code></div>
      <div class="kv"><label>Ready response</label><code>${escapeHtml(String(overview?.ready_detail ?? "—"))}</code></div>
    </div>`;
}

async function loadEndpointPanel(id, endpoint, panelKey) {
  const panel = els.panels[panelKey];
  panel.innerHTML = `<div class="empty">Loading ${endpoint}…</div>`;

  try {
    const result = await api(`/api/gateways/${id}/${endpoint}`);
    const data = result.data;

    if (endpoint === "stats" && typeof data === "object") {
      const rows = Object.entries(data)
        .filter(([key]) => !key.includes("."))
        .slice(0, 80)
        .map(([key, value]) => `<tr><td>${escapeHtml(key)}</td><td>${formatNumber(value)}</td></tr>`)
        .join("");
      panel.innerHTML = rows
        ? `<div class="table-wrap"><table><thead><tr><th>Stat</th><th>Value</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty">No stats available.</div>`;
      return;
    }

    if ((endpoint === "clusters" || endpoint === "listeners") && data?.cluster_statuses) {
      const clusters = data.cluster_statuses || [];
      const rows = clusters
        .map((cluster) => {
          const healthy = cluster.host_statuses?.filter((h) => h.health_status?.healthy).length || 0;
          const total = cluster.host_statuses?.length || 0;
          return `<tr><td>${escapeHtml(cluster.name || "—")}</td><td>${healthy}/${total}</td><td>${escapeHtml(cluster.observed_state || "—")}</td></tr>`;
        })
        .join("");
      panel.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Cluster</th><th>Healthy Hosts</th><th>State</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      return;
    }

    if (endpoint === "listeners" && Array.isArray(data?.listener_statuses)) {
      const rows = data.listener_statuses
        .map((listener) => `<tr><td>${escapeHtml(listener.name || "—")}</td><td>${escapeHtml(listener.local_address?.socket_address?.address || "—")}</td><td>${listener.active_state?.version_info || "—"}</td></tr>`)
        .join("");
      panel.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Listener</th><th>Address</th><th>Version</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      return;
    }

    panel.innerHTML = `<pre class="code-block">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
  } catch (error) {
    panel.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
  }
}

async function refreshAll() {
  try {
    const [gateways, overview] = await Promise.all([
      api("/api/gateways"),
      api("/api/overview"),
    ]);
    state.gateways = gateways.gateways;
    state.overview = overview;
    els.lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderControlPlaneStatus();
    renderSummaryBar();
    renderImpactLogs(overview.impact_logs || [], els.impactLogsPanel);
    els.impactMeta.textContent = overview.logs_available
      ? `${overview.impact_logs?.length || 0} recent impact events across gateways and control-plane`
      : "Log access unavailable (requires in-cluster ServiceAccount)";

    if (state.selectedId) {
      renderNav();
      await loadGatewayDetail(state.selectedId);
    } else {
      renderNav();
      renderCards();
    }
  } catch (error) {
    els.lastUpdated.textContent = `Refresh failed: ${error.message}`;
  }
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      els.panels[tab.dataset.tab].classList.add("active");
    });
  });
}

function setupEvents() {
  els.refreshBtn.addEventListener("click", refreshAll);
  els.autoRefresh.addEventListener("change", scheduleRefresh);
  els.gatewayNav.addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (!button) return;
    if (button.dataset.view === "dashboard") {
      showDashboard();
      return;
    }
    if (button.dataset.id) {
      selectGateway(button.dataset.id);
    }
  });
}

function scheduleRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  if (els.autoRefresh.checked) {
    state.refreshTimer = setInterval(refreshAll, 10000);
  }
}

setupTabs();
setupEvents();
scheduleRefresh();
refreshAll();