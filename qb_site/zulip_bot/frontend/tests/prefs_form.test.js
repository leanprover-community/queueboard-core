import { describe, expect, it } from "vitest";

import { mountPrefsForm } from "../../../console/static/console/prefs_form.js";

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
