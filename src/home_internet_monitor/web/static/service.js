const params = new URLSearchParams(window.location.search);
const monitorId = params.get("id");
let selectedWindow = "7d";
let allSettings = null;
let serviceSettings = null;
let lastSummary = null;
let manualResult = null;
let checking = false;
let loadSequence = 0;
let activeLoads = 0;
let pingPayload = null;
let windowChosen = false;

const elements = {
  error: document.querySelector("#service-error"),
  name: document.querySelector("#service-name"),
  current: document.querySelector("#service-current"),
  status: document.querySelector("#service-status"),
  checked: document.querySelector("#service-checked"),
  availability: document.querySelector("#service-availability"),
  availabilityNote: document.querySelector("#service-availability-note"),
  windowDescription: document.querySelector("#service-window-description"),
  duration: document.querySelector("#service-duration"),
  incident: document.querySelector("#service-last-incident"),
  incidentTime: document.querySelector("#service-last-incident-time"),
  latency: document.querySelector("#service-latency"),
  timeline: document.querySelector("#service-detail-timeline"),
  pingChart: document.querySelector("#service-ping-chart"),
  pingEmpty: document.querySelector("#service-ping-empty"),
  pingTooltip: document.querySelector("#service-ping-tooltip"),
  start: document.querySelector("#service-timeline-start"),
  end: document.querySelector("#service-timeline-end"),
  incidents: document.querySelector("#service-incident-list"),
  settingsForm: document.querySelector("#service-settings-form"),
  settingsMessage: document.querySelector("#service-settings-message"),
  saveSettings: document.querySelector("#save-service-settings"),
  interval: document.querySelector("#service-interval"),
  timeout: document.querySelector("#service-timeout"),
  failureSeconds: document.querySelector("#service-failure-seconds"),
  failureNote: document.querySelector("#service-failure-note"),
  recoverySeconds: document.querySelector("#service-recovery-seconds"),
  recoveryNote: document.querySelector("#service-recovery-note"),
  useIcmp: document.querySelector("#service-use-icmp"),
  endpoint: document.querySelector("#service-endpoint"),
  useStatus: document.querySelector("#service-use-status"),
  statusFile: document.querySelector("#service-status-file"),
  clientName: document.querySelector("#service-client-name"),
  dashboardMode: document.querySelector("#service-dashboard-mode"),
  defaultWindow: document.querySelector("#service-default-window"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_error) {}
    throw new Error(message);
  }
  return response.json();
}

function range() {
  const hours = { "1h": 1, "24h": 24, "7d": 168 }[selectedWindow];
  const end = new Date();
  return { start: new Date(end.getTime() - hours * 3600 * 1000), end };
}

async function load() {
  if (!monitorId) throw new Error("No service monitor was selected");
  const sequence = ++loadSequence;
  activeLoads += 1;
  try {
    if (!serviceSettings) {
      const settings = allSettings || await api("/api/v1/settings");
      if (sequence !== loadSequence) return;
      allSettings = settings;
      serviceSettings = settings.service_monitors.find(item => item.id === monitorId);
      if (!serviceSettings) throw new Error("Service settings are unavailable");
      if (!windowChosen) selectWindow(serviceSettings.default_window || "7d");
      renderSettings(serviceSettings);
    }
    const selected = range();
    const query = new URLSearchParams({ start: selected.start.toISOString(), end: selected.end.toISOString() });
    const pingQuery = new URLSearchParams({
      start: selected.start.toISOString(),
      end: selected.end.toISOString(),
      bucket_seconds: { "1h": 60, "24h": 300, "7d": 1800 }[selectedWindow],
    });
    const [summary, timeline, incidents, ping] = await Promise.all([
      api(`/api/v1/services/${encodeURIComponent(monitorId)}`),
      api(`/api/v1/services/${encodeURIComponent(monitorId)}/timeline?${query}`),
      api(`/api/v1/services/${encodeURIComponent(monitorId)}/incidents?limit=20`),
      api(`/api/v1/services/${encodeURIComponent(monitorId)}/latency?${pingQuery}`),
    ]);
    if (sequence !== loadSequence) return;
    lastSummary = summary;
    if (manualResult && summary.last_checked && new Date(summary.last_checked) >= new Date(manualResult.last_checked)) {
      manualResult = null;
    }
    renderSummary(summary);
    renderTimeline(timeline);
    renderIncidents(incidents.items);
    pingPayload = ping;
    renderPing(ping);
    elements.error.hidden = true;
  } finally {
    activeLoads -= 1;
  }
}

function renderSummary(item) {
  document.title = `${item.label} · Network monitor`;
  elements.name.textContent = item.label;
  const latest = manualResult || item;
  elements.current.dataset.status = latest.status;
  elements.status.textContent = statusLabel(latest.status);
  elements.current.disabled = checking || !serviceSettings?.enabled;
  elements.checked.textContent = checking ? "Checking..." : latest.last_checked ? `Checked ${relativeTime(latest.last_checked)}` : "Not checked yet";
  elements.duration.textContent = item.stable_since ? formatDuration((Date.now() - new Date(item.stable_since).getTime()) / 1000) : "No data";
  elements.latency.textContent = latest.last_latency_ms == null ? "No data" : `${Math.round(latest.last_latency_ms)} ms`;
  if (item.last_incident) {
    elements.incident.textContent = item.last_incident.duration_seconds === null ? "Ongoing" : formatDuration(item.last_incident.duration_seconds);
    elements.incidentTime.textContent = formatDate(item.last_incident.start, true);
  } else {
    elements.incident.textContent = "None";
    elements.incidentTime.textContent = "No confirmed incidents";
  }
}

function renderTimeline(payload) {
  elements.timeline.replaceChildren();
  const start = new Date(payload.start).getTime();
  const end = new Date(payload.end).getTime();
  const totals = { up: 0, degraded: 0, down: 0, unknown: 0 };
  const segments = fillTimelineGaps(payload.segments || [], start, end);
  segments.forEach((segment) => {
    const segmentStart = new Date(segment.start).getTime();
    const segmentEnd = new Date(segment.end).getTime();
    const duration = Math.max(0, segmentEnd - segmentStart);
    totals[segment.status] += duration;
    const node = document.createElement("i");
    node.dataset.status = segment.status;
    node.style.flexGrow = String(Math.max(1, duration));
    node.setAttribute("title", `${statusLabel(segment.status)} · ${formatDate(segment.start, true)} to ${formatDate(segment.end, true)}`);
    elements.timeline.appendChild(node);
  });
  const classified = totals.up + totals.degraded + totals.down;
  const coverage = classified / Math.max(1, end - start);
  elements.availability.textContent = classified ? `${(totals.up / classified * 100).toFixed(2)}%` : "No data";
  elements.availabilityNote.textContent = classified
    ? `${(coverage * 100).toFixed(2)}% of selected window monitored`
    : "No monitored time in selected window";
  elements.windowDescription.textContent = `Showing ${windowLabel(selectedWindow)}. Striped time has no monitoring data.`;
  elements.start.textContent = formatDate(payload.start);
  elements.end.textContent = formatDate(payload.end);
  elements.timeline.setAttribute(
    "aria-label",
    `${windowLabel(selectedWindow)} service status timeline; ${(coverage * 100).toFixed(2)}% monitored`,
  );
}

function renderPing(payload) {
  const chart = elements.pingChart;
  chart.replaceChildren();
  elements.pingTooltip.hidden = true;
  const points = payload.points || [];
  const hasData = points.some(point => point.avg_ms !== null || point.failure_count > 0);
  chart.toggleAttribute("hidden", !hasData);
  elements.pingEmpty.hidden = hasData;
  if (!hasData) return;
  const compact = window.matchMedia("(max-width: 540px)").matches;
  const width = compact ? 420 : 900;
  const height = compact ? 190 : 160;
  const margin = { left: compact ? 66 : 54, right: 16, top: 16, bottom: 40 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const start = new Date(payload.start).getTime();
  const end = new Date(payload.end).getTime();
  const {min: yMin, max: yMax} = pingAxisBounds(points);
  chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  chart.dataset.compact = String(compact);
  const svg = (name, attributes, text) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const xAt = time => margin.left + (time - start) / (end - start) * plotWidth;
  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + plotHeight * index / 4;
    chart.append(
      svg("line", {x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "latency-grid-line"}),
      svg("text", {x: margin.left - 9, y: y + 4, class: "latency-y-label", "text-anchor": "end"}, `${Math.round(yMax - (yMax - yMin) * index / 4)} ms`),
    );
  }
  const ticks = compact ? 3 : 5;
  for (let index = 0; index < ticks; index += 1) {
    const time = start + (end - start) * index / (ticks - 1);
    const options = selectedWindow === "1h" ? {hour: "2-digit", minute: "2-digit"} : {day: "numeric", month: "short", hour: "2-digit"};
    chart.append(svg("text", {x: xAt(time), y: height - 8, class: "latency-x-label", "text-anchor": index === 0 ? "start" : index === ticks - 1 ? "end" : "middle"}, new Intl.DateTimeFormat(undefined, options).format(new Date(time))));
  }
  let group = [];
  let previous = null;
  const flush = () => {
    if (group.length) chart.append(svg("polyline", {points: group.join(" "), class: "latency-line", stroke: "var(--online)"}));
    group = [];
  };
  points.forEach(point => {
    const time = new Date(point.start).getTime();
    if (point.avg_ms === null || (previous !== null && time - previous > payload.bucket_seconds * 1750)) flush();
    const x = xAt(Math.min(end - 1, time + payload.bucket_seconds * 500));
    if (point.avg_ms !== null) {
      const y = margin.top + plotHeight * (1 - (point.avg_ms - yMin) / (yMax - yMin));
      group.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      chart.append(svg("circle", {cx: x, cy: y, r: 2, fill: "var(--online)"}));
    }
    if (point.failure_count > 0) chart.append(svg("circle", {cx: x, cy: margin.top + plotHeight, r: 3, fill: "var(--down)"}));
    previous = time;
  });
  flush();
  const show = event => {
    const rect = chart.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * width;
    const time = start + (x - margin.left) / plotWidth * (end - start);
    if (time < start || time >= end) { elements.pingTooltip.hidden = true; return; }
    const bucketStart = start + Math.floor((time - start) / (payload.bucket_seconds * 1000)) * payload.bucket_seconds * 1000;
    const point = points.find(item => new Date(item.start).getTime() === bucketStart);
    const lines = [formatDate(new Date(bucketStart).toISOString(), true)];
    if (point?.avg_ms != null) {
      lines.push(`Average ${Math.round(point.avg_ms)} ms`, `Min ${Math.round(point.min_ms)} · max ${Math.round(point.max_ms)} ms`);
    } else lines.push(point ? "No successful ping" : "No ping data");
    if (point?.failure_count) lines.push(`${point.failure_count} failed ${point.failure_count === 1 ? "check" : "checks"}`);
    elements.pingTooltip.replaceChildren(...lines.map(text => {
      const node = document.createElement("span"); node.textContent = text; return node;
    }));
    const wrap = chart.parentElement.getBoundingClientRect();
    elements.pingTooltip.style.setProperty("--latency-tooltip-left", `${event.clientX - wrap.left}px`);
    elements.pingTooltip.style.setProperty("--latency-tooltip-top", `${Math.max(110, event.clientY - wrap.top)}px`);
    elements.pingTooltip.hidden = false;
  };
  chart.onpointerdown = show;
  chart.onpointermove = show;
  chart.onpointerleave = () => { elements.pingTooltip.hidden = true; };
  chart.onpointercancel = chart.onpointerleave;
  chart.onpointerup = () => { setTimeout(chart.onpointerleave, 1500); };
}

function pingAxisBounds(points) {
  const values = points.map(point => point.avg_ms).filter(value => value !== null && Number.isFinite(value));
  if (!values.length) return {min: 0, max: 100};
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = Math.max(60, (high - low) * 1.6);
  const step = 10 ** Math.floor(Math.log10(span / 6));
  const center = (low + high) / 2;
  return {
    min: Math.max(0, Math.floor((center - span / 2) / step) * step),
    max: Math.ceil((center + span / 2) / step) * step,
  };
}

function fillTimelineGaps(segments, start, end) {
  const result = [];
  let cursor = start;
  [...segments]
    .sort((left, right) => new Date(left.start) - new Date(right.start))
    .forEach((segment) => {
      const segmentStart = Math.max(start, new Date(segment.start).getTime());
      const segmentEnd = Math.min(end, new Date(segment.end).getTime());
      if (segmentEnd <= segmentStart || segmentEnd <= cursor) return;
      if (segmentStart > cursor) {
        result.push({ status: "unknown", start: new Date(cursor).toISOString(), end: new Date(segmentStart).toISOString() });
      }
      const visibleStart = Math.max(cursor, segmentStart);
      result.push({ status: segment.status, start: new Date(visibleStart).toISOString(), end: new Date(segmentEnd).toISOString() });
      cursor = segmentEnd;
    });
  if (cursor < end) {
    result.push({ status: "unknown", start: new Date(cursor).toISOString(), end: new Date(end).toISOString() });
  }
  return result;
}

function windowLabel(value) {
  return ({ "1h": "1 hour", "24h": "24 hours", "7d": "7 days" })[value] || value;
}

function renderIncidents(items) {
  if (!items.length) {
    elements.incidents.innerHTML = '<p class="empty-state">No confirmed service incidents.</p>';
    return;
  }
  elements.incidents.replaceChildren(...items.map(incidentCard));
}

function incidentCard(item) {
  const card = document.createElement("article");
  card.className = "service-incident";
  card.dataset.status = item.status;

  const header = document.createElement("div");
  header.className = "service-incident-heading";
  const identity = document.createElement("div");
  identity.className = "service-incident-identity";
  const icon = document.createElement("i");
  icon.setAttribute("aria-hidden", "true");
  const title = document.createElement("strong");
  title.textContent = item.status === "down" ? "VPN down" : "VPN degraded";
  const duration = document.createElement("span");
  duration.className = "service-incident-duration";
  duration.textContent = formatDuration(item.duration_seconds === null
    ? Math.max(0, (Date.now() - new Date(item.start).getTime()) / 1000)
    : item.duration_seconds);
  duration.setAttribute("aria-label", `${item.lifecycle === "open" ? "Elapsed" : "Duration"}: ${duration.textContent}`);
  identity.append(icon, title, duration);
  const lifecycle = document.createElement("span");
  lifecycle.className = "service-incident-lifecycle";
  lifecycle.dataset.lifecycle = item.lifecycle;
  lifecycle.textContent = item.lifecycle === "open" ? "Ongoing" : "Recovered";
  header.append(identity, lifecycle);

  const evidence = document.createElement("div");
  evidence.className = "service-incident-evidence";
  const evidenceTitle = document.createElement("strong");
  evidenceTitle.textContent = detectionTitle(item.error_class);
  const evidenceDescription = document.createElement("p");
  evidenceDescription.textContent = detectionDescription(item.error_class);
  evidence.append(evidenceTitle, evidenceDescription);

  const facts = document.createElement("dl");
  facts.className = "service-incident-facts";
  facts.append(
    incidentFact(
      "Incident started",
      confirmationLabel(item.start, item.confirmed_start),
    ),
    incidentFact(
      "Connection restored",
      item.end ? confirmationLabel(item.end, item.confirmed_end) : "Not yet restored",
    ),
  );
  card.append(header, facts, evidence);
  return card;
}

function incidentFact(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.append(value);
  wrapper.append(term, description);
  return wrapper;
}

function detectionTitle(errorClass) {
  return ({
    client_session_absent: "OpenVPN session disappeared",
    status_permission_denied: "OpenVPN status file was not readable",
    status_file_missing: "OpenVPN status file was missing",
    status_unavailable: "OpenVPN status data was unavailable",
    timeout: "ICMP check timed out",
    icmp_timeout: "ICMP check timed out",
    unreachable: "VPN address was unreachable",
  })[errorClass] || "Service check failed";
}

function detectionDescription(errorClass) {
  if (errorClass === "client_session_absent") {
    return `The OpenVPN server status did not list ${serviceSettings?.client_name || "the configured client"}.`;
  }
  if (["status_permission_denied", "status_file_missing", "status_unavailable"].includes(errorClass)) {
    return `The session-list check failed for ${serviceSettings?.status_file || "the configured OpenVPN status file"}.`;
  }
  if (["timeout", "icmp_timeout", "unreachable"].includes(errorClass)) {
    return `ICMP reachability checks to ${serviceSettings?.endpoint || "the configured VPN address"} failed continuously.`;
  }
  return errorClass ? `Detector result: ${errorClass.replaceAll("_", " ")}.` : "The configured service checks reported a continuous failure.";
}

function confirmationLabel(observed, confirmed) {
  const value = document.createDocumentFragment();
  const note = document.createElement("span");
  note.className = "service-incident-confirmation";
  if (confirmed) {
    const delay = Math.max(0, (new Date(confirmed) - new Date(observed)) / 1000);
    note.textContent = `(Confirmed after: ${Math.round(delay)}s)`;
  } else {
    note.textContent = "(Not yet confirmed)";
  }
  value.append(`${formatDate(observed, true)} `, note);
  return value;
}

function renderSettings(item) {
  elements.interval.value = item.interval_seconds;
  elements.timeout.value = item.timeout_seconds;
  elements.failureSeconds.value = thresholdSeconds(
    item.failure_threshold,
    item.interval_seconds,
  );
  elements.recoverySeconds.value = thresholdSeconds(
    item.recovery_threshold,
    item.interval_seconds,
  );
  elements.useIcmp.checked = item.endpoint !== null;
  elements.endpoint.value = item.endpoint || "";
  elements.useStatus.checked = item.status_file !== null && item.client_name !== null;
  elements.statusFile.value = item.status_file || "";
  elements.clientName.value = item.client_name || "";
  elements.dashboardMode.value = item.dashboard;
  elements.defaultWindow.value = item.default_window || "7d";
  updateMethodState();
  updateThresholdNotes();
  elements.saveSettings.disabled = false;
}

function thresholdSeconds(threshold, interval) {
  return Math.max(0, (threshold - 1) * interval);
}

function thresholdForSeconds(seconds, interval) {
  return Math.max(1, Math.ceil(seconds / interval) + 1);
}

function updateThresholdNotes() {
  const interval = Number(elements.interval.value);
  if (!Number.isFinite(interval) || interval <= 0) return;
  const failureThreshold = thresholdForSeconds(
    Number(elements.failureSeconds.value),
    interval,
  );
  const recoveryThreshold = thresholdForSeconds(
    Number(elements.recoverySeconds.value),
    interval,
  );
  elements.failureNote.textContent = `${failureThreshold} consecutive failed ${failureThreshold === 1 ? "check" : "checks"}; effective minimum ${formatDuration(thresholdSeconds(failureThreshold, interval))}.`;
  elements.recoveryNote.textContent = `${recoveryThreshold} consecutive healthy ${recoveryThreshold === 1 ? "check" : "checks"}; effective minimum ${formatDuration(thresholdSeconds(recoveryThreshold, interval))}.`;
}

function updateMethodState() {
  elements.endpoint.disabled = !elements.useIcmp.checked;
  elements.endpoint.required = elements.useIcmp.checked;
  elements.statusFile.disabled = !elements.useStatus.checked;
  elements.clientName.disabled = !elements.useStatus.checked;
  elements.statusFile.required = elements.useStatus.checked;
  elements.clientName.required = elements.useStatus.checked;
  document.querySelector("#service-icmp-method").classList.toggle("disabled", !elements.useIcmp.checked);
  document.querySelector("#service-status-method").classList.toggle("disabled", !elements.useStatus.checked);
}

function statusLabel(status) {
  return ({ up: "Connected", degraded: "Degraded", down: "Disconnected", unknown: "Unknown" })[status] || "Unknown";
}

function relativeTime(value) {
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  if (hours < 24) return remaining ? `${hours}h ${remaining}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function formatDate(value, seconds = false) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: seconds ? "medium" : "short",
  }).format(new Date(value));
}

function selectWindow(value) {
  selectedWindow = value;
  document.querySelectorAll(".service-window").forEach(item => {
    const active = item.dataset.window === value;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
}

document.querySelectorAll(".service-window").forEach((button) => {
  button.addEventListener("click", () => {
    windowChosen = true;
    selectWindow(button.dataset.window);
    load().catch(showError);
  });
});

window.matchMedia("(max-width: 540px)").addEventListener("change", () => {
  if (pingPayload) renderPing(pingPayload);
});

[elements.interval, elements.failureSeconds, elements.recoverySeconds].forEach(
  (input) => input.addEventListener("input", updateThresholdNotes),
);
elements.useIcmp.addEventListener("change", updateMethodState);
elements.useStatus.addEventListener("change", updateMethodState);

elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.settingsForm.reportValidity()) return;
  if (!elements.useIcmp.checked && !elements.useStatus.checked) {
    elements.settingsMessage.dataset.status = "error";
    elements.settingsMessage.textContent = "Enable at least one detection method.";
    return;
  }
  const interval = Number(elements.interval.value);
  const updated = {
    ...serviceSettings,
    endpoint: elements.useIcmp.checked ? elements.endpoint.value.trim() : null,
    status_file: elements.useStatus.checked ? elements.statusFile.value.trim() : null,
    client_name: elements.useStatus.checked ? elements.clientName.value.trim() : null,
    interval_seconds: interval,
    timeout_seconds: Number(elements.timeout.value),
    failure_threshold: thresholdForSeconds(
      Number(elements.failureSeconds.value),
      interval,
    ),
    recovery_threshold: thresholdForSeconds(
      Number(elements.recoverySeconds.value),
      interval,
    ),
    dashboard: elements.dashboardMode.value,
    default_window: elements.defaultWindow.value,
  };
  const payload = {
    ...allSettings,
    service_monitors: allSettings.service_monitors.map((item) => (
      item.id === monitorId ? updated : item
    )),
  };
  elements.saveSettings.disabled = true;
  elements.settingsMessage.dataset.status = "pending";
  elements.settingsMessage.textContent = "Validating and saving...";
  try {
    const result = await api("/api/v1/settings", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Action": "settings-update",
      },
      body: JSON.stringify(payload),
    });
    allSettings = result.settings;
    serviceSettings = allSettings.service_monitors.find(
      (item) => item.id === monitorId,
    );
    renderSettings(serviceSettings);
    selectWindow(serviceSettings.default_window || "7d");
    elements.settingsMessage.dataset.status = "success";
    elements.settingsMessage.textContent = "Saved. The monitor will restart after its current check.";
    load().catch(showError);
  } catch (error) {
    elements.settingsMessage.dataset.status = "error";
    elements.settingsMessage.textContent = error.message;
  } finally {
    elements.saveSettings.disabled = false;
  }
});

function showError(error) {
  elements.error.textContent = `Service monitor failed: ${error.message}`;
  elements.error.hidden = false;
}

elements.current.addEventListener("click", async () => {
  if (checking || !serviceSettings?.enabled) return;
  checking = true;
  elements.current.disabled = true;
  elements.current.setAttribute("aria-busy", "true");
  elements.checked.textContent = "Checking...";
  try {
    manualResult = await api(`/api/v1/services/${encodeURIComponent(monitorId)}/test`, {
      method: "POST",
      headers: { "X-Monitor-Action": "manual-test" },
    });
    elements.error.hidden = true;
  } catch (error) {
    showError(error);
  } finally {
    checking = false;
    elements.current.setAttribute("aria-busy", "false");
    if (lastSummary) renderSummary(lastSummary);
  }
});

setInterval(() => {
  if (!document.hidden && lastSummary) renderSummary(lastSummary);
}, 1000);

setInterval(() => {
  if (!document.hidden && !checking && activeLoads === 0) load().catch(showError);
}, 5000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !checking && activeLoads === 0) load().catch(showError);
});

load().catch(showError);
