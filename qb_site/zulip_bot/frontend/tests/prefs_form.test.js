import { describe, expect, it } from "vitest";

import { formatRemaining, getExpiryState, mountPrefsForm } from "../../static/zulip_bot/prefs_form.js";

describe("getExpiryState", () => {
  it("reports not expired when remaining time is positive", () => {
    const state = getExpiryState(2000, 1_999_000);
    expect(state.expired).toBe(false);
    expect(state.remainingMs).toBe(1_000);
  });

  it("reports expired when remaining time is zero or less", () => {
    const exact = getExpiryState(100, 100_000);
    const past = getExpiryState(100, 100_500);
    expect(exact.expired).toBe(true);
    expect(past.expired).toBe(true);
  });
});

describe("formatRemaining", () => {
  it("formats hours/minutes/seconds", () => {
    expect(formatRemaining(3_723_000)).toBe("1h 2m 3s");
  });

  it("formats minute-only durations", () => {
    expect(formatRemaining(125_000)).toBe("2m 5s");
  });

  it("handles expired durations", () => {
    expect(formatRemaining(0)).toBe("expired");
    expect(formatRemaining(-1_000)).toBe("expired");
  });
});

describe("mountPrefsForm", () => {
  it("disables submit when link is already expired", () => {
    document.body.innerHTML = `
      <main id="prefs-root" data-exp-unix="1" data-exp-iso="1970-01-01T00:00:01Z" data-timezone="UTC">
        <time id="expires-at"></time>
        <span id="countdown-label"></span>
        <span id="countdown-text"></span>
        <span id="submit-hint"></span>
        <form id="prefs-form">
          <input name="field" value="x" />
          <button id="submit-button" type="submit">Save</button>
          <button type="button" class="js-clear-away" data-target="away"></button>
          <input id="away" />
        </form>
      </main>
    `;

    const unmount = mountPrefsForm(document);
    const submit = document.getElementById("submit-button");
    const text = document.getElementById("countdown-text");
    expect(submit.disabled).toBe(true);
    expect(text.textContent).toContain("expired");
    unmount();
  });
});
