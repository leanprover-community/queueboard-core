// Reviewer preferences form behavior for /console/preferences/ (design doc 022).
//
// Progressive enhancement only: an unsaved-changes guard, the per-row "clear away time" buttons and
// the excluded-labels picker. There is deliberately no countdown — the console session bounds this
// page, not a link TTL — which is why the expiry helpers stayed behind in
// `zulip_bot/static/zulip_bot/expiry.js` with the close-pr / label-pr token pages.

function serializeForm(form) {
  return new URLSearchParams(new FormData(form)).toString();
}

const fold = (name) => name.toLowerCase();

// Excluded-labels picker (design doc 057): a filter box over a short scrolling list of checkboxes,
// one per repository label (the JSON block the template renders), plus a removable chip per chosen
// label. Real checkboxes rather than a pop-up suggestion list: the browsers' own <datalist> shows
// three suggestions at a time on iOS and none at all in Firefox for Android, and an inline list
// cannot end up hidden behind a phone's keyboard. The textarea stays in the form, hidden, as the
// submitted field: one label per line, which is what the server parses. So without JS the textarea
// still works, and the server stays the only validator.
export function mountLabelPicker(container, root = document) {
  const textarea = container.querySelector("textarea");
  const optionsScript = root.getElementById(container.dataset.options || "");
  if (!(textarea instanceof HTMLTextAreaElement) || !optionsScript) {
    return () => undefined;
  }
  let names;
  try {
    names = JSON.parse(optionsScript.textContent);
  } catch {
    return () => undefined;
  }
  if (!Array.isArray(names)) {
    return () => undefined;
  }

  const catalog = new Map();
  for (const raw of names) {
    const name = String(raw).trim();
    if (name && !catalog.has(fold(name))) {
      catalog.set(fold(name), name);
    }
  }
  const selected = [];
  for (const line of textarea.value.split("\n")) {
    const name = line.trim();
    if (name && !selected.some((chosen) => fold(chosen) === fold(name))) {
      selected.push(catalog.get(fold(name)) ?? name);
    }
  }
  const isSelected = (name) => selected.some((chosen) => fold(chosen) === fold(name));

  const fieldLabel = root.querySelector(`label[for="${textarea.id}"]`);
  const fieldName = fieldLabel ? fieldLabel.textContent.trim() : "Labels";

  const chips = root.createElement("ul");
  chips.className = "label-chips";
  chips.setAttribute("aria-label", `${fieldName}: chosen`);

  const filter = root.createElement("input");
  filter.type = "text";
  filter.id = `${textarea.id}-filter`;
  filter.className = "label-filter";
  filter.placeholder = "Filter labels";
  filter.autocomplete = "off";
  filter.spellcheck = false;
  // Label names are not words: keep the phone keyboard from capitalizing or "correcting" them.
  filter.setAttribute("autocapitalize", "none");
  filter.setAttribute("autocorrect", "off");
  filter.setAttribute("enterkeyhint", "done");

  const list = root.createElement("ul");
  list.className = "label-options";
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", `${fieldName}: all labels in this repository`);
  const empty = root.createElement("li");
  empty.className = "label-options-empty";
  empty.hidden = true;

  const status = root.createElement("span");
  status.className = "help label-picker-status";
  status.setAttribute("aria-live", "polite");

  // The field's <label> should name the box the reviewer types into, not the hidden textarea.
  if (fieldLabel) {
    fieldLabel.htmlFor = filter.id;
  }

  // Write the choice back to the textarea and tell the unsaved-changes guard about it.
  const commit = (message) => {
    textarea.value = selected.join("\n");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    status.textContent = message;
    renderChips();
  };

  const boxes = new Map();
  for (const name of catalog.values()) {
    const item = root.createElement("li");
    const label = root.createElement("label");
    label.className = "label-option";
    const box = root.createElement("input");
    box.type = "checkbox"; // no `name`: only the textarea is submitted
    box.value = name;
    box.checked = isSelected(name);
    box.addEventListener("change", () => {
      const index = selected.findIndex((chosen) => fold(chosen) === fold(name));
      if (box.checked && index === -1) {
        selected.push(name);
        commit(`Added ${name}.`);
      } else if (!box.checked && index !== -1) {
        selected.splice(index, 1);
        commit(`Removed ${name}.`);
      }
    });
    const text = root.createElement("span");
    text.textContent = name;
    label.append(box, text);
    item.append(label);
    list.append(item);
    boxes.set(fold(name), { item, box });
  }
  list.append(empty);

  function renderChips() {
    chips.replaceChildren(
      ...selected.map((name, index) => {
        const chip = root.createElement("li");
        chip.className = "label-chip";
        const text = root.createElement("span");
        text.textContent = name;
        chip.append(text);
        if (!catalog.has(fold(name))) {
          // Saved earlier, since renamed or deleted on GitHub: it matches nothing (the server flags it too).
          chip.classList.add("is-legacy");
          chip.title = "No longer a label in this repository, so it matches nothing.";
        }
        const remove = root.createElement("button");
        remove.type = "button";
        remove.className = "label-chip-remove";
        remove.setAttribute("aria-label", `Remove ${name}`);
        remove.textContent = "×";
        remove.addEventListener("click", (event) => {
          selected.splice(index, 1);
          const entry = boxes.get(fold(name));
          if (entry) {
            entry.box.checked = false;
          }
          commit(`Removed ${name}.`);
          // Keyboard users keep their place; a tap (detail > 0) leaves focus alone, so a phone
          // does not pop its keyboard up after every removal.
          if (event.detail === 0) {
            const next = chips.querySelectorAll(".label-chip-remove")[Math.min(index, selected.length - 1)];
            (next || filter).focus();
          }
        });
        chip.append(remove);
        return chip;
      }),
    );
    chips.hidden = selected.length === 0;
  }

  const visibleBoxes = () => [...boxes.values()].filter(({ item }) => !item.hidden).map(({ box }) => box);

  const onFilter = () => {
    const query = fold(filter.value.trim());
    for (const [key, { item }] of boxes) {
      item.hidden = query !== "" && !key.includes(query);
    }
    const shown = visibleBoxes().length;
    empty.hidden = shown > 0;
    empty.textContent = shown > 0 ? "" : `No labels match “${filter.value.trim()}”.`;
  };
  const onKeydown = (event) => {
    if (event.key !== "Enter") {
      return;
    }
    // Enter in a text input would submit the whole form. With exactly one match left, it ticks it
    // and clears the filter, ready for the next label.
    event.preventDefault();
    const shown = visibleBoxes();
    if (filter.value.trim() && shown.length === 1 && !shown[0].checked) {
      shown[0].checked = true;
      shown[0].dispatchEvent(new Event("change", { bubbles: true }));
      filter.value = "";
      onFilter();
    }
  };
  filter.addEventListener("input", onFilter);
  filter.addEventListener("keydown", onKeydown);

  const fallbackHints = [...container.querySelectorAll(".js-label-picker-fallback")];
  textarea.hidden = true;
  for (const hint of fallbackHints) {
    hint.hidden = true;
  }
  textarea.after(chips, filter, list, status);
  renderChips();

  return () => {
    filter.removeEventListener("input", onFilter);
    filter.removeEventListener("keydown", onKeydown);
    chips.remove();
    filter.remove();
    list.remove();
    status.remove();
    textarea.hidden = false;
    for (const hint of fallbackHints) {
      hint.hidden = false;
    }
    if (fieldLabel) {
      fieldLabel.htmlFor = textarea.id;
    }
  };
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

  // Before the snapshot below. Mounting never rewrites the textarea, so the page starts clean.
  const unmountPickers = [...root.querySelectorAll(".js-label-picker")].map((picker) => mountLabelPicker(picker, root));

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
    for (const unmount of unmountPickers) {
      unmount();
    }
  };
}

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    mountPrefsForm(document);
  });
}
