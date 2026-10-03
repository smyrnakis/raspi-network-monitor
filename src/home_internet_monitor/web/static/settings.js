"use strict";

const theme = window.NetworkMonitorTheme;
const buttons = document.querySelectorAll("[data-theme-choice]");
const form = document.querySelector("#monitor-settings");
const probeSettings = document.querySelector("#probe-settings");
const saveButton = document.querySelector("#save-settings");
const message = document.querySelector("#settings-message");
const keepIncidentsForever = document.querySelector("#keep-incidents-forever");
const incidentDays = document.querySelector("#incidents-days");
const incidentDaysField = document.querySelector("#incident-days-field");
const hideShortIncidents = document.querySelector("#hide-short-incidents");
let settings = null;

const KIND_LABELS = {
  gateway: "Gateway",
  external_ip: "External IP",
  dns: "DNS",
  https: "HTTPS",
};

function select(choice) {
  const selected = theme.save(choice);
  buttons.forEach((button) => {
    const active = button.dataset.themeChoice === selected;
    button.setAttribute("aria-checked", String(active));
    button.classList.toggle("active", active);
  });
}

buttons.forEach((button) => {
  button.addEventListener("click", () => select(button.dataset.themeChoice));
});

select(theme.current());

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_error) {
      // Keep the bounded generic error.
    }
    throw new Error(detail);
  }
  return response.json();
}

function numberValue(id) {
  return Number(document.querySelector(`#${id}`).value);
}

function populate(payload) {
  settings = payload;
  document.querySelector("#interval-seconds").value = payload.monitor.interval_seconds;
  document.querySelector("#round-timeout-seconds").value =
    payload.monitor.round_timeout_seconds;
  document.querySelector("#failure-threshold").value = payload.monitor.failure_threshold;
  document.querySelector("#recovery-threshold").value = payload.monitor.recovery_threshold;
  document.querySelector("#raw-samples-days").value =
    payload.retention.raw_samples_days;
  document.querySelector("#latency-aggregates-days").value =
    payload.retention.latency_aggregates_days;
  keepIncidentsForever.checked = payload.retention.incidents_days === null;
  incidentDays.value = payload.retention.incidents_days ?? 730;
  hideShortIncidents.checked = payload.dashboard.hide_short_incidents;
  document.querySelector("#mtbf-minimum-incident-minutes").value =
    payload.dashboard.mtbf_minimum_incident_minutes;
  document.querySelector("#default-timeline-window").value =
    payload.dashboard.default_timeline_window;
  document.querySelector("#default-latency-window").value =
    payload.dashboard.default_latency_window;
  updateIncidentRetentionState();
  probeSettings.replaceChildren(...payload.probes.map(probeEditor));
  saveButton.disabled = false;
}

function probeEditor(probe) {
  const row = document.createElement("article");
  row.className = "probe-setting";
  row.dataset.probeId = probe.id;

  const heading = document.createElement("div");
  heading.className = "probe-setting-heading";
  const title = document.createElement("strong");
  title.textContent = `${KIND_LABELS[probe.kind] || probe.kind} · ${probe.id}`;
  const enabledLabel = document.createElement("label");
  enabledLabel.className = "switch-label";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.className = "probe-enabled";
  enabled.checked = probe.enabled;
  enabledLabel.append(enabled, document.createTextNode("Enabled"));
  heading.append(title, enabledLabel);

  const fields = document.createElement("div");
  fields.className = "probe-fields";
  fields.append(
    inputField(probe.kind === "https" ? "URL" : "Address or hostname", "probe-endpoint", probe.endpoint, "text"),
    inputField("Timeout (seconds)", "probe-timeout", probe.timeout_seconds, "number"),
  );
  if (probe.kind === "https") {
    const expected = inputField(
      "Expected HTTP status",
      "probe-expected",
      probe.expected_status ?? "",
      "number",
      false,
    );
    const input = expected.querySelector("input");
    input.min = "100";
    input.max = "599";
    input.step = "1";
    fields.append(expected);
  }
  row.append(heading, fields);
  return row;
}

function inputField(labelText, className, value, type, required = true) {
  const label = document.createElement("label");
  label.append(document.createTextNode(labelText));
  const input = document.createElement("input");
  input.className = className;
  input.type = type;
  input.value = value;
  input.required = required;
  if (type === "number") {
    input.min = "0.1";
    input.step = "0.1";
  }
  label.append(input);
  return label;
}

function payloadFromForm() {
  const probes = settings.probes.map((probe) => {
    const row = probeSettings.querySelector(`[data-probe-id="${probe.id}"]`);
    return {
      ...probe,
      enabled: row.querySelector(".probe-enabled").checked,
      endpoint: row.querySelector(".probe-endpoint").value.trim(),
      timeout_seconds: Number(row.querySelector(".probe-timeout").value),
      expected_status: probe.kind === "https"
        ? optionalNumber(row.querySelector(".probe-expected").value)
        : probe.expected_status,
    };
  });
  return {
    revision: settings.revision,
    monitor: {
      interval_seconds: numberValue("interval-seconds"),
      round_timeout_seconds: numberValue("round-timeout-seconds"),
      failure_threshold: numberValue("failure-threshold"),
      recovery_threshold: numberValue("recovery-threshold"),
    },
    retention: {
      raw_samples_days: numberValue("raw-samples-days"),
      incidents_days: keepIncidentsForever.checked
        ? null
        : numberValue("incidents-days"),
      latency_aggregates_days: numberValue("latency-aggregates-days"),
    },
    dashboard: {
      hide_short_incidents: hideShortIncidents.checked,
      mtbf_minimum_incident_minutes: numberValue("mtbf-minimum-incident-minutes"),
      default_timeline_window:
        document.querySelector("#default-timeline-window").value,
      default_latency_window:
        document.querySelector("#default-latency-window").value,
    },
    probes,
  };
}

function updateIncidentRetentionState() {
  incidentDays.disabled = keepIncidentsForever.checked;
  incidentDays.required = !keepIncidentsForever.checked;
  incidentDaysField.classList.toggle("disabled-field", keepIncidentsForever.checked);
}

keepIncidentsForever.addEventListener("change", updateIncidentRetentionState);

function optionalNumber(value) {
  return value === "" ? null : Number(value);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  saveButton.disabled = true;
  message.dataset.status = "pending";
  message.textContent = "Validating and saving...";
  try {
    const result = await api("/api/v1/settings", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Action": "settings-update",
      },
      body: JSON.stringify(payloadFromForm()),
    });
    populate(result.settings);
    message.dataset.status = "success";
    message.textContent = "Saved. The monitor will restart after its current check.";
  } catch (error) {
    message.dataset.status = "error";
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
});

api("/api/v1/settings")
  .then(populate)
  .catch((error) => {
    message.dataset.status = "error";
    message.textContent = `Could not load monitoring settings: ${error.message}`;
  });
