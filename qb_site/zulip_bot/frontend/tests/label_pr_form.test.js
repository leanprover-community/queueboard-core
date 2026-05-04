import { describe, expect, it } from "vitest";

import { mountLabelPrForm } from "../../static/zulip_bot/label_pr_form.js";

describe("mountLabelPrForm", () => {
  const futureExp = () => Math.floor(Date.now() / 1000) + 3600;

  function buildDom(expUnix, { bugChecked = false, featureChecked = false } = {}) {
    document.body.innerHTML = `
      <form id="label-pr-form" data-exp-unix="${expUnix}">
        <input type="checkbox" name="selected_labels" value="bug" ${bugChecked ? "checked" : ""} />
        <input type="checkbox" name="selected_labels" value="feature" ${featureChecked ? "checked" : ""} />
        <button id="label-pr-submit" type="submit">Update Labels</button>
        <button id="label-select-all" type="button">Select All</button>
        <button id="label-clear-all" type="button">Clear All</button>
        <span id="label-picker-count"></span>
      </form>
      <strong id="label-pr-countdown-label"></strong>
      <span id="label-pr-countdown-text"></span>
    `;
  }

  function triggerBeforeUnload() {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }

  it("disables submit when link is already expired", () => {
    buildDom(1);
    const unmount = mountLabelPrForm(document);
    expect(document.getElementById("label-pr-submit").disabled).toBe(true);
    expect(document.getElementById("label-pr-countdown-text").textContent).toContain("expired");
    unmount();
  });

  it("enables submit when link is not yet expired", () => {
    buildDom(futureExp());
    const unmount = mountLabelPrForm(document);
    expect(document.getElementById("label-pr-submit").disabled).toBe(false);
    unmount();
  });

  it("does not warn on close when form is pristine", () => {
    buildDom(futureExp());
    const unmount = mountLabelPrForm(document);
    expect(triggerBeforeUnload()).toBe(false);
    unmount();
  });

  it("warns on close when a checkbox is changed from initial state", () => {
    buildDom(futureExp());
    const unmount = mountLabelPrForm(document);
    const cb = document.querySelector('input[value="bug"]');
    cb.checked = true;
    cb.dispatchEvent(new Event("change", { bubbles: true }));
    expect(triggerBeforeUnload()).toBe(true);
    unmount();
  });

  it("warns on close when a pre-checked label is unchecked", () => {
    buildDom(futureExp(), { bugChecked: true });
    const unmount = mountLabelPrForm(document);
    const cb = document.querySelector('input[value="bug"]');
    cb.checked = false;
    cb.dispatchEvent(new Event("change", { bubbles: true }));
    expect(triggerBeforeUnload()).toBe(true);
    unmount();
  });

  it("does not warn when link is expired even if a checkbox changed", () => {
    buildDom(1);
    const unmount = mountLabelPrForm(document);
    const cb = document.querySelector('input[value="bug"]');
    cb.checked = true;
    cb.dispatchEvent(new Event("change", { bubbles: true }));
    expect(triggerBeforeUnload()).toBe(false);
    unmount();
  });

  it("does not warn after form is submitted with a label checked", () => {
    buildDom(futureExp());
    const unmount = mountLabelPrForm(document);
    const cb = document.querySelector('input[value="bug"]');
    cb.checked = true;
    cb.dispatchEvent(new Event("change", { bubbles: true }));
    expect(triggerBeforeUnload()).toBe(true);
    document.getElementById("label-pr-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(triggerBeforeUnload()).toBe(false);
    unmount();
  });

  it("selectAll marks form dirty when not all boxes were initially checked", () => {
    buildDom(futureExp());
    const unmount = mountLabelPrForm(document);
    document.getElementById("label-select-all").click();
    expect(triggerBeforeUnload()).toBe(true);
    unmount();
  });

  it("clearAll marks form dirty when some boxes were initially checked", () => {
    buildDom(futureExp(), { bugChecked: true });
    const unmount = mountLabelPrForm(document);
    document.getElementById("label-clear-all").click();
    expect(triggerBeforeUnload()).toBe(true);
    unmount();
  });

  it("selectAll does not mark dirty when all boxes were already checked", () => {
    buildDom(futureExp(), { bugChecked: true, featureChecked: true });
    const unmount = mountLabelPrForm(document);
    document.getElementById("label-select-all").click();
    expect(triggerBeforeUnload()).toBe(false);
    unmount();
  });

  it("confirms before submitting with no labels selected", () => {
    buildDom(futureExp());
    let confirmed = false;
    window.confirm = () => { confirmed = true; return false; };
    const unmount = mountLabelPrForm(document);
    document.getElementById("label-pr-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(confirmed).toBe(true);
    unmount();
  });
});
