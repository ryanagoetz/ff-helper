// ==UserScript==
// @name         ff-helper news bridge
// @namespace    ff-helper
// @version      1.0
// @description  Send the article you are reading to ff-helper. Read-only.
// @match        https://www.4for4.com/*
// @match        https://4for4.com/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @run-at       document-idle
// ==/UserScript==

/*
 * What this does, and what it deliberately does not.
 *
 * It reads the text of the article you already have open and posts it, whole, to your own
 * machine. It does not log in, does not store a password, does not crawl, does not follow
 * links, and does not run when you are not reading. You are a subscriber looking at a page
 * you paid for; this copies that page's text to a local file. It never fetches anything
 * you did not open yourself.
 *
 * It does NOT try to understand the DOM. It takes innerText from the article element and
 * lets the server parse it -- the same parser the paste box uses. A selector that changes
 * breaks a scraper; here the worst case is a slightly bigger blob of text, and the server
 * ignores whatever is not a sentence about a player.
 *
 * Nothing here decides anything. The server only recognizes phrases in a fixed vocabulary,
 * each worth a fixed bounded multiplier, and everything else becomes a caveat printed next
 * to the recommendation. If a sentence moves a projection, the digest shows you that
 * sentence.
 *
 * GM_xmlhttpRequest rather than fetch, so there is no CORS preflight, no private-network
 * check, and no mixed-content question -- and the endpoint stays unreachable from ordinary
 * web pages. A plain bookmarklet cannot do this, which is why this is a userscript.
 *
 * It sends on a click, not on a timer. Capturing every page you idle on would fill the log
 * with navigation blobs, and the whole value of the log is that it is a record of what you
 * actually read and when.
 *
 * Install: Tampermonkey -> Create a new script -> paste this -> save. Then open an article
 * and click the badge bottom-right.
 */

(function () {
  "use strict";

  // ---- the only two things you edit ----------------------------------------------
  const PORT = 8778;                    // whatever scripts/news.py --serve is on
  const TOKEN = "PASTE_TOKEN_HERE";     // printed by: scripts/news.py --serve --bridge
  // ---------------------------------------------------------------------------------

  const ENDPOINT = "http://127.0.0.1:" + PORT + "/api/news/paste";

  // Tried in order; the first that exists with enough text wins. The last is the whole
  // body, which always works because the server ignores what is not reporting.
  const CONTAINERS = [
    "article",
    "main",
    "[role='main']",
    ".article-body",
    ".field--name-body",
    "#content",
    "body",
  ];

  const MIN_CHARS = 200;

  const badge = document.createElement("div");
  badge.style.cssText = [
    "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
    "font:12px/1.4 system-ui,sans-serif", "padding:8px 11px", "border-radius:8px",
    "background:#171a21", "color:#e6e9ef", "border:1px solid #2a2f3a",
    "cursor:pointer", "max-width:340px", "box-shadow:0 2px 10px rgba(0,0,0,.4)",
  ].join(";");
  badge.title = "Click to send this article to ff-helper";
  document.body.appendChild(badge);

  function say(text, colour) {
    badge.textContent = "ff-helper: " + text;
    badge.style.color = colour || "#e6e9ef";
  }

  function readArticle() {
    for (const selector of CONTAINERS) {
      const node = document.querySelector(selector);
      const text = node && (node.innerText || "");
      if (text && text.length >= MIN_CHARS) return text;
    }
    return "";
  }

  function send() {
    const text = readArticle();
    if (!text) { say("no article text on this page", "#fbbf24"); return; }

    say("sending…", "#99a1b3");
    GM_xmlhttpRequest({
      method: "POST",
      url: ENDPOINT,
      headers: { "Content-Type": "application/json", "X-Bridge-Token": TOKEN },
      data: JSON.stringify({
        text: text,
        url: location.href,
        title: document.title || "",
        captured_at: Date.now() / 1000,
      }),
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
        if (!body.stored) {
          say(body.reason || "not stored", "#fbbf24");
          return;
        }
        const flags = body.flags || [];
        if (!flags.length) {
          say("stored, nothing in the vocabulary matched", "#fbbf24");
          return;
        }
        say(flags.length + " flag(s): " +
            flags.map(function (f) { return f.player + " " + f.flag; }).join(", "),
            "#4ade80");
        // The whole point is that you can check it. Every flag, with its sentence.
        console.table(flags);
      },
      onerror: function () {
        say("cannot reach ff-helper — is news.py --serve running on " + PORT + "?", "#f87171");
      },
      ontimeout: function () { say("timed out talking to ff-helper", "#f87171"); },
    });
  }

  badge.addEventListener("click", send);
  say("click to capture this article");
})();
