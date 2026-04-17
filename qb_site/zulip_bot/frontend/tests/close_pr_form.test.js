import { describe, expect, it } from "vitest";

import { formatTimestamp, mountClosePrForm } from "../../static/zulip_bot/close_pr_form.js";

describe("formatTimestamp", () => {
  it("returns the original string for invalid ISO input", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });

  it("includes '(X days ago)' for a past timestamp", () => {
    const twoDaysAgo = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
    const result = formatTimestamp(twoDaysAgo);
    expect(result).toContain("2 days ago");
  });

  it("omits the ago suffix for future timestamps", () => {
    const future = new Date(Date.now() + 60_000).toISOString();
    const result = formatTimestamp(future);
    expect(result).not.toContain("ago");
  });
});

describe("mountClosePrForm", () => {
  function buildDom(expUnix) {
    document.body.innerHTML = `
      <section class="pr-card">
        <time class="ts" data-iso="2020-01-01T00:00:00Z">Jan 1, 2020</time>
      </section>
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
    expect(document.getElementById("close-pr-submit").disabled).toBe(false);
    unmount();
  });

  it("loads preset text into textarea on preset button click", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    const unmount = mountClosePrForm(document);
    document.querySelector(".preset-btn").click();
    expect(document.getElementById("close_message").value).toBe("preset text");
    unmount();
  });

  it("confirms before submitting with empty close message", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    let confirmed = false;
    window.confirm = () => { confirmed = true; return false; };
    const unmount = mountClosePrForm(document);
    document.getElementById("close-pr-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(confirmed).toBe(true);
    unmount();
  });

  it("does not confirm when close message is present", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    let confirmed = false;
    window.confirm = () => { confirmed = true; return true; };
    const unmount = mountClosePrForm(document);
    document.getElementById("close_message").value = "some message";
    document.getElementById("close-pr-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(confirmed).toBe(false);
    unmount();
  });

  it("formats .ts time elements on mount", () => {
    buildDom(Math.floor(Date.now() / 1000) + 3600);
    const unmount = mountClosePrForm(document);
    const timeEl = document.querySelector("time.ts");
    expect(timeEl.textContent).toContain("ago");
    unmount();
  });
});
