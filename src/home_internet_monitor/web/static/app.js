"use strict";

const STATUS_LABELS = {
  online: "Online",
  internet_down: "Internet down",
  gateway_unreachable: "Gateway unreachable",
  dns_failure: "DNS failure",
  partial_connectivity: "Partial connectivity",
  monitoring_unknown: "Monitoring unknown",
  reachable: "Reachable",
  unreachable: "Unreachable",
  unknown: "Unknown",
  not_applicable: "Disabled",
  running: "Testing",
};

const AUTO_REFRESH_SECONDS = 10;
const TOOLTIP_HIDE_DELAY_MS = 3200;

const STATUS_PRIORITY = {
  monitoring_unknown: 0,
  online: 1,
  partial_connectivity: 2,
  dns_failure: 3,
  internet_down: 4,
  gateway_unreachable: 5,
};

const state = {
  preset: "24h",
  latencyPreset: "1h",
  customStart: null,
  customEnd: null,
  timezone: undefined,
  refreshInFlight: false,
  manualTestRunning: false,
  lastUpdatedAt: null,
  nextRefreshAt: null,
  timelineTooltipTimer: null,
  hiddenLatencyTargets: new Set(),
};

const elements = {
  error: document.querySelector("#error-banner"),
  siteName: document.querySelector("#site-name"),
  health: document.querySelector("#monitor-health"),
  healthLabel: document.querySelector("#health-label"),
  statusPanel: document.querySelector("#status-panel"),
  statusOrb: document.querySelector("#status-orb"),
  statusHeading: document.querySelector("#current-status-heading"),
  lastCheck: document.querySelector("#last-check"),
  componentGrid: document.querySelector("#component-grid"),
  runTest: document.querySelector("#run-test"),
  availability: document.querySelector("#availability-value"),
  coverage: document.querySelector("#coverage-value"),
  mtbf: document.querySelector("#mtbf-value"),
  mtbfDetail: document.querySelector("#mtbf-detail"),
  timeline: document.querySelector("#timeline-track"),
  timelineIncidents: document.querySelector("#timeline-incidents"),
  timelineTooltip: document.querySelector("#timeline-tooltip"),
  timelineStart: document.querySelector("#timeline-start"),
  timelineEnd: document.querySelector("#timeline-end"),
  latencyChart: document.querySelector("#latency-chart"),
  latencyEmpty: document.querySelector("#latency-empty"),
  latencyTooltip: document.querySelector("#latency-tooltip"),
  latencyControls: document.querySelector("#latency-series-controls"),
  incidentList: document.querySelector("#incident-list"),
  gapList: document.querySelector("#gap-list"),
  customForm: document.querySelector("#custom-window"),
  customStart: document.querySelector("#custom-start"),
  customEnd: document.querySelector("#custom-end"),
  customError: document.querySelector("#custom-error"),
  refreshNote: document.querySelector("#refresh-note"),
  appVersion: document.querySelector("#app-version"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch (_error) {
      // Keep the bounded generic message when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

function selectedRange() {
  if (state.preset === "custom") {
    return { start: state.customStart, end: state.customEnd };
  }
  const end = new Date();
  const durations = { "24h": 24, "7d": 24 * 7, "30d": 24 * 30 };
  const start = new Date(end.getTime() - durations[state.preset] * 3600 * 1000);
  return { start, end };
}

function rangeQuery(range) {
  const params = new URLSearchParams({
    start: range.start.toISOString(),
    end: range.end.toISOString(),
  });
  return params.toString();
}

async function loadOverview() {
  const [status, health] = await Promise.all([
    api("/api/v1/status"),
    api("/api/v1/health"),
  ]);
  const minimumDuration = status.dashboard?.hide_short_incidents ? 60 : 0;
  const [incidents, gaps] = await Promise.all([
    api(`/api/v1/incidents?limit=5&minimum_duration_seconds=${minimumDuration}`),
    api(`/api/v1/gaps?limit=5&minimum_duration_seconds=${minimumDuration}`),
  ]);
  state.timezone = status.site.timezone;
  renderStatus(status);
  renderHealth(health);
  renderIncidents(incidents.items);
  renderGaps(gaps.items);
}

async function loadTimeline() {
  const range = selectedRange();
  const query = rangeQuery(range);
  const [availability, timeline] = await Promise.all([
    api(`/api/v1/availability?${query}`),
    api(`/api/v1/timeline?${query}`),
  ]);
  renderAvailability(availability);
  renderTimeline(timeline);
}

async function loadLatency() {
  const end = new Date();
  const hours = state.latencyPreset === "7d" ? 24 * 7
    : state.latencyPreset === "1h" ? 1 : 24;
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  const bucketSeconds = state.latencyPreset === "7d" ? 1800
    : state.latencyPreset === "1h" ? 60 : 300;
  const query = `${rangeQuery({ start, end })}&bucket_seconds=${bucketSeconds}`;
  renderLatency(await api(`/api/v1/latency?${query}`));
}

async function refreshAll() {
  if (state.refreshInFlight || state.manualTestRunning) return;
  state.refreshInFlight = true;
  try {
    await loadOverview();
    await Promise.all([loadTimeline(), loadLatency()]);
    elements.error.hidden = true;
    state.lastUpdatedAt = new Date();
  } catch (error) {
    elements.error.textContent = `Dashboard update failed: ${error.message}`;
    elements.error.hidden = false;
  } finally {
    state.refreshInFlight = false;
    state.nextRefreshAt = Date.now() + AUTO_REFRESH_SECONDS * 1000;
    updateRefreshNote();
  }
}

function renderStatus(payload) {
  const status = payload.stable_status;
  document.title = `${payload.hostname} · Network monitor`;
  elements.siteName.textContent = payload.hostname;
  elements.appVersion.textContent = `Version ${payload.version}`;
  elements.statusOrb.dataset.status = status;
  elements.statusHeading.textContent = label(status);
  let lastCheck = payload.last_observed_at
    ? `Last completed check ${formatDate(payload.last_observed_at, true)}`
    : "No completed check yet";

  const components = payload.latest_round ? payload.latest_round.components : {};
  renderComponents(components);

  if (payload.pending_status) {
    lastCheck += ` · Checking ${label(payload.pending_status).toLowerCase()} ` +
      `(${payload.pending_count} confirming round${payload.pending_count === 1 ? "" : "s"})`;
  }
  elements.lastCheck.textContent = lastCheck;
}

function renderHealth(payload) {
  elements.health.dataset.health = payload.status;
  elements.healthLabel.textContent = payload.status === "healthy"
    ? "Monitor healthy"
    : "Monitor needs attention";
}

function renderComponents(components) {
  elements.componentGrid.querySelectorAll("[data-component]").forEach((item) => {
    const componentStatus = components[item.dataset.component] || "unknown";
    item.querySelector("strong").textContent = label(componentStatus);
    item.dataset.status = componentStatus;
  });
}

function renderAvailability(payload) {
  elements.availability.textContent = payload.availability === null
    ? "No data"
    : formatPercent(payload.availability);
  elements.coverage.textContent =
    `${formatPercent(payload.coverage)} (${formatKnownTime(payload.classified_seconds)})`;
  if (payload.classified_seconds <= 0) {
    elements.mtbf.textContent = "No data";
    elements.mtbfDetail.textContent = "in selected window";
  } else if (payload.incident_count === 0) {
    elements.mtbf.textContent = "No failures";
    elements.mtbfDetail.textContent = "in selected window";
  } else {
    elements.mtbf.textContent = formatMetricDuration(payload.mtbf_seconds);
    elements.mtbfDetail.textContent =
      `${payload.incident_count} confirmed incident${payload.incident_count === 1 ? "" : "s"}`;
  }
  const band = payload.availability === null || payload.coverage < 0.5
    ? "unknown"
    : payload.availability >= 0.995
      ? "good"
      : payload.availability >= 0.98 ? "warning" : "poor";
  elements.availability.dataset.band = band;
  elements.availability.title = band === "unknown"
    ? "Color withheld until at least half of the selected window is classified"
    : "Green: 99.5% or higher. Yellow: 98% to 99.5%. Red: below 98%.";
}

const LATENCY_COLORS = ["#1479c9", "#7c4dcc", "#d36b13", "#008b76"];
const SVG_NS = "http://www.w3.org/2000/svg";

function renderLatency(payload) {
  const visibleSeries = payload.series.filter(
    (series) => !state.hiddenLatencyTargets.has(series.target_id),
  );
  renderLatencyControls(payload);
  elements.latencyChart.replaceChildren();
  elements.latencyTooltip.hidden = true;

  const allPoints = visibleSeries.flatMap((series) => series.points)
    .filter((point) => point.avg_ms !== null);
  const hasObservations = visibleSeries.some((series) =>
    series.points.some((point) => point.avg_ms !== null || point.failure_count > 0)
  );
  elements.latencyEmpty.hidden = hasObservations;
  elements.latencyChart.hidden = !hasObservations;
  if (!hasObservations) {
    return;
  }

  const compact = window.matchMedia("(max-width: 540px)").matches;
  const width = compact ? 420 : 900;
  const height = compact ? 280 : 260;
  const margin = compact
    ? { left: 68, right: 14, top: 16, bottom: 50 }
    : { left: 54, right: 16, top: 16, bottom: 42 };
  elements.latencyChart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  elements.latencyChart.dataset.compact = String(compact);
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const startMs = new Date(payload.start).getTime();
  const endMs = new Date(payload.end).getTime();
  // Scale to successful averages so timeouts cannot flatten healthy lines.
  const observedMax = allPoints.length
    ? Math.max(...allPoints.map((point) => point.avg_ms))
    : 100;
  const yMax = niceLatencyMaximum(observedMax);

  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + (plotHeight * index) / 4;
    elements.latencyChart.append(
      svgElement("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "latency-grid-line" }),
      svgText(margin.left - 9, y + 4, formatMilliseconds(yMax * (1 - index / 4)), "latency-y-label"),
    );
  }

  renderLatencyTimeAxis(
    startMs,
    endMs,
    width,
    height,
    margin,
    plotWidth,
    plotHeight,
    compact ? 3 : 5,
  );

  visibleSeries.forEach((series) => {
    const color = LATENCY_COLORS[payload.series.findIndex((item) => item.target_id === series.target_id) % LATENCY_COLORS.length];
    const groups = contiguousLatencyGroups(series.points, payload.bucket_seconds);
    groups.forEach((points) => {
      const coordinates = points.map((point) => {
        const x = margin.left + ((new Date(point.start).getTime() - startMs) / (endMs - startMs)) * plotWidth;
        const y = margin.top + plotHeight - Math.min(point.avg_ms, yMax) / yMax * plotHeight;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      elements.latencyChart.append(svgElement("polyline", {
        points: coordinates,
        class: "latency-line",
        stroke: color,
      }));
    });
  });

  (payload.incidents || []).forEach((incident) => {
    const incidentTime = Math.max(startMs, new Date(incident.start).getTime());
    const x = margin.left + ((incidentTime - startMs) / (endMs - startMs)) * plotWidth;
    const marker = svgElement("circle", {
      cx: x,
      cy: height - margin.bottom + 3,
      r: 4.2,
      class: "latency-incident",
      tabindex: 0,
      role: "link",
      "aria-label": `Open ${label(incident.status)} incident from ${formatDate(incident.start, true)}`,
    });
    const title = svgElement("title", {});
    title.textContent = `Open ${label(incident.status)} incident`;
    marker.append(title);
    const openIncident = () => {
      window.location.href =
        `/history?focus_type=incident&focus_id=${encodeURIComponent(incident.incident_id)}`;
    };
    marker.addEventListener("pointerdown", (event) => event.stopPropagation());
    marker.addEventListener("click", openIncident);
    marker.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openIncident();
      }
    });
    elements.latencyChart.append(marker);
  });

  const tooltipGeometry = { width, height, margin, plotWidth, startMs, endMs };
  elements.latencyChart.onpointerdown = (event) => {
    if (event.pointerType === "touch") {
      elements.latencyChart.setPointerCapture(event.pointerId);
      event.preventDefault();
    }
    showLatencyTooltip(event, payload, visibleSeries, tooltipGeometry);
  };
  elements.latencyChart.onpointermove = (event) => {
    if (event.pointerType === "touch") event.preventDefault();
    showLatencyTooltip(event, payload, visibleSeries, tooltipGeometry);
  };
  elements.latencyChart.onpointerup = (event) => {
    if (elements.latencyChart.hasPointerCapture(event.pointerId)) {
      elements.latencyChart.releasePointerCapture(event.pointerId);
    }
    window.setTimeout(hideLatencyTooltip, 650);
  };
  elements.latencyChart.onpointercancel = hideLatencyTooltip;
  elements.latencyChart.onpointerleave = (event) => {
    if (event.pointerType !== "touch") hideLatencyTooltip();
  };
}

function renderLatencyTimeAxis(
  startMs,
  endMs,
  width,
  height,
  margin,
  plotWidth,
  plotHeight,
  tickCount,
) {
  for (let index = 0; index < tickCount; index += 1) {
    const share = index / (tickCount - 1);
    const x = margin.left + plotWidth * share;
    const time = new Date(startMs + (endMs - startMs) * share);
    elements.latencyChart.append(
      svgElement("line", {
        x1: x,
        x2: x,
        y1: margin.top,
        y2: margin.top + plotHeight,
        class: "latency-grid-line latency-time-grid",
      }),
      svgText(
        x,
        height - 9,
        formatLatencyTick(time),
        "latency-x-label",
        index === 0 ? "start" : index === tickCount - 1 ? "end" : "middle",
      ),
    );
  }
}

function formatLatencyTick(value) {
  const options = state.latencyPreset === "1h"
    ? { hour: "2-digit", minute: "2-digit" }
    : state.latencyPreset === "24h"
      ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
      : { day: "numeric", month: "short", hour: "2-digit" };
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: state.timezone }).format(value);
}

function showLatencyTooltip(event, payload, visibleSeries, geometry) {
  const chartRect = elements.latencyChart.getBoundingClientRect();
  const chartX = (event.clientX - chartRect.left) / chartRect.width * geometry.width;
  if (chartX < geometry.margin.left || chartX > geometry.width - geometry.margin.right) {
    hideLatencyTooltip();
    return;
  }
  const requestedTime = geometry.startMs +
    ((chartX - geometry.margin.left) / geometry.plotWidth) *
    (geometry.endMs - geometry.startMs);
  const candidates = visibleSeries.flatMap((series) => series.points.map((point) => ({
    series,
    point,
    time: new Date(point.start).getTime(),
  })));
  if (!candidates.length) {
    hideLatencyTooltip();
    return;
  }
  const nearest = candidates.reduce((best, item) =>
    Math.abs(item.time - requestedTime) < Math.abs(best.time - requestedTime) ? item : best
  );
  if (Math.abs(nearest.time - requestedTime) > payload.bucket_seconds * 1000) {
    hideLatencyTooltip();
    return;
  }
  const values = visibleSeries.map((series) => {
    const point = series.points.find((item) => item.start === nearest.point.start);
    if (!point) return null;
    const name = latencyTooltipLabel(series);
    if (point.avg_ms !== null) return `${name}: ${Math.round(point.avg_ms)}ms`;
    if (point.failure_count > 0) return `${name}: failed`;
    return null;
  }).filter(Boolean);
  if (!values.length) {
    hideLatencyTooltip();
    return;
  }
  const lines = [formatLatencyTooltipTime(new Date(nearest.time)), ...values];
  elements.latencyTooltip.replaceChildren(...lines.map((value) => {
    const line = document.createElement("span");
    line.textContent = value;
    return line;
  }));
  const wrapRect = elements.latencyChart.parentElement.getBoundingClientRect();
  elements.latencyTooltip.style.setProperty(
    "--latency-tooltip-left",
    `${event.clientX - wrapRect.left}px`,
  );
  elements.latencyTooltip.style.setProperty(
    "--latency-tooltip-top",
    `${event.clientY - wrapRect.top}px`,
  );
  elements.latencyTooltip.hidden = false;
}

function latencyTooltipLabel(series) {
  if (series.kind === "gateway") return "Gateway";
  if (series.kind === "external_ip") {
    const shortName = series.label.replace(/^External\s+/i, "");
    return `${shortName} (ext)`;
  }
  return series.label;
}

function formatLatencyTooltipTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: state.timezone,
    hour: "2-digit",
    minute: "2-digit",
  }).format(value);
}

function hideLatencyTooltip() {
  elements.latencyTooltip.hidden = true;
}

function renderLatencyControls(payload) {
  elements.latencyControls.replaceChildren(...payload.series.map((series, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "latency-series-button";
    button.dataset.hidden = String(state.hiddenLatencyTargets.has(series.target_id));
    button.innerHTML = `<i aria-hidden="true"></i><span></span>`;
    button.querySelector("i").style.background = LATENCY_COLORS[index % LATENCY_COLORS.length];
    button.querySelector("span").textContent = series.label;
    button.setAttribute("aria-pressed", String(!state.hiddenLatencyTargets.has(series.target_id)));
    button.addEventListener("click", () => {
      if (state.hiddenLatencyTargets.has(series.target_id)) {
        state.hiddenLatencyTargets.delete(series.target_id);
      } else {
        state.hiddenLatencyTargets.add(series.target_id);
      }
      renderLatency(payload);
    });
    return button;
  }));
}

function contiguousLatencyGroups(points, bucketSeconds) {
  const groups = [];
  let current = [];
  points.filter((point) => point.avg_ms !== null).forEach((point) => {
    const previous = current[current.length - 1];
    if (previous && new Date(point.start) - new Date(previous.start) > bucketSeconds * 1750) {
      groups.push(current);
      current = [];
    }
    current.push(point);
  });
  if (current.length) groups.push(current);
  return groups;
}

function niceLatencyMaximum(value) {
  const minimum = Math.max(10, value * 1.1);
  const magnitude = 10 ** Math.floor(Math.log10(minimum));
  const normalized = minimum / magnitude;
  const step = normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function formatMilliseconds(value) {
  return `${Math.round(value)} ms`;
}

function svgElement(name, attributes) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function svgText(x, y, content, className, anchor = "end") {
  const node = svgElement("text", { x, y, class: className, "text-anchor": anchor });
  node.textContent = content;
  return node;
}

function renderTimeline(payload) {
  elements.timeline.replaceChildren();
  hideTimelineTooltip();
  const start = new Date(payload.start).getTime();
  const end = new Date(payload.end).getTime();
  elements.timelineStart.textContent = formatDate(payload.start);
  elements.timelineEnd.textContent = formatDate(payload.end);
  const buckets = timelineBuckets(start, end, payload.segments);
  elements.timeline.style.setProperty("--segment-count", buckets.length);

  buckets.forEach((bucket, index) => {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "timeline-segment";
    node.dataset.status = bucket.status;
    const description = bucketDescription(bucket);
    node.setAttribute("aria-label", description);
    node.addEventListener("click", () => showTimelineTooltip(bucket, index, buckets.length));
    elements.timeline.appendChild(node);
  });
  renderTimelineIncidents(payload.incidents || [], start, end);
  elements.timeline.setAttribute(
    "aria-label",
    `Connection status timeline from ${formatDate(payload.start, true)} to ` +
      `${formatDate(payload.end, true)}`,
  );
}

function renderTimelineIncidents(incidents, start, end) {
  elements.timelineIncidents.replaceChildren();
  elements.timelineIncidents.setAttribute(
    "aria-label",
    `${incidents.length} confirmed incident${incidents.length === 1 ? "" : "s"} in selected window`,
  );
  incidents.forEach((incident) => {
    const incidentStart = new Date(incident.start).getTime();
    const incidentEnd = incident.end ? new Date(incident.end).getTime() : Date.now();
    const visibleStart = Math.max(start, incidentStart);
    const visibleEnd = Math.min(end, incidentEnd);
    if (visibleEnd <= visibleStart) return;

    const left = ((visibleStart - start) / (end - start)) * 100;
    const width = ((visibleEnd - visibleStart) / (end - start)) * 100;
    const center = ((visibleStart + visibleEnd) / 2 - start) / (end - start);
    const description = incidentDescription(incident, incidentStart, incidentEnd);
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = "timeline-incident";
    marker.dataset.status = incident.status;
    marker.style.left = `${left}%`;
    marker.style.width = `${width}%`;
    marker.setAttribute("aria-label", `Open ${description.replace("\n", ". ")}`);
    marker.addEventListener("pointerenter", () => showTimelineTooltipText(description, center));
    marker.addEventListener("focus", () => showTimelineTooltipText(description, center));
    marker.addEventListener("pointerleave", hideTimelineTooltip);
    marker.addEventListener("blur", hideTimelineTooltip);
    marker.addEventListener("click", () => {
      window.location.href =
        `/history?focus_type=incident&focus_id=${encodeURIComponent(incident.incident_id)}`;
    });
    elements.timelineIncidents.appendChild(marker);
  });
}

function incidentDescription(incident, start, end) {
  const categories = incident.categories || [incident.status];
  const title = categories.length > 1 ? "Connectivity incident" : label(incident.status);
  const duration = formatDuration(Math.max(0, end - start) / 1000);
  const endLabel = incident.end ? formatDate(incident.end) : "ongoing";
  return `${title} · ${duration}\n${formatDate(incident.start)} to ${endLabel}`;
}

function timelineBucketCount(start, end) {
  if (state.preset === "24h") return 24;
  if (state.preset === "7d") return 28;
  if (state.preset === "30d") return 30;

  const hours = (end - start) / 3600000;
  if (hours <= 24) return 24;
  if (hours <= 24 * 7) return 28;
  return 30;
}

function timelineBuckets(start, end, segments) {
  const count = timelineBucketCount(start, end);
  const bucketMs = (end - start) / count;
  return Array.from({ length: count }, (_unused, index) => {
    const bucketStart = start + index * bucketMs;
    const bucketEnd = index === count - 1 ? end : start + (index + 1) * bucketMs;
    const durations = {};
    let classifiedMs = 0;

    segments.forEach((segment) => {
      const overlapStart = Math.max(bucketStart, new Date(segment.start).getTime());
      const overlapEnd = Math.min(bucketEnd, new Date(segment.end).getTime());
      const overlapMs = Math.max(0, overlapEnd - overlapStart);
      if (!overlapMs) return;
      durations[segment.status] = (durations[segment.status] || 0) + overlapMs;
      classifiedMs += overlapMs;
    });

    durations.monitoring_unknown = Math.max(0, bucketEnd - bucketStart - classifiedMs);
    const status = Object.entries(durations).sort(([statusA, durationA], [statusB, durationB]) =>
      durationB - durationA || STATUS_PRIORITY[statusB] - STATUS_PRIORITY[statusA]
    )[0][0];
    return {
      start: new Date(bucketStart),
      end: new Date(bucketEnd),
      status,
      statusShare: durations[status] / (bucketEnd - bucketStart),
    };
  });
}

function bucketDescription(bucket) {
  return `${label(bucket.status)} (${formatPercent(bucket.statusShare)} of this segment): ` +
    `${formatDate(bucket.start.toISOString())} to ${formatDate(bucket.end.toISOString())}`;
}

function showTimelineTooltip(bucket, index, count) {
  showTimelineTooltipText(bucketDescription(bucket), (index + 0.5) / count);
}

function showTimelineTooltipText(description, position) {
  window.clearTimeout(state.timelineTooltipTimer);
  elements.timelineTooltip.textContent = description;
  elements.timelineTooltip.style.setProperty("--tooltip-left", `${position * 100}%`);
  elements.timelineTooltip.hidden = false;
  state.timelineTooltipTimer = window.setTimeout(hideTimelineTooltip, TOOLTIP_HIDE_DELAY_MS);
}

function hideTimelineTooltip() {
  window.clearTimeout(state.timelineTooltipTimer);
  state.timelineTooltipTimer = null;
  elements.timelineTooltip.hidden = true;
}

function renderIncidents(items) {
  elements.incidentList.replaceChildren();
  if (!items.length) {
    elements.incidentList.appendChild(emptyState("No confirmed incidents yet."));
    return;
  }
  items.forEach((item) => {
    const end = item.observed_end ? new Date(item.observed_end) : new Date();
    const seconds = (end - new Date(item.observed_start)) / 1000;
    const categories = item.categories || [item.status];
    const hasPhases = categories.length > 1;
    elements.incidentList.appendChild(eventRow(
      hasPhases ? "Connectivity incident" : label(item.status),
      `${formatDate(item.observed_start, true)}${item.observed_end ? "" : " · ongoing"}`,
      hasPhases
        ? categories.map(label).join(" → ")
        : (item.end_reason ? `Ended: ${label(item.end_reason)}` : "Confirmed incident"),
      formatDuration(seconds),
      item.status,
      `/history?focus_type=incident&focus_id=${encodeURIComponent(item.incident_id)}`,
    ));
  });
}

function renderGaps(items) {
  elements.gapList.replaceChildren();
  if (!items.length) {
    elements.gapList.appendChild(emptyState("No monitoring gaps recorded."));
    return;
  }
  items.forEach((item) => {
    const end = item.end ? new Date(item.end) : new Date();
    const seconds = (end - new Date(item.start)) / 1000;
    elements.gapList.appendChild(eventRow(
      "Monitoring gap",
      `${formatDate(item.start, true)}${item.end ? "" : " · ongoing"}`,
      label(item.reason),
      formatDuration(seconds),
      "monitoring_unknown",
      `/history?focus_type=gap&focus_id=${encodeURIComponent(item.gap_id)}`,
    ));
  });
}

function eventRow(title, time, meta, duration, status, href = null) {
  const row = document.createElement(href ? "a" : "article");
  row.className = "event-row";
  if (href) row.href = href;
  const content = document.createElement("div");
  const heading = document.createElement("div");
  heading.className = "event-title";
  const headingText = document.createElement("span");
  headingText.textContent = title;
  const pill = document.createElement("span");
  pill.className = "status-pill";
  pill.dataset.status = status;
  pill.textContent = label(status);
  heading.append(headingText, pill);
  content.append(heading, textNode("event-time", time), textNode("event-meta", meta));
  const durationNode = textNode("event-duration", duration);
  row.append(content, durationNode);
  return row;
}

function textNode(className, content) {
  const node = document.createElement("div");
  node.className = className;
  node.textContent = content;
  return node;
}

function emptyState(message) {
  const node = document.createElement("p");
  node.className = "empty-state";
  node.textContent = message;
  return node;
}

function label(value) {
  return STATUS_LABELS[value] || value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

function formatPercent(value) {
  return new Intl.NumberFormat(undefined, {
    style: "percent",
    minimumFractionDigits: value > 0.999 ? 2 : 1,
    maximumFractionDigits: 2,
  }).format(value);
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "Not available";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 48) return `${hours}h ${remainingMinutes}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function formatKnownTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "not available";
  const range = selectedRange();
  const rangeHours = (range.end - range.start) / 3600000;
  if (state.preset === "24h" || rangeHours <= 48) {
    return `${Math.round(seconds / 3600)}h`;
  }
  return `${Math.round(seconds / 86400)}d`;
}

function formatMetricDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "Not available";
  const totalMinutes = Math.round(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (totalHours < 48) return `${totalHours}h ${minutes}m`;
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return `${days}d ${hours}h`;
}

function formatDate(value, includeSeconds = false) {
  const options = {
    timeZone: state.timezone,
    dateStyle: "medium",
    timeStyle: includeSeconds ? "medium" : "short",
  };
  return new Intl.DateTimeFormat(undefined, options).format(new Date(value));
}

function localInputValue(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function updateRefreshNote() {
  if (state.manualTestRunning) {
    elements.refreshNote.textContent = "Manual test running";
    return;
  }
  if (!state.lastUpdatedAt) {
    elements.refreshNote.textContent = "Updating...";
    return;
  }
  const seconds = Math.max(
    0,
    Math.ceil((state.nextRefreshAt - Date.now()) / 1000),
  );
  elements.refreshNote.textContent =
    `Updated ${formatDate(state.lastUpdatedAt.toISOString(), true)} ` +
    `(${seconds}s until next)`;
}

function setManualTestRunning(running) {
  state.manualTestRunning = running;
  elements.statusPanel.dataset.testing = running ? "true" : "false";
  elements.runTest.disabled = running;
  elements.runTest.textContent = running ? "Testing..." : "Run test now";
  updateRefreshNote();
}

async function runManualTest() {
  if (state.manualTestRunning) return;
  setManualTestRunning(true);
  try {
    const result = await api("/api/v1/test", {
      method: "POST",
      headers: { "X-Monitor-Action": "manual-test" },
    });
    elements.statusOrb.dataset.status = result.status;
    elements.statusHeading.textContent = label(result.status);
    elements.lastCheck.textContent =
      `Manual test completed ${formatDate(result.observed_at, true)}`;
    renderComponents(result.components);
    elements.error.hidden = true;
    state.lastUpdatedAt = new Date();
  } catch (error) {
    showError(error);
  } finally {
    setManualTestRunning(false);
    state.nextRefreshAt = Date.now() + AUTO_REFRESH_SECONDS * 1000;
    updateRefreshNote();
  }
}

document.querySelectorAll(".window-button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".window-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    const preset = button.dataset.window;
    if (preset === "custom") {
      elements.customForm.hidden = false;
      const now = new Date();
      elements.customEnd.value = localInputValue(state.customEnd || now);
      elements.customStart.value = localInputValue(
        state.customStart || new Date(now.getTime() - 24 * 3600 * 1000),
      );
      elements.customStart.focus();
      return;
    }
    elements.customForm.hidden = true;
    state.preset = preset;
    loadTimeline().catch(showError);
  });
});

elements.customForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const start = new Date(elements.customStart.value);
  const end = new Date(elements.customEnd.value);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) {
    elements.customError.textContent = "Choose both a start and end time.";
    return;
  }
  if (end <= start) {
    elements.customError.textContent = "End must be after start.";
    return;
  }
  elements.customError.textContent = "";
  state.preset = "custom";
  state.customStart = start;
  state.customEnd = end;
  loadTimeline().catch(showError);
});

elements.runTest.addEventListener("click", runManualTest);

document.querySelectorAll(".latency-window").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".latency-window").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.latencyPreset = button.dataset.latencyWindow;
    loadLatency().catch(showError);
  });
});

function showError(error) {
  elements.error.textContent = `Dashboard update failed: ${error.message}`;
  elements.error.hidden = false;
}

refreshAll();
window.setInterval(() => {
  updateRefreshNote();
  if (state.nextRefreshAt && Date.now() >= state.nextRefreshAt) {
    refreshAll();
  }
}, 1000);
