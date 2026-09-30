import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { mountLabelPicker, mountPrefsForm } from "../../../console/static/console/prefs_form.js";

describe("mountPrefsForm", () => {
  it("wires the clear-away buttons and leaves submit enabled", () => {
    // There is no countdown on the console prefs page, so nothing may disable submit and the
    // progressive-enhancement bits must still mount (design doc 022).
    document.body.innerHTML = `
      <main id="prefs-root">
        <form id="prefs-form">
          <input id="away" name="away_until" value="2026-01-01T09:00" />
          <button id="submit-button" type="submit">Save</button>
          <button type="button" class="js-clear-away" data-target="away"></button>
        </form>
      </main>
    `;

    const unmount = mountPrefsForm(document);
    const submit = document.getElementById("submit-button");
    const away = document.getElementById("away");
    expect(submit.disabled).toBe(false);

    document.querySelector(".js-clear-away").click();
    expect(away.value).toBe("");
    unmount();
  });

  it("tracks unsaved changes and clears the flag on submit", () => {
    document.body.innerHTML = `
      <main id="prefs-root">
        <form id="prefs-form">
          <input id="cap" name="maximum_capacity" value="5" />
          <button id="submit-button" type="submit">Save</button>
        </form>
      </main>
    `;

    const unmount = mountPrefsForm(document);
    const form = document.getElementById("prefs-form");
    const input = document.getElementById("cap");

    let prevented = false;
    const fire = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      prevented = event.defaultPrevented;
    };

    fire();
    expect(prevented).toBe(false); // pristine

    input.value = "9";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    fire();
    expect(prevented).toBe(true); // dirty

    form.dispatchEvent(new Event("submit"));
    fire();
    expect(prevented).toBe(false); // saved
    unmount();
  });

  it("is a no-op without the form container", () => {
    document.body.innerHTML = "<main></main>";
    expect(mountPrefsForm(document)()).toBeUndefined();
  });
});

// The excluded-labels picker (design doc 057). The markup mirrors `_reviewer_prefs_fields.html`.
const CATALOG = ["LLM-generated", "needs-review, blocked", "t-algebra", "t-algebraic-geometry", "WIP"];

// jsdom does not propagate an exception thrown inside an event listener out of dispatchEvent, so a
// broken handler would look like a silently missing effect. Record them and fail the test instead.
let handlerErrors = [];
const onWindowError = (event) => handlerErrors.push(event.error ?? event.message);
beforeEach(() => {
  handlerErrors = [];
  window.addEventListener("error", onWindowError);
});
afterEach(() => {
  window.removeEventListener("error", onWindowError);
  expect(handlerErrors).toEqual([]);
});

function pickerPage(saved = "", catalog = CATALOG) {
  document.body.innerHTML = `
    <main id="prefs-root">
      <form id="prefs-form">
        <label for="id_form-0-excluded_labels">Excluded labels</label>
        <div class="label-picker js-label-picker" data-options="id_form-0-excluded_labels-options">
          <textarea id="id_form-0-excluded_labels" name="form-0-excluded_labels">${saved}</textarea>
          <script id="id_form-0-excluded_labels-options" type="application/json">${JSON.stringify(catalog)}</script>
          <span class="help js-label-picker-fallback">One label per line.</span>
        </div>
        <button id="submit-button" type="submit">Save</button>
      </form>
    </main>
  `;
  const unmount = mountPrefsForm(document);
  const box = (name) => document.querySelector(`.label-options input[value="${name}"]`);
  return {
    unmount,
    box,
    textarea: document.getElementById("id_form-0-excluded_labels"),
    filter: document.getElementById("id_form-0-excluded_labels-filter"),
    status: document.querySelector(".label-picker-status"),
    chips: () => [...document.querySelectorAll(".label-chip span")].map((el) => el.textContent),
    shown: () =>
      [...document.querySelectorAll(".label-options li:not(.label-options-empty)")]
        .filter((item) => !item.hidden)
        .map((item) => item.textContent),
    toggle: (name) => {
      box(name).click();
    },
  };
}

function typeFilter(filter, value) {
  filter.value = value;
  filter.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText" }));
}

function pressEnter(input) {
  const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  input.dispatchEvent(event);
  return event;
}

function beforeUnloadPrevented() {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

describe("excluded-labels picker", () => {
  it("replaces the textarea with chips, a filter box and a checkbox per label", () => {
    const page = pickerPage("llm-generated\ngone-label");
    expect(page.textarea.hidden).toBe(true);
    expect(document.querySelector(".js-label-picker-fallback").hidden).toBe(true);
    expect(document.querySelector("label").htmlFor).toBe(page.filter.id);
    expect(page.shown()).toEqual(CATALOG);
    // Saved labels show in the catalog's spelling and are ticked; one that left the catalog is marked.
    expect(page.chips()).toEqual(["LLM-generated", "gone-label"]);
    expect(page.box("LLM-generated").checked).toBe(true);
    expect(page.box("t-algebra").checked).toBe(false);
    expect(document.querySelectorAll(".label-chip")[1].classList.contains("is-legacy")).toBe(true);
    // The checkboxes are not form fields: only the textarea is submitted.
    expect(page.box("t-algebra").name).toBe("");
    // Mounting alone does not count as an unsaved change.
    expect(beforeUnloadPrevented()).toBe(false);
    page.unmount();
  });

  it("keeps the phone keyboard from capitalizing or correcting label names", () => {
    const page = pickerPage();
    expect(page.filter.getAttribute("autocapitalize")).toBe("none");
    expect(page.filter.getAttribute("autocorrect")).toBe("off");
    expect(page.filter.spellcheck).toBe(false);
    page.unmount();
  });

  it("filters the list case-insensitively by substring", () => {
    const page = pickerPage();
    typeFilter(page.filter, "ALG");
    expect(page.shown()).toEqual(["t-algebra", "t-algebraic-geometry"]);
    typeFilter(page.filter, "nothing-like-this");
    expect(page.shown()).toEqual([]);
    expect(document.querySelector(".label-options-empty").hidden).toBe(false);
    expect(document.querySelector(".label-options-empty").textContent).toContain("nothing-like-this");
    typeFilter(page.filter, "");
    expect(page.shown()).toEqual(CATALOG);
    page.unmount();
  });

  it("ticking a label adds it, and unticking removes it", () => {
    const page = pickerPage();
    page.toggle("needs-review, blocked");
    page.toggle("WIP");
    expect(page.chips()).toEqual(["needs-review, blocked", "WIP"]);
    // A label containing a comma stays one line, so the server keeps it whole.
    expect(page.textarea.value).toBe("needs-review, blocked\nWIP");
    expect(beforeUnloadPrevented()).toBe(true);

    page.toggle("needs-review, blocked");
    expect(page.chips()).toEqual(["WIP"]);
    expect(page.textarea.value).toBe("WIP");
    page.unmount();
  });

  it("removing a chip unticks its checkbox", () => {
    const page = pickerPage("LLM-generated\nt-algebra");
    document.querySelector('[aria-label="Remove LLM-generated"]').click();
    expect(page.chips()).toEqual(["t-algebra"]);
    expect(page.textarea.value).toBe("t-algebra");
    expect(page.box("LLM-generated").checked).toBe(false);
    expect(page.status.textContent).toBe("Removed LLM-generated.");
    page.unmount();
  });

  it("moves focus on a keyboard removal but not on a tap", () => {
    const page = pickerPage("LLM-generated\nt-algebra\nWIP");
    // A tap reports detail 1: focus stays put, so a phone keyboard does not pop up.
    document.querySelector('[aria-label="Remove LLM-generated"]').dispatchEvent(
      new MouseEvent("click", { bubbles: true, detail: 1 }),
    );
    expect(document.activeElement).not.toBe(page.filter);

    // Keyboard activation reports detail 0: focus moves to the next remove button...
    document.querySelector('[aria-label="Remove t-algebra"]').dispatchEvent(
      new MouseEvent("click", { bubbles: true, detail: 0 }),
    );
    expect(document.activeElement).toBe(document.querySelector('[aria-label="Remove WIP"]'));
    // ...and to the filter box once no chips are left.
    document.activeElement.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 0 }));
    expect(document.activeElement).toBe(page.filter);
    page.unmount();
  });

  it("Enter never submits, and ticks the only match left", () => {
    const page = pickerPage();
    typeFilter(page.filter, "t-alg");
    expect(pressEnter(page.filter).defaultPrevented).toBe(true);
    expect(page.chips()).toEqual([]); // two matches: nothing to choose between

    typeFilter(page.filter, "llm");
    pressEnter(page.filter);
    expect(page.chips()).toEqual(["LLM-generated"]);
    expect(page.filter.value).toBe(""); // ready for the next label
    expect(page.shown()).toEqual(CATALOG);
    page.unmount();
  });

  it("restores the textarea on unmount", () => {
    const page = pickerPage("t-algebra");
    page.unmount();
    expect(page.textarea.hidden).toBe(false);
    expect(document.querySelector(".label-options")).toBeNull();
    expect(document.querySelector("label").htmlFor).toBe("id_form-0-excluded_labels");
  });

  it("is a no-op without its label list", () => {
    document.body.innerHTML = `<div class="js-label-picker" data-options="missing"><textarea id="t"></textarea></div>`;
    const unmount = mountLabelPicker(document.querySelector(".js-label-picker"), document);
    expect(document.getElementById("t").hidden).toBe(false);
    expect(unmount()).toBeUndefined();
  });
});
