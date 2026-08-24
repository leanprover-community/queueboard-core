const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;

export function getExpiryState(expUnixSeconds, nowMs = Date.now()) {
  const expMs = Number(expUnixSeconds) * SECOND_MS;
  const remainingMs = expMs - nowMs;
  return {
    expired: remainingMs <= 0,
    remainingMs,
  };
}

export function formatRemaining(remainingMs) {
  if (remainingMs <= 0) {
    return "expired";
  }
  const totalSeconds = Math.floor(remainingMs / SECOND_MS);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  return `${minutes}m ${seconds}s`;
}

function formatExpiryDate(expIso, timezone) {
  const date = new Date(expIso);
  if (Number.isNaN(date.getTime())) {
    return expIso;
  }
  const formatter = new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
    timeZoneName: "short",
  });
  return formatter.format(date);
}

function serializeForm(form) {
  return new URLSearchParams(new FormData(form)).toString();
}

export function mountPrefsForm(root = document) {
  const container = root.getElementById("prefs-root");
  if (!container) {
    return () => undefined;
  }
  const form = root.getElementById("prefs-form");
  const submitButton = root.getElementById("submit-button");
  if (!form || !submitButton) {
    return () => undefined;
  }

  // The expiry countdown belongs to the token-link page only. The session-authenticated console page
  // (/console/preferences/) renders no countdown, so every expiry element is optional — the
  // unsaved-changes guard and the clear-away buttons below must keep working without them
  // (design doc 022).
  const countdownText = root.getElementById("countdown-text");
  const countdownLabel = root.getElementById("countdown-label");
  const hint = root.getElementById("submit-hint");
  const expiresAt = root.getElementById("expires-at");
  const expUnix = Number(container.dataset.expUnix || "0");
  const hasExpiry = Boolean(countdownText && countdownLabel && expiresAt && expUnix > 0);

  if (hasExpiry) {
    const expIso = String(container.dataset.expIso || "");
    const timezone = String(container.dataset.timezone || "UTC");
    expiresAt.textContent = formatExpiryDate(expIso, timezone);
  }

  let dirty = false;
  let initialSnapshot = serializeForm(form);

  const update = () => {
    if (!hasExpiry) {
      return;
    }
    const state = getExpiryState(expUnix);
    if (state.expired) {
      countdownLabel.textContent = "Expired:";
      countdownText.textContent = "This link has expired. Request a fresh one in Zulip.";
      submitButton.disabled = true;
      if (hint) {
        hint.textContent = "Submitting is disabled after expiration.";
      }
    } else {
      countdownLabel.textContent = "Expires in:";
      countdownText.textContent = formatRemaining(state.remainingMs);
      submitButton.disabled = false;
      if (hint) {
        hint.textContent = "You can submit repeatedly until expiration.";
      }
    }
  };
  update();
  const intervalId = hasExpiry ? window.setInterval(update, SECOND_MS) : null;

  const onClearAway = (event) => {
    const target = event.currentTarget;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const id = target.dataset.target;
    if (!id) {
      return;
    }
    const input = root.getElementById(id);
    if (input instanceof HTMLInputElement) {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  };

  for (const button of root.querySelectorAll(".js-clear-away")) {
    button.addEventListener("click", onClearAway);
  }

  form.addEventListener("input", () => {
    dirty = serializeForm(form) !== initialSnapshot;
  });

  const onBeforeUnload = (event) => {
    if (!dirty || submitButton.disabled) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", onBeforeUnload);

  form.addEventListener("submit", (event) => {
    update();
    if (submitButton.disabled) {
      event.preventDefault();
      return;
    }
    initialSnapshot = serializeForm(form);
    dirty = false;
  });

  return () => {
    if (intervalId !== null) {
      window.clearInterval(intervalId);
    }
    window.removeEventListener("beforeunload", onBeforeUnload);
    for (const button of root.querySelectorAll(".js-clear-away")) {
      button.removeEventListener("click", onClearAway);
    }
  };
}

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    mountPrefsForm(document);
  });
}
