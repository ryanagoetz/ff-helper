/* ff-helper draft bridge — console version, no extension needed.
 *
 * Paste this into the DevTools console on the Yahoo draft room tab (F12 → Console).
 * Set TOKEN to the value ff-helper prints when started with --bridge, and PORT to the
 * port it is running on.
 *
 *     uv run ff-helper --offline data/league-mock.yaml --port 8779 --bridge
 *
 * It reads the draft-results panel every few seconds and posts the text to ff-helper,
 * which parses it. It never clicks anything, never bids, never nominates.
 *
 * Two caveats versus the Tampermonkey version:
 *   - it dies when you reload the tab, so re-paste after a refresh;
 *   - it needs ff-helper started with --bridge, which opens the endpoint to this one
 *     Yahoo origin and gates it behind the token.
 *
 * Stop it with ffStop() in the console.
 */

(() => {
  const PORT = 8779;
  const TOKEN = "PASTE_TOKEN_HERE";
  const EVERY_MS = 4000;

  const PANELS = [
    "[data-id='draft-results']",
    "#draft-results",
    ".draft-results",
    "[class*='DraftResults']",
    "[class*='draftresults']",
    "body",
  ];

  if (window.__ffStop) window.__ffStop();

  let lastSent = "";

  const badge = document.createElement("div");
  badge.style.cssText = [
    "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
    "font:12px/1.4 system-ui,sans-serif", "padding:8px 11px", "border-radius:8px",
    "background:#171a21", "color:#e6e9ef", "border:1px solid #2a2f3a",
    "max-width:320px", "box-shadow:0 2px 10px rgba(0,0,0,.4)",
  ].join(";");
  document.body.appendChild(badge);

  const say = (text, colour) => {
    badge.textContent = "ff-helper: " + text;
    badge.style.color = colour || "#e6e9ef";
  };

  const readBoard = () => {
    for (const selector of PANELS) {
      const node = document.querySelector(selector);
      const text = node && (node.innerText || "");
      // A results panel has prices in it. Without one we have the wrong element, so keep
      // looking rather than posting a blob containing no sales.
      if (text && /\$\s*\d/.test(text)) return text;
    }
    return "";
  };

  const tick = async () => {
    const text = readBoard();
    if (!text) { say("no sales on screen yet", "#99a1b3"); return; }
    if (text === lastSent) return;

    let res;
    try {
      res = await fetch(`http://127.0.0.1:${PORT}/api/board/paste`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Token": TOKEN },
        // strict:false — an unattended reader must not stall the whole draft over one
        // unmatchable name. An unresolved BUYER still blocks, server-side, because that
        // makes every price wrong rather than merely leaving the board stale.
        body: JSON.stringify({ text, strict: false }),
      });
    } catch (e) {
      say("cannot reach ff-helper — running with --bridge on " + PORT + "?", "#f87171");
      return;
    }

    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      say("REFUSED — " + String(body.detail || res.status).slice(0, 160), "#f87171");
      return;
    }

    lastSent = text;
    const bits = [body.read + " read"];
    if (body.applied) bits.push(body.applied + " new");
    if (body.corrected) bits.push(body.corrected + " fixed");
    if (body.skipped && body.skipped.length) bits.push(body.skipped.length + " skipped");
    say(bits.join(", ") + " · " + new Date().toLocaleTimeString(),
        body.skipped && body.skipped.length ? "#fbbf24" : "#4ade80");
    if (body.skipped && body.skipped.length) console.warn("[ff-helper] not recorded:", body.skipped);
    if (body.assumed && body.assumed.length) {
      console.warn("[ff-helper] resolved by price, worth checking:", body.assumed);
    }
  };

  const timer = setInterval(tick, EVERY_MS);
  window.__ffStop = () => { clearInterval(timer); badge.remove(); delete window.__ffStop; };
  window.ffStop = window.__ffStop;

  say("starting…");
  tick();
  console.log("[ff-helper] bridge running. Stop it with ffStop()");
})();
