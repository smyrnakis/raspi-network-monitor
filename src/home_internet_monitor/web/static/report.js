"use strict";

const IMPAIRED_STATUSES = new Set([
  "internet_down",
  "gateway_unreachable",
  "dns_failure",
  "partial_connectivity",
]);
const LATENCY_COLORS = ["#1479c9", "#7c4dcc", "#d36b13", "#008b76"];
const SVG_NS = "http://www.w3.org/2000/svg";
const LABELS = {
  internet_down: "Internet down",
  gateway_unreachable: "Gateway unreachable",
  dns_failure: "DNS failure",
  partial_connectivity: "Partial connectivity",
  host_reboot: "Host reboot",
  process_restart: "Process restart",
  stale_heartbeat: "Stale heartbeat",
  clock_uncertain: "Clock uncertain",
};

const query = new URLSearchParams(window.location.search);
const state = { timezone: undefined, start: null, end: null };
const elements = {
  filters: document.querySelector("#report-filter-summary"),
  period: document.querySelector("#report-period"),
  error: document.querySelector("#report-error"),
  back: document.querySelector("#report-back"),
  print: document.querySelector("#report-print"),
  availability: document.querySelector("#report-availability"),
  coverage: document.querySelector("#report-coverage"),
  mtbf: document.querySelector("#report-mtbf"),
  incidentCount: document.querySelector("#report-incident-count"),
  timeline: document.querySelector("#report-timeline"),
  start: document.querySelector("#report-start"),
  end: document.querySelector("#report-end"),
  latency: document.querySelector("#report-latency"),
  latencyLegend: document.querySelector("#report-latency-legend"),
  eventCount: document.querySelector("#report-event-count"),
  eventRows: document.querySelector("#report-event-rows"),
  eventsEmpty: document.querySelector("#report-events-empty"),
  footerHost: document.querySelector("#report-footer-host"),
  generated: document.querySelector("#report-generated"),
};

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      message = (await response.json()).detail || message;
    } catch (_error) {
      // Keep the bounded generic message for non-JSON errors.
    }
    throw new Error(message);
  }
  return response.json();
}

function reportRange() {
  const now = new Date();
  const requestedStart = parsedDate(query.get("start"));
  const requestedEnd = parsedDate(query.get("end"));
  const end = requestedEnd || now;
  const start = requestedStart || new Date(end.getTime() - 7 * 24 * 3600 * 1000);
  if (end <= start) throw new Error("Until must be after From.");
  return { start, end };
}

function parsedDate(value) {
  if (!value) return null;
  const result = new Date(value);
  return Number.isFinite(result.getTime()) ? result : null;
}

function rangeParameters(start, end) {
  return new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
}

function historyParameters(start, end) {
  const parameters = rangeParameters(start, end);
  for (const name of ["event_type", "category"]) {
    if (query.get(name)) parameters.set(name, query.get(name));
  }
  return parameters;
}

async function allHistory(parameters) {
  const items = [];
  let offset = 0;
  let total = 0;
  do {
    const page = new URLSearchParams(parameters);
    page.set("limit", "100");
    page.set("offset", String(offset));
    const payload = await api(`/api/v1/history?${page}`);
    items.push(...payload.items);
    total = payload.total;
    if (!payload.items.length) break;
    offset += payload.items.length;
  } while (offset < total);
  return items;
}

async function loadReport() {
  try {
    const range = reportRange();
    state.start = range.start;
    state.end = range.end;
    const status = await api("/api/v1/status");
    state.timezone = status.site.timezone;
    const rangeQuery = rangeParameters(range.start, range.end);
    const windowSeconds = (range.end - range.start) / 1000;
    const bucketSeconds = Math.min(
      86400,
      Math.max(60, Math.ceil(windowSeconds / 240 / 60) * 60),
    );
    const [availability, timeline, latency, events] = await Promise.all([
      api(`/api/v1/availability?${rangeQuery}`),
      api(`/api/v1/timeline?${rangeQuery}`),
      api(`/api/v1/latency?${rangeQuery}&bucket_seconds=${bucketSeconds}`),
      allHistory(historyParameters(range.start, range.end)),
    ]);
    renderHeader(status, events.length);
    renderMetrics(availability);
    renderTimeline(timeline);
    renderLatency(latency);
    renderEvents(events);
    elements.error.hidden = true;
  } catch (error) {
    elements.error.textContent = `Could not build report: ${error.message}`;
    elements.error.hidden = false;
  }
}

function renderHeader(status) {
  const eventType = query.get("event_type");
  const category = query.get("category");
  const chips = [
    `Event classification: ${eventType === "incident" ? "Incidents" : eventType === "gap" ? "Monitoring gaps" : "All events"}`,
    `Event status/reason: ${category ? label(category) : "Any"}`,
    `MTBF minimum: ${status.dashboard.mtbf_minimum_incident_minutes}m`,
  ];
  elements.filters.replaceChildren(...chips.map((text) => {
    const chip = document.createElement("span");
    chip.textContent = text;
    return chip;
  }));
  elements.period.textContent = `${formatDate(state.start)} to ${formatDate(state.end)}`;
  elements.footerHost.textContent = status.hostname;
  elements.generated.textContent = `Generated ${formatDate(new Date(), true)}`;
  const returnQuery = new URLSearchParams(query);
  elements.back.href = `/history${returnQuery.size ? `?${returnQuery}` : ""}`;
  document.title = `${status.hostname} · Network monitoring report`;
}

function renderMetrics(payload) {
  elements.availability.textContent = payload.availability === null
    ? "No data" : formatPercent(payload.availability);
  elements.coverage.textContent = formatPercent(payload.coverage);
  elements.mtbf.textContent = payload.classified_seconds <= 0
    ? "No data"
    : payload.incident_count === 0 ? "No failures" : formatDuration(payload.mtbf_seconds);
  elements.incidentCount.textContent = String(payload.incident_count);
}

function renderTimeline(payload) {
  const start = new Date(payload.start).getTime();
  const end = new Date(payload.end).getTime();
  const hours = (end - start) / 3600000;
  const count = hours <= 24 ? 24 : hours <= 24 * 7 ? 28 : 30;
  const bucketMs = (end - start) / count;
  const buckets = Array.from({ length: count }, (_unused, index) => {
    const bucketStart = start + index * bucketMs;
    const bucketEnd = index === count - 1 ? end : start + (index + 1) * bucketMs;
    let observedMs = 0;
    let impairedMs = 0;
    let explicitUnknownMs = 0;
    payload.segments.forEach((segment) => {
      const overlap = Math.max(0, Math.min(bucketEnd, Date.parse(segment.end)) -
        Math.max(bucketStart, Date.parse(segment.start)));
      if (!overlap) return;
      observedMs += overlap;
      if (IMPAIRED_STATUSES.has(segment.status)) impairedMs += overlap;
      if (segment.status === "monitoring_unknown") explicitUnknownMs += overlap;
    });
    const duration = bucketEnd - bucketStart;
    return {
      impairedShare: impairedMs / duration,
      unknownShare: (explicitUnknownMs + Math.max(0, duration - observedMs)) / duration,
    };
  });
  elements.timeline.replaceChildren(...buckets.map((bucket) => {
    const node = document.createElement("i");
    node.style.setProperty("--report-impairment", impairmentColor(bucket.impairedShare));
    node.style.setProperty("--report-unknown", Math.min(0.9, bucket.unknownShare * 1.5));
    return node;
  }));
  elements.start.textContent = formatDate(payload.start);
  elements.end.textContent = formatDate(payload.end);
}

function impairmentColor(share) {
  if (!share) return "#118765";
  const intensity = Math.min(1, Math.max(0.22, Math.sqrt(share)));
  return `hsl(352 72% ${(78 - intensity * 44).toFixed(1)}%)`;
}

function renderLatency(payload) {
  elements.latency.replaceChildren();
  const points = payload.series.flatMap((series) => series.points)
    .filter((point) => point.avg_ms !== null);
  if (!points.length) {
    const note = svgText(260, 67, "No latency data in this period", "report-svg-note");
    elements.latency.append(note);
    elements.latencyLegend.replaceChildren();
    return;
  }
  const width = 520;
  const height = 126;
  const margin = { left: 36, right: 8, top: 8, bottom: 18 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const startMs = Date.parse(payload.start);
  const endMs = Date.parse(payload.end);
  const observedMax = Math.max(...points.map((point) => point.avg_ms));
  const yMax = Math.max(25, Math.ceil(observedMax / 25) * 25);
  for (let index = 0; index <= 2; index += 1) {
    const y = margin.top + plotHeight * index / 2;
    elements.latency.append(
      svgElement("line", {
        x1: margin.left, x2: width - margin.right, y1: y, y2: y,
        class: "report-latency-grid",
      }),
      svgText(31, y + 3, `${Math.round(yMax * (1 - index / 2))} ms`, "report-latency-label"),
    );
  }
  payload.series.forEach((series, seriesIndex) => {
    const coordinates = series.points
      .filter((point) => point.avg_ms !== null)
      .map((point) => {
        const x = margin.left + (Date.parse(point.start) - startMs) / (endMs - startMs) * plotWidth;
        const y = margin.top + plotHeight - Math.min(point.avg_ms, yMax) / yMax * plotHeight;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
    if (coordinates) {
      elements.latency.append(svgElement("polyline", {
        points: coordinates,
        class: "report-latency-line",
        stroke: LATENCY_COLORS[seriesIndex % LATENCY_COLORS.length],
      }));
    }
  });
  elements.latencyLegend.replaceChildren(...payload.series.map((series, index) => {
    const item = document.createElement("span");
    const key = document.createElement("i");
    key.style.background = LATENCY_COLORS[index % LATENCY_COLORS.length];
    item.append(key, document.createTextNode(series.label));
    return item;
  }));
}

function renderEvents(events) {
  const chronological = [...events].sort(
    (left, right) => Date.parse(left.start) - Date.parse(right.start),
  );
  elements.eventRows.replaceChildren(...chronological.map((event) => {
    const row = document.createElement("tr");
    const classification = document.createElement("span");
    classification.className = "report-event-tag";
    classification.dataset.type = event.event_type;
    classification.textContent = event.event_type === "incident" ? "Incident" : "Monitoring gap";
    row.append(
      cell(classification),
      cell(label(event.category)),
      cell(formatDate(event.start)),
      cell(event.end ? formatDate(event.end) : "Ongoing"),
      cell(event.end ? formatDuration(event.duration_seconds) : `${formatDuration((state.end - new Date(event.start)) / 1000)}+`),
    );
    return row;
  }));
  elements.eventCount.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  elements.eventsEmpty.hidden = events.length > 0;
}

function cell(value) {
  const node = document.createElement("td");
  node.append(value);
  return node;
}

function svgElement(name, attributes) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  return element;
}

function svgText(x, y, value, className) {
  const text = svgElement("text", { x, y, class: className });
  text.textContent = value;
  return text;
}

function label(value) {
  return LABELS[value] || value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function formatDate(value, includeSeconds = false) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: state.timezone,
    dateStyle: "medium",
    timeStyle: includeSeconds ? "medium" : "short",
  }).format(new Date(value));
}

function formatPercent(value) {
  return Number.isFinite(value) ? `${(value * 100).toFixed(2).replace(/\.00$/, "")}%` : "No data";
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "Not available";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 48) return `${hours}h ${remainingMinutes}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

elements.print.addEventListener("click", () => window.print());
loadReport();
