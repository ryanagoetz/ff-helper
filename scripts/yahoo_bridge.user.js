// ==UserScript==
// @name         ff-helper draft bridge
// @namespace    ff-helper
// @version      1.0
// @description  Read the Yahoo draft board and send it to ff-helper. Read-only.
// @match        https://football.fantasysports.yahoo.com/draftclient/*
// @match        https://football.fantasysports.yahoo.com/draft*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @run-at       document-idle
// ==/UserScript==

/*
 * What this does, and what it deliberately does not.
 *
 * It reads the text of the draft-results panel every few seconds and posts it, whole, to
 * ff-helper. It never clicks anything, never bids, never nominates. Bidding is your
 * decision and irreversible in a live auction; this script has no business near it.
 *
 * It does NOT try to understand Yahoo's DOM. It takes innerText and lets the server parse
 * it -- the same parser the paste box uses, tested against text copied out of a real
 * draft room. That is the whole trick: a selector that changes breaks a scraper, but here
 * the worst case is that we grab a slightly bigger blob of text and the parser ignores
 * the parts that are not sales.
 *
 * It sends the WHOLE board every time, never a delta. The server matches sales by player,
 * so re-sending is free and self-correcting: a missed poll, a page refresh, a restart,
 * even a mis-read price all come right on the next pass.
 *
 * GM_xmlhttpRequest rather than fetch, so there is no CORS, no preflight, no
 * private-network check, and no mixed-content question -- and the ff-helper endpoint
 * stays unreachable from ordinary web pages.
 *
 * Install: Tampermonkey -> Create a new script -> paste this -> save. Then open the draft
 * room. A badge appears bottom-right; click it to pause.
 */

(function () {
  "use strict";

  const ENDPOINT = "http://127.0.0.1:8777/api/board/paste";
  const EVERY_MS = 4000;

  // Tried in order; the first that exists and has text wins. The last is the whole page,
  // which always works because the server ignores anything that is not a sale.
  const PANELS = [
    "[data-id='draft-results']",
    "#draft-results",
    ".draft-results",
    "[class*='DraftResults']",
    "[class*='draftresults']",
    "body",
  ];

  let paused = false;
  let lastSent = "";
  let lastOk = null;

  const badge = document.createElement("div");
  badge.style.cssText = [
    "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
    "font:12px/1.4 system-ui,sans-serif", "padding:8px 11px", "border-radius:8px",
    "background:#171a21", "color:#e6e9ef", "border:1px solid #2a2f3a",
    "cursor:pointer", "max-width:320px", "box-shadow:0 2px 10px rgba(0,0,0,.4)",
  ].join(";");
  badge.title = "Click to pause or resume the ff-helper bridge";
  badge.addEventListener("click", () => {
    paused = !paused;
    say(paused ? "paused — click to resume" : "resuming…", paused ? "#fbbf24" : "#99a1b3");
  });
  document.body.appendChild(badge);

  function say(text, colour) {
    badge.textContent = "ff-helper: " + text;
    badge.style.color = colour || "#e6e9ef";
  }

  function readBoard() {
    for (const selector of PANELS) {
      const node = document.querySelector(selector);
      const text = node && (node.innerText || "");
      // A real results panel has prices in it. Without one we are looking at the wrong
      // element, so keep going rather than posting a blob with no sales in it.
      if (text && /\$\s*\d/.test(text)) return text;
    }
    return "";
  }

  function post(text) {
    GM_xmlhttpRequest({
      method: "POST",
      url: ENDPOINT,
      headers: { "Content-Type": "application/json" },
      // strict:false — an unattended reader must not stall the rest of the draft over one
      // unmatchable name. An unresolved BUYER still blocks, server-side, because that
      // corrupts every price rather than merely leaving the board stale.
      data: JSON.stringify({ text: text, strict: false }),
      timeout: 8000,
      onload: function (res) {
        if (res.status >= 400) {
          let detail = res.responseText;
          try { detail = JSON.parse(res.responseText).detail || detail; } catch (e) {}
          say("REFUSED — " + String(detail).slice(0, 160), "#f87171");
          return;
        }
        let body = {};
        try { body = JSON.parse(res.responseText); } catch (e) {}
        lastSent = text;
        lastOk = new Date();
        const bits = [body.read + " read"];
        if (body.applied) bits.push(body.applied + " new");
        if (body.corrected) bits.push(body.corrected + " fixed");
        if (body.skipped && body.skipped.length) bits.push(body.skipped.length + " skipped");
        say(bits.join(", ") + " · " + lastOk.toLocaleTimeString(),
            body.skipped && body.skipped.length ? "#fbbf24" : "#4ade80");
        if (body.skipped && body.skipped.length) {
          console.warn("[ff-helper] not recorded:", body.skipped);
        }
        if (body.assumed && body.assumed.length) {
          console.warn("[ff-helper] resolved by price, worth checking:", body.assumed);
        }
      },
      onerror: function () {
        say("cannot reach ff-helper — is it running on 8777?", "#f87171");
      },
      ontimeout: function () { say("timed out talking to ff-helper", "#f87171"); },
    });
  }

  function tick() {
    if (paused) return;
    const text = readBoard();
    if (!text) { say("no sales on screen yet", "#99a1b3"); return; }
    // Nothing changed since the last successful post, so there is nothing to say.
    if (text === lastSent) return;
    post(text);
  }

  say("starting…");
  tick();
  setInterval(tick, EVERY_MS);
})();
