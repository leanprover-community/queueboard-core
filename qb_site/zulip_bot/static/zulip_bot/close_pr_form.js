import { formatRemaining, getExpiryState } from "./prefs_form.js";

export function mountClosePrForm(root = document) {
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
