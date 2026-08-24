import { describe, expect, it } from "vitest";

import { formatRemaining, getExpiryState } from "../../static/zulip_bot/expiry.js";

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
