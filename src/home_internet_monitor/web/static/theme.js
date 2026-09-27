"use strict";

(() => {
  const storageKey = "network-monitor-theme";
  const choices = new Set(["system", "light", "dark"]);

  function current() {
    try {
      const stored = localStorage.getItem(storageKey);
      return choices.has(stored) ? stored : "system";
    } catch (_error) {
      return "system";
    }
  }

  function apply(choice) {
    const selected = choices.has(choice) ? choice : "system";
    if (selected === "system") {
      document.documentElement.removeAttribute("data-theme");
      document.documentElement.style.colorScheme = "light dark";
    } else {
      document.documentElement.dataset.theme = selected;
      document.documentElement.style.colorScheme = selected;
    }
    return selected;
  }

  function save(choice) {
    const selected = apply(choice);
    try {
      localStorage.setItem(storageKey, selected);
    } catch (_error) {
      // The theme still applies for this page when storage is unavailable.
    }
    return selected;
  }

  window.NetworkMonitorTheme = { current, apply, save };
  apply(current());
})();
