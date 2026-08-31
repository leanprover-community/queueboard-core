// Reviewer preferences form behavior for /console/preferences/ (design doc 022).
//
// Progressive enhancement only: an unsaved-changes guard and the per-row "clear away time" buttons.
// There is deliberately no countdown — the console session bounds this page, not a link TTL — which
// is why the expiry helpers stayed behind in `zulip_bot/static/zulip_bot/expiry.js` with the
// close-pr / label-pr token pages.

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

  let dirty = false;
  let initialSnapshot = serializeForm(form);

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
    if (!dirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", onBeforeUnload);

  form.addEventListener("submit", () => {
    initialSnapshot = serializeForm(form);
    dirty = false;
  });

  return () => {
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
