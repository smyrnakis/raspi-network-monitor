"use strict";

const PAGE_SIZE = 20;
const CATEGORY_GROUPS = {
  incident: [
    ["internet_down", "Internet down"],
    ["gateway_unreachable", "Gateway unreachable"],
    ["dns_failure", "DNS failure"],
    ["partial_connectivity", "Partial connectivity"],
  ],
  gap: [
    ["host_reboot", "Host reboot"],
    ["process_restart", "Process restart"],
    ["stale_heartbeat", "Stale heartbeat"],
    ["clock_uncertain", "Clock uncertain"],
  ],
};

const urlParameters = new URLSearchParams(window.location.search);
const state = {
  offset: 0,
  total: 0,
  timezone: undefined,
  focusType: urlParameters.get("focus_type"),
  focusId: urlParameters.get("focus_id"),
};

const elements = {
  form: document.querySelector("#history-filters"),
  type: document.querySelector("#history-type"),
  category: document.querySelector("#history-category"),
  start: document.querySelector("#history-start"),
  end: document.querySelector("#history-end"),
  reset: document.querySelector("#history-reset"),
  export: document.querySelector("#history-export"),
  list: document.querySelector("#history-list"),
  count: document.querySelector("#history-count"),
  previous: document.querySelector("#history-previous"),
  next: document.querySelector("#history-next"),
  pageLabel: document.querySelector("#history-page-label"),
  error: document.querySelector("#history-error"),
  siteName: document.querySelector("#site-name"),
  appVersion: document.querySelector("#app-version"),
};

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      message = (await response.json()).detail || message;
    } catch (_error) {
      // Keep generic message for non-JSON errors.
    }
    throw new Error(message);
  }
  return response.json();
}

function updateCategories() {
  const selected = elements.category.value;
  const groups = elements.type.value
    ? [elements.type.value]
    : ["incident", "gap"];
  const options = [new Option("All statuses and reasons", "")];
  groups.forEach((group) => {
    CATEGORY_GROUPS[group].forEach(([value, text]) => options.push(new Option(text, value)));
  });
  elements.category.replaceChildren(...options);
  if (options.some((option) => option.value === selected)) {
    elements.category.value = selected;
  }
}

function filterParameters() {
  const parameters = new URLSearchParams();
  if (elements.type.value) parameters.set("event_type", elements.type.value);
  if (elements.category.value) parameters.set("category", elements.category.value);
  if (elements.start.value) parameters.set("start", new Date(elements.start.value).toISOString());
  if (elements.end.value) parameters.set("end", new Date(elements.end.value).toISOString());
  return parameters;
}

function updateExportLink() {
  const parameters = filterParameters();
  const query = parameters.toString();
  elements.export.href = `/api/v1/history.csv${query ? `?${query}` : ""}`;
}

async function loadHistory() {
  const parameters = filterParameters();
  parameters.set("limit", PAGE_SIZE);
  parameters.set("offset", state.offset);
  updateExportLink();
  try {
    const payload = await api(`/api/v1/history?${parameters}`);
    let items = payload.items;
    const focused = focusKey();
    if (focused && !items.some((item) => itemKey(item) === focused)) {
      const focusParameters = new URLSearchParams({
        event_type: state.focusType,
        event_id: state.focusId,
      });
      try {
        const focusedItem = await api(`/api/v1/history/item?${focusParameters}`);
        state.focusId = focusedItem.event_id;
        items = [
          focusedItem,
          ...items.filter((item) => itemKey(item) !== itemKey(focusedItem)),
        ];
      } catch (_error) {
        // Filters may intentionally exclude the linked event; keep the normal page.
      }
    }
    state.total = payload.total;
    renderEvents(items);
    renderPagination(payload);
    elements.error.hidden = true;
  } catch (error) {
    elements.error.textContent = `Could not load history: ${error.message}`;
    elements.error.hidden = false;
  }
}

function renderEvents(items) {
  elements.list.replaceChildren();
  if (!items.length) {
    elements.list.append(emptyState("No events match these filters."));
    return;
  }
  const focused = focusKey();
  items.forEach((item) => elements.list.append(historyEvent(item, itemKey(item) === focused)));
  if (focused) {
    const selected = document.getElementById(eventDomId(focused));
    if (selected) window.setTimeout(() => selected.scrollIntoView({ block: "center" }), 0);
  }
}

function historyEvent(item, focused) {
  const details = document.createElement("details");
  details.className = "history-event";
  details.id = eventDomId(itemKey(item));
  details.open = focused;
  if (focused) details.dataset.focused = "true";

  const summary = document.createElement("summary");
  const summaryMain = document.createElement("span");
  summaryMain.className = "history-event-summary";
  const title = document.createElement("strong");
  title.textContent = item.event_type === "incident" ? "Incident" : "Monitoring gap";
  const pill = document.createElement("span");
  pill.className = "status-pill";
  pill.dataset.status = item.event_type === "gap" ? "monitoring_unknown" : item.category;
  pill.textContent = label(item.category);
  const time = document.createElement("span");
  time.className = "history-event-time";
  time.textContent = formatDate(item.start);
  summaryMain.append(title, pill, time);
  const duration = document.createElement("span");
  duration.className = "history-event-duration";
  duration.textContent = item.end
    ? formatDuration(item.duration_seconds)
    : "Ongoing";
  summary.append(summaryMain, duration);

  const body = document.createElement("div");
  body.className = "history-event-details";
  appendDetail(body, "Event type", item.event_type === "incident" ? "Incident" : "Monitoring gap");
  appendDetail(
    body,
    item.event_type === "incident" ? "Status" : "Reason",
    item.event_type === "incident"
      ? (item.categories || [item.category]).map(label).join(" → ")
      : label(item.category),
  );
  appendDetail(body, "State", label(item.state));
  appendDetail(body, "Started", formatDate(item.start, true));
  appendDetail(body, "Ended", item.end ? formatDate(item.end, true) : "Still open");
  appendDetail(body, "Duration", item.end ? formatDuration(item.duration_seconds) : "Ongoing");
  if (item.event_type === "incident") {
    appendDetail(body, "Confirmed start", formatOptionalDate(item.confirmed_start));
    appendDetail(body, "Confirmed end", formatOptionalDate(item.confirmed_end));
    appendDetail(body, "End reason", item.end_reason ? label(item.end_reason) : "Not available");
  }
  appendDetail(body, "Event ID", item.event_id, true);
  details.append(summary, body);
  if (item.event_type === "incident") {
    const explanation = document.createElement("p");
    explanation.className = "incident-explanation";
    const continuity = item.phase_count > 1
      ? `One continuous incident with ${item.phase_count} classification phases. `
      : "";
    explanation.textContent = continuity + incidentExplanation(item.failed_tests);
    details.append(explanation);
    if (item.phases?.length > 1) details.append(incidentPhases(item.phases));
  }
  return details;
}

function incidentPhases(phases) {
  const section = document.createElement("div");
  section.className = "incident-phases";
  const heading = document.createElement("strong");
  heading.textContent = "Classification phases";
  section.append(heading);
  phases.forEach((phase) => {
    const row = document.createElement("div");
    const status = document.createElement("span");
    status.className = "status-pill";
    status.dataset.status = phase.category;
    status.textContent = label(phase.category);
    const timing = document.createElement("span");
    timing.textContent = `${formatDate(phase.start, true)} · ${
      phase.end ? formatDuration(phase.duration_seconds) : "ongoing"
    }`;
    row.append(status, timing);
    section.append(row);
  });
  return section;
}

function incidentExplanation(failedTests) {
  if (failedTests === null || failedTests === undefined) {
    return "Failed-test details are unavailable because the original probe data has expired.";
  }
  if (!failedTests.length) {
    return "No individual failed test was recorded when this incident was confirmed.";
  }
  const tests = failedTests.map((test) => {
    const name = test.label || label(test.target_id);
    const reason = test.error_class ? label(test.error_class) : label(test.outcome);
    return `${name} (${reason})`;
  });
  return `Failed when confirmed: ${tests.join(", ")}.`;
}

function appendDetail(container, name, value, code = false) {
  const item = document.createElement("div");
  const term = document.createElement("span");
  term.textContent = name;
  const content = document.createElement(code ? "code" : "strong");
  content.textContent = value;
  item.append(term, content);
  container.append(item);
}

function renderPagination(payload) {
  const first = payload.total ? payload.offset + 1 : 0;
  const last = Math.min(payload.offset + payload.items.length, payload.total);
  const page = Math.floor(payload.offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(payload.total / PAGE_SIZE));
  elements.count.textContent = `Showing ${first}-${last} of ${payload.total} events`;
  elements.pageLabel.textContent = `Page ${page} of ${pages}`;
  elements.previous.disabled = payload.offset === 0;
  elements.next.disabled = payload.offset + PAGE_SIZE >= payload.total;
}

function focusKey() {
  return state.focusType && state.focusId ? `${state.focusType}:${state.focusId}` : null;
}

function itemKey(item) {
  return `${item.event_type}:${item.event_id}`;
}

function eventDomId(key) {
  return `event-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function label(value) {
  const labels = Object.fromEntries([...CATEGORY_GROUPS.incident, ...CATEGORY_GROUPS.gap]);
  return labels[value] || value.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function formatDate(value, includeSeconds = false) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: state.timezone,
    dateStyle: "medium",
    timeStyle: includeSeconds ? "medium" : "short",
  }).format(new Date(value));
}

function formatOptionalDate(value) {
  return value ? formatDate(value, true) : "Not available";
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

function emptyState(message) {
  const node = document.createElement("p");
  node.className = "empty-state";
  node.textContent = message;
  return node;
}

elements.type.addEventListener("change", () => {
  updateCategories();
  updateExportLink();
});
elements.form.addEventListener("input", updateExportLink);
elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!elements.form.reportValidity()) return;
  if (elements.start.value && elements.end.value &&
      new Date(elements.end.value) <= new Date(elements.start.value)) {
    elements.error.textContent = "Until must be after From.";
    elements.error.hidden = false;
    return;
  }
  state.offset = 0;
  state.focusType = null;
  state.focusId = null;
  loadHistory();
});
elements.reset.addEventListener("click", () => {
  elements.form.reset();
  updateCategories();
  state.offset = 0;
  state.focusType = null;
  state.focusId = null;
  loadHistory();
});
elements.previous.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  state.focusType = null;
  state.focusId = null;
  loadHistory();
});
elements.next.addEventListener("click", () => {
  state.offset += PAGE_SIZE;
  state.focusType = null;
  state.focusId = null;
  loadHistory();
});

updateCategories();
api("/api/v1/status")
  .then((status) => {
    state.timezone = status.site.timezone;
    elements.siteName.textContent = `${status.hostname} · Detailed history`;
    elements.appVersion.textContent = `Version ${status.version}`;
    document.title = `${status.hostname} · Detailed history`;
    return loadHistory();
  })
  .catch((error) => {
    elements.error.textContent = `Could not load history: ${error.message}`;
    elements.error.hidden = false;
  });
