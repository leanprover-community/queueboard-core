import { describe, expect, it } from "vitest";

import { mountClosePrForm } from "../../static/zulip_bot/close_pr_form.js";

describe("mountClosePrForm", () => {
  function buildDom(expUnix) {
    document.body.innerHTML = `
      <form id="close-pr-form" data-exp-unix="${expUnix}" data-exp-iso="1970-01-01T00:00:01Z">
        <button id="close-pr-submit" type="submit">Close this pull request</button>
        <button type="button" class="preset-btn" data-body="preset text">My Preset</button>
        <textarea id="close_message"></textarea>
      </form>
      <section class="countdown">
        <strong id="close-pr-countdown-label"></strong>
        <span id="close-pr-countdown-text"></span>
      </section>
    `;
  }

  it("disables submit when link is already expired", () => {
    buildDom(1);
    const unmount = mountClosePrForm(document);
    const submit = document.getElementById("close-pr-submit");
    const text = document.getElementById("close-pr-countdown-text");
    expect(submit.disabled).toBe(true);
    expect(text.textContent).toContain("expired");
    unmount();
  });

  it("enables submit when link is not yet expired", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    const unmount = mountClosePrForm(document);
    const submit = document.getElementById("close-pr-submit");
    expect(submit.disabled).toBe(false);
    unmount();
  });

  it("loads preset text into textarea on preset button click", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    const unmount = mountClosePrForm(document);
    document.querySelector(".preset-btn").click();
    const textarea = document.getElementById("close_message");
    expect(textarea.value).toBe("preset text");
    unmount();
  });
});
