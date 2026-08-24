// Expiry helpers for the token-link pages (close-pr, label-pr): a countdown over a Fernet link's
// `exp`. Extracted from prefs_form.js when the prefs token page was retired (design doc 022, phase
// 3) — the console's prefs page has no expiry at all, so it no longer shares this code.
const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;

export function getExpiryState(expUnixSeconds, nowMs = Date.now()) {
  const expMs = Number(expUnixSeconds) * SECOND_MS;
  const remainingMs = expMs - nowMs;
  return {
    expired: remainingMs <= 0,
    remainingMs,
  };
}

export function formatRemaining(remainingMs) {
  if (remainingMs <= 0) {
    return "expired";
  }
  const totalSeconds = Math.floor(remainingMs / SECOND_MS);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  return `${minutes}m ${seconds}s`;
}
