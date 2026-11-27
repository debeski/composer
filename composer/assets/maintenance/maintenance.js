(function () {
  "use strict";
  var statusUrl = "/_update/status.json";
  var logUrl = "/_update/log.txt";
  // A status older than this describes an earlier update, not this outage.
  var STALE_STATUS_MS = 15 * 60 * 1000;
  var TERMINAL = { ready: true, completed: true, failed: true, rolled_back: true };
  var STRINGS = {
    en: {
      dir: "ltr",
      title: "Temporarily unavailable",
      heading: "This site is temporarily unavailable",
      summary: "The application is restarting or under maintenance. This page returns to the site as soon as it is back.",
      updateTitle: "Update in progress",
      updateHeading: "Application update in progress",
      updateSummary: "DjangoLux and Composer are applying the verified image update. This page stays available while the application containers restart.",
      waiting: "Waiting for status...",
      updated: "Updated ",
      phase: "Current phase: ",
      log: "Waiting for deployment log...",
      phases: {}
    },
    ar: {
      dir: "rtl",
      title: "الموقع غير متاح مؤقتًا",
      heading: "الموقع غير متاح مؤقتًا",
      summary: "التطبيق قيد إعادة التشغيل أو الصيانة. ستعود هذه الصفحة إلى الموقع فور عودته.",
      updateTitle: "جارٍ التحديث",
      updateHeading: "جارٍ تحديث التطبيق",
      updateSummary: "يطبّق DjangoLux وComposer تحديث الصورة الموثّق. تبقى هذه الصفحة متاحة أثناء إعادة تشغيل حاويات التطبيق.",
      waiting: "بانتظار الحالة...",
      updated: "آخر تحديث ",
      phase: "المرحلة الحالية: ",
      log: "بانتظار سجل النشر...",
      phases: {
        starting: "البدء",
        preparing: "التحضير",
        pulling: "تنزيل الصورة",
        recreating: "إعادة الإنشاء",
        restarting: "إعادة التشغيل",
        migrating: "ترحيل قاعدة البيانات",
        ready: "جاهز",
        failed: "فشل",
        backing_up: "النسخ الاحتياطي",
        maintenance: "صيانة",
        collecting_static: "تجميع الملفات الثابتة",
        switching: "التبديل",
        verifying_health: "التحقق من السلامة",
        completed: "اكتمل",
        rolled_back: "تم التراجع"
      }
    }
  };

  function pickLanguage() {
    var wanted = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || "en"];
    for (var i = 0; i < wanted.length; i++) {
      var code = String(wanted[i] || "").toLowerCase().split("-")[0];
      if (STRINGS[code]) return code;
    }
    return "en";
  }

  var lang = pickLanguage();
  var text = STRINGS[lang];
  var root = document.documentElement;
  root.lang = lang;
  root.dir = text.dir;

  var headingEl = document.getElementById("heading");
  var summaryEl = document.getElementById("summary");
  var metaEl = document.getElementById("meta");
  var phaseEl = document.getElementById("phase");
  var updatedEl = document.getElementById("updated");
  var barEl = document.getElementById("bar");
  var progressEl = document.getElementById("progress");
  var statusTextEl = document.getElementById("statusText");
  var logEl = document.getElementById("log");
  var recoveryTimer = null;
  var updating = false;
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
    var key = String(value || "maintenance");
    return text.phases[key] || key.replace(/_/g, " ");
  }

  // Update wording only while an update run is reporting progress; any other
  // outage (a crash loop, a stopped service) gets the neutral page.
  function isActiveUpdate(doc) {
    var status = String((doc && doc.status) || "");
    if (!status || TERMINAL[status]) return false;
    var stamp = doc.updated_at ? Date.parse(doc.updated_at) : NaN;
    return isNaN(stamp) || Date.now() - stamp < STALE_STATUS_MS;
  }

  function showUpdate(active) {
    updating = active;
    document.title = active ? text.updateTitle : text.title;
    headingEl.textContent = active ? text.updateHeading : text.heading;
    summaryEl.textContent = active ? text.updateSummary : text.summary;
    metaEl.hidden = !active;
    progressEl.hidden = !active;
    statusTextEl.hidden = !active;
    logEl.hidden = !active;
  }

  // The proxy serves this page as a 503 at the URL the visitor asked for, so
  // once the update reaches an end state, or when no update is running at all,
  // probe that same URL and return to it as soon as the app answers.
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
    var active = isActiveUpdate(doc);
    showUpdate(active);
    if (!active) {
      scheduleRecoveryProbe();
      return;
    }
    var status = String((doc && doc.status) || "maintenance");
    var percent = percentByStatus[status] || 20;
    phaseEl.textContent = label(status);
    updatedEl.textContent = doc && doc.updated_at ? text.updated + doc.updated_at : text.waiting;
    barEl.style.width = percent + "%";
    progressEl.setAttribute("aria-valuenow", String(percent));
    statusTextEl.textContent = doc && doc.error ? doc.error : text.phase + label(status);
  }

  function fetchStatus() {
    fetch(statusUrl, { cache: "no-store" })
      .then(function (response) { return response.ok ? response.json() : {}; })
      .then(setStatus)
      .catch(function () { setStatus({}); });
  }

  function fetchLog() {
    if (!updating) return;
    fetch(logUrl, { cache: "no-store" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (body) {
        if (!body) return;
        logEl.textContent = body;
        logEl.classList.remove("muted");
        logEl.scrollTop = logEl.scrollHeight;
      })
      .catch(function () {});
  }

  showUpdate(false);
  updatedEl.textContent = text.waiting;
  logEl.textContent = text.log;
  fetchStatus();
  window.setInterval(fetchStatus, 3000);
  window.setInterval(fetchLog, 3000);
})();
