import { formatRemaining, getExpiryState } from "./prefs_form.js";

function formatAgo(ms) {
  const s = ms / 1000;
  if (s < 60) return `${Math.floor(s)}s ago`;
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m ago`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h ago`;
  const d = h / 24;
  if (d < 30) return `${Math.floor(d)} day${Math.floor(d) === 1 ? "" : "s"} ago`;
  const mo = d / 30.4;
  if (mo < 12) return `${Math.floor(mo)} month${Math.floor(mo) === 1 ? "" : "s"} ago`;
  const yr = mo / 12;
  return `${Math.floor(yr)} year${Math.floor(yr) === 1 ? "" : "s"} ago`;
}

export function formatTimestamp(isoString, nowMs = Date.now()) {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  const local = d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const elapsed = nowMs - d.getTime();
  return elapsed >= 0 ? `${local} (${formatAgo(elapsed)})` : local;
}

function formatTimestamps(root) {
  for (const el of root.querySelectorAll("time.ts[data-iso]")) {
    const formatted = formatTimestamp(el.dataset.iso);
    if (formatted) el.textContent = formatted;
  }
}

export function mountClosePrForm(root = document) {
  formatTimestamps(root);

  const form = root.getElementById("close-pr-form");
  const submitButton = root.getElementById("close-pr-submit");
  const countdownText = root.getElementById("close-pr-countdown-text");
  const countdownLabel = root.getElementById("close-pr-countdown-label");
  if (!form || !submitButton || !countdownText || !countdownLabel) {
    return () => undefined;
  }

  const expUnix = Number(form.dataset.expUnix || "0");

  const update = () => {
    const state = getExpiryState(expUnix);
    if (state.expired) {
      countdownLabel.textContent = "Expired:";
      countdownText.textContent = "This link has expired. Request a fresh one in Zulip.";
      submitButton.disabled = true;
    } else {
      countdownLabel.textContent = "Expires in:";
      countdownText.textContent = formatRemaining(state.remainingMs);
      submitButton.disabled = false;
    }
  };
  update();
  const intervalId = window.setInterval(update, 1000);

  for (const btn of root.querySelectorAll(".preset-btn")) {
    btn.addEventListener("click", () => {
      const textarea = root.getElementById("close_message");
      if (textarea) {
        textarea.value = btn.getAttribute("data-body") || "";
      }
    });
  }

  form.addEventListener("submit", (event) => {
    update();
    if (submitButton.disabled) {
      event.preventDefault();
    }
  });

  return () => window.clearInterval(intervalId);
}

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    mountClosePrForm(document);
  });
}
