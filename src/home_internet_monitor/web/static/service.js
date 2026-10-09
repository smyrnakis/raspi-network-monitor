const params = new URLSearchParams(window.location.search);
const monitorId = params.get("id");
let selectedWindow = "7d";
let allSettings = null;
let serviceSettings = null;

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
};

async function api(path, options = {}) {
  const response = await fetch(path, {
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
  const hours = { "24h": 24, "7d": 168, "30d": 720 }[selectedWindow];
  const end = new Date();
  return { start: new Date(end.getTime() - hours * 3600 * 1000), end };
}

async function load() {
  if (!monitorId) throw new Error("No service monitor was selected");
  const selected = range();
  const query = new URLSearchParams({ start: selected.start.toISOString(), end: selected.end.toISOString() });
  const [summary, timeline, incidents, settings] = await Promise.all([
    api(`/api/v1/services/${encodeURIComponent(monitorId)}`),
    api(`/api/v1/services/${encodeURIComponent(monitorId)}/timeline?${query}`),
    api(`/api/v1/services/${encodeURIComponent(monitorId)}/incidents?limit=20`),
    api("/api/v1/settings"),
  ]);
  allSettings = settings;
  serviceSettings = settings.service_monitors.find((item) => item.id === monitorId);
  if (!serviceSettings) throw new Error("Service settings are unavailable");
  renderSummary(summary);
  renderTimeline(timeline);
  renderIncidents(incidents.items);
  renderSettings(serviceSettings);
  elements.error.hidden = true;
}

function renderSummary(item) {
  document.title = `${item.label} · Network monitor`;
  elements.name.textContent = item.label;
  elements.current.dataset.status = item.status;
  elements.status.textContent = statusLabel(item.status);
  elements.checked.textContent = item.last_checked ? `Checked ${relativeTime(item.last_checked)}` : "Not checked yet";
  elements.duration.textContent = item.stable_since ? formatDuration((Date.now() - new Date(item.stable_since).getTime()) / 1000) : "No data";
  elements.latency.textContent = item.last_latency_ms === null ? "No data" : `${Math.round(item.last_latency_ms)} ms`;
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
  return ({ "24h": "24 hours", "7d": "7 days", "30d": "30 days" })[value] || value;
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
  description.textContent = value;
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
  if (!confirmed) return `${formatDate(observed, true)} (Not yet confirmed)`;
  const delay = Math.max(0, (new Date(confirmed) - new Date(observed)) / 1000);
  return `${formatDate(observed, true)} (Confirmed after: ${Math.round(delay)}s)`;
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

document.querySelectorAll(".service-window").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".service-window").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    });
    selectedWindow = button.dataset.window;
    load().catch(showError);
  });
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
    elements.settingsMessage.dataset.status = "success";
    elements.settingsMessage.textContent = "Saved. The monitor will restart after its current check.";
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

load().catch(showError);
