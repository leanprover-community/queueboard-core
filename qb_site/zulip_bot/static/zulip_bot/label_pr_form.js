import { formatRemaining, getExpiryState } from "./prefs_form.js";
import { formatTimestamp } from "./close_pr_form.js";

function serializeForm(form) {
  return new URLSearchParams(new FormData(form)).toString();
}

function formatTimestamps(root) {
  for (const el of root.querySelectorAll("time.ts[data-iso]")) {
    const formatted = formatTimestamp(el.dataset.iso);
    if (formatted) el.textContent = formatted;
  }
}

function updateCount(checkboxes, countEl) {
  const checked = Array.from(checkboxes).filter((cb) => cb.checked).length;
  countEl.textContent = `${checked} of ${checkboxes.length} selected`;
}

export function mountLabelPrForm(root = document) {
  formatTimestamps(root);

  const form = root.getElementById("label-pr-form");
  const submitButton = root.getElementById("label-pr-submit");
  const countdownText = root.getElementById("label-pr-countdown-text");
  const countdownLabel = root.getElementById("label-pr-countdown-label");
  if (!form || !submitButton || !countdownText || !countdownLabel) {
    return () => undefined;
  }

  const expUnix = Number(form.dataset.expUnix || "0");
  const checkboxes = form.querySelectorAll('input[type="checkbox"][name="selected_labels"]');
  const countEl = root.getElementById("label-picker-count");

  let dirty = false;
  const initialSnapshot = serializeForm(form);
  const updateDirty = () => {
    dirty = serializeForm(form) !== initialSnapshot;
  };

  if (countEl) updateCount(checkboxes, countEl);
  form.addEventListener("change", () => {
    if (countEl) updateCount(checkboxes, countEl);
    updateDirty();
  });

  const onBeforeUnload = (event) => {
    if (!dirty || submitButton.disabled) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", onBeforeUnload);

  const filterInput = root.getElementById("label-filter-input");
  if (filterInput) {
    filterInput.addEventListener("input", () => {
      const q = filterInput.value.trim().toLowerCase();
      for (const item of root.querySelectorAll(".label-list-item")) {
        const name = (item.querySelector(".label-chip")?.textContent ?? "").toLowerCase();
        item.hidden = q.length > 0 && !name.includes(q);
      }
    });
  }

  const selectAll = root.getElementById("label-select-all");
  const clearAll = root.getElementById("label-clear-all");
  if (selectAll) {
    selectAll.addEventListener("click", () => {
      for (const cb of checkboxes) cb.checked = true;
      if (countEl) updateCount(checkboxes, countEl);
      updateDirty();
    });
  }
  if (clearAll) {
    clearAll.addEventListener("click", () => {
      for (const cb of checkboxes) cb.checked = false;
      if (countEl) updateCount(checkboxes, countEl);
      updateDirty();
    });
  }

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

  form.addEventListener("submit", (event) => {
    update();
    if (submitButton.disabled) {
      event.preventDefault();
      return;
    }
    const checked = Array.from(checkboxes).filter((cb) => cb.checked);
    if (checked.length === 0) {
      if (!window.confirm("No labels are selected. This will remove all labels from this issue/PR. Continue?")) {
        event.preventDefault();
        return;
      }
    }
    dirty = false;
  });

  return () => {
    window.clearInterval(intervalId);
    window.removeEventListener("beforeunload", onBeforeUnload);
  };
}

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    mountLabelPrForm(document);
  });
}
