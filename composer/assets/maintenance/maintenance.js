(function () {
  "use strict";
  var statusUrl = "/_update/status.json";
  var logUrl = "/_update/log.txt";
  var phaseEl = document.getElementById("phase");
  var updatedEl = document.getElementById("updated");
  var barEl = document.getElementById("bar");
  var progressEl = document.querySelector(".progress");
  var statusTextEl = document.getElementById("statusText");
  var logEl = document.getElementById("log");
  var recoveryTimer = null;
  var percentByStatus = {
    starting: 12,
    preparing: 16,
    pulling: 30,
    recreating: 58,
    restarting: 72,
    migrating: 86,
    ready: 100,
    failed: 100,
    backing_up: 52,
    maintenance: 60,
    collecting_static: 78,
    switching: 86,
    verifying_health: 96,
    completed: 100,
    rolled_back: 100
  };

  function label(value) {
    return String(value || "maintenance").replace(/_/g, " ");
  }

  // The proxy serves this page as a 503 at the URL the visitor asked for, so
  // once the update reaches an end state, probe that same URL and return to
  // it as soon as the app answers. A stale end state from an earlier update
  // is harmless: the probe keeps getting 503 until the site is really back.
  function scheduleRecoveryProbe() {
    if (recoveryTimer !== null) return;
    var target = window.location.href;
    function probeRecovery() {
      recoveryTimer = null;
      fetch(target, { cache: "no-store", credentials: "same-origin" })
        .then(function (response) {
          if (response.ok) {
            window.location.replace(target);
            return;
          }
          recoveryTimer = window.setTimeout(probeRecovery, 2000);
        })
        .catch(function () {
          recoveryTimer = window.setTimeout(probeRecovery, 2000);
        });
    }
    recoveryTimer = window.setTimeout(probeRecovery, 1000);
  }

  function setStatus(doc) {
    var status = String((doc && doc.status) || "maintenance");
    var percent = percentByStatus[status] || 20;
    phaseEl.textContent = label(status);
    updatedEl.textContent = doc && doc.updated_at ? "Updated " + doc.updated_at : "Waiting for status...";
    barEl.style.width = percent + "%";
    progressEl.setAttribute("aria-valuenow", String(percent));
    statusTextEl.textContent = doc && doc.error ? doc.error : "Current phase: " + label(status);
    if (status === "failed") {
      barEl.classList.add("is-failed");
      scheduleRecoveryProbe();
    } else if (status === "ready" || status === "completed" || status === "rolled_back") {
      barEl.classList.add("is-done");
      scheduleRecoveryProbe();
    }
  }

  function fetchStatus() {
    fetch(statusUrl, { cache: "no-store" })
      .then(function (response) { return response.ok ? response.json() : {}; })
      .then(setStatus)
      .catch(function () { setStatus({ status: "maintenance" }); });
  }

  function fetchLog() {
    fetch(logUrl, { cache: "no-store" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (text) {
        if (!text) return;
        logEl.textContent = text;
        logEl.classList.remove("muted");
        logEl.scrollTop = logEl.scrollHeight;
      })
      .catch(function () {});
  }

  fetchStatus();
  fetchLog();
  window.setInterval(fetchStatus, 3000);
  window.setInterval(fetchLog, 3000);
})();
