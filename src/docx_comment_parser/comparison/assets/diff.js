/* docx-comment-parser diff report — inlined into every generated file.
 *
 * Same contract as the review report's script: there is no backend and no
 * network, the changes are embedded in the page as JSON, and every filter,
 * search and page turn happens here.  A diff of two large documents has to stay
 * responsive, so:
 *
 *   - changes are stored as compact arrays with authors, documents and change
 *     kinds de-duplicated into lookup tables;
 *   - filtering is a single linear scan, debounced on keystroke;
 *   - only one page of results is ever in the DOM.
 *
 * Text from the documents is inserted with textContent, never innerHTML, so a
 * comment containing markup is displayed rather than interpreted.
 */
(function () {
  "use strict";

  var node = document.getElementById("diff-data");
  if (!node) return;

  var DATA = JSON.parse(node.textContent);
  var KINDS = DATA.kinds;
  var AUTHORS = DATA.authors;
  var DOCS = DATA.documents;
  var ROWS = DATA.rows;

  /* Column offsets in a packed row. */
  var KIND = 0, KINDS_ALL = 1, LEVEL = 2, SCORE = 3, ID_BEFORE = 4, ID_AFTER = 5,
      AUTHOR = 6, DATE_DISP = 7, DATE_ISO = 8, TEXT_BEFORE = 9, TEXT_AFTER = 10,
      REF_BEFORE = 11, REF_AFTER = 12, RES_BEFORE = 13, RES_AFTER = 14, DOC = 15;

  var PAGE_SIZE = 40;

  var state = { q: "", kind: "", author: "", view: "cards", page: 0 };

  var $ = function (id) { return document.getElementById(id); };

  /* The comment a row is "about": the newer text where there is one. */
  function faceText(row) {
    return row[TEXT_AFTER] || row[TEXT_BEFORE];
  }
  function faceRef(row) {
    return row[REF_AFTER] || row[REF_BEFORE];
  }
  function faceId(row) {
    return row[ID_AFTER] >= 0 ? row[ID_AFTER] : row[ID_BEFORE];
  }

  /* ── Theme ──────────────────────────────────────────────────────────────── */

  var toggle = $("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var dark = root.getAttribute("data-theme") === "dark";
      if (!root.hasAttribute("data-theme")) {
        // No explicit choice yet: flip away from whatever the OS is doing.
        dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      }
      root.setAttribute("data-theme", dark ? "light" : "dark");
    });
  }

  /* ── Author list ────────────────────────────────────────────────────────── */
  /* Built from the data rather than the template so the two can never drift. */

  var authorSelect = $("f-author");
  if (authorSelect) {
    AUTHORS.slice().sort().forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name || "—";
      authorSelect.appendChild(option);
    });
  }

  /* ── Filtering ──────────────────────────────────────────────────────────── */

  function matches(row) {
    if (state.author && AUTHORS[row[AUTHOR]] !== state.author) return false;
    if (state.kind) {
      var wanted = KINDS.indexOf(state.kind);
      if (wanted === -1 || row[KINDS_ALL].indexOf(wanted) === -1) return false;
    }
    if (state.q) {
      var q = state.q;
      if (row[TEXT_AFTER].toLowerCase().indexOf(q) === -1 &&
          row[TEXT_BEFORE].toLowerCase().indexOf(q) === -1 &&
          faceRef(row).toLowerCase().indexOf(q) === -1 &&
          AUTHORS[row[AUTHOR]].toLowerCase().indexOf(q) === -1) return false;
    }
    return true;
  }

  function selected() {
    var out = [];
    for (var i = 0; i < ROWS.length; i++) if (matches(ROWS[i])) out.push(ROWS[i]);
    return out;
  }

  /* ── Rendering helpers ──────────────────────────────────────────────────── */

  /* Insert text, wrapping matches of the active query in <mark>. Built from
   * text nodes so document content is never parsed as HTML. */
  function withHighlight(el, text) {
    if (!state.q) { el.textContent = text; return; }
    var lower = text.toLowerCase();
    var q = state.q;
    var at = 0, found;
    while ((found = lower.indexOf(q, at)) !== -1) {
      if (found > at) el.appendChild(document.createTextNode(text.slice(at, found)));
      var hit = document.createElement("mark");
      hit.textContent = text.slice(found, found + q.length);
      el.appendChild(hit);
      at = found + q.length;
    }
    if (at < text.length) el.appendChild(document.createTextNode(text.slice(at)));
  }

  function tag(kindIndex) {
    var name = KINDS[kindIndex];
    var span = document.createElement("span");
    span.className = "tag " + name;
    // The word is the signal; the coloured dot only reinforces it.
    span.textContent = name === "reanchored" ? "re-anchored"
      : name === "reopened" ? "re-opened" : name;
    return span;
  }

  function tagsFor(row) {
    var host = document.createElement("span");
    host.className = "tags";
    row[KINDS_ALL].forEach(function (index) { host.appendChild(tag(index)); });
    return host;
  }

  function cell(tr, className) {
    var td = document.createElement("td");
    if (className) td.className = className;
    tr.appendChild(td);
    return td;
  }

  function statusWord(flag) {
    return flag < 0 ? "—" : (flag ? "Resolved" : "Open");
  }

  /* ── Table view ─────────────────────────────────────────────────────────── */

  function renderTable(rows, host) {
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var hrow = document.createElement("tr");
    var cols = ["Change", "ID", "Author", "Comment", "Before", "After"];
    if (DOCS.length > 1) cols.push("Document");
    cols.forEach(function (name, index) {
      var th = document.createElement("th");
      th.textContent = name;
      if (index === 1) th.className = "num";
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      cell(tr).appendChild(tagsFor(row));
      cell(tr, "num").textContent = faceId(row);
      withHighlight(cell(tr), AUTHORS[row[AUTHOR]]);
      withHighlight(cell(tr), faceText(row));
      cell(tr).textContent = statusWord(row[RES_BEFORE]);
      cell(tr).textContent = statusWord(row[RES_AFTER]);
      if (DOCS.length > 1) cell(tr).textContent = DOCS[row[DOC]];
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  /* ── Detail view ────────────────────────────────────────────────────────── */

  function side(label, text, className) {
    var box = document.createElement("div");
    box.className = "side " + className;
    var head = document.createElement("div");
    head.className = "s-label";
    head.textContent = label;
    box.appendChild(head);
    var body = document.createElement("div");
    body.className = "c-text";
    withHighlight(body, text);
    box.appendChild(body);
    return box;
  }

  function renderCards(rows, host) {
    rows.forEach(function (row) {
      var card = document.createElement("div");
      card.className = "change";

      var head = document.createElement("div");
      head.className = "c-head";
      head.appendChild(tagsFor(row));

      var who = document.createElement("span");
      who.className = "c-author";
      withHighlight(who, AUTHORS[row[AUTHOR]] || "—");
      head.appendChild(who);

      var ident = document.createElement("span");
      ident.className = "c-id";
      ident.textContent = "#" + faceId(row);
      head.appendChild(ident);

      var when = document.createElement("span");
      when.textContent = row[DATE_DISP];
      head.appendChild(when);

      if (DOCS.length > 1) {
        var doc = document.createElement("span");
        doc.textContent = DOCS[row[DOC]];
        head.appendChild(doc);
      }

      if (row[LEVEL]) {
        var level = document.createElement("span");
        level.className = "c-score";
        // How the pair was matched, and how alike the two texts are. Shown
        // because a fuzzy match is a judgement the reader may want to check.
        level.textContent = "matched by " + row[LEVEL] +
          (row[LEVEL] === "fuzzy" ? " · " + Math.round(row[SCORE] * 100) + "%" : "");
        head.appendChild(level);
      }
      card.appendChild(head);

      var edited = row[KINDS_ALL].indexOf(KINDS.indexOf("edited")) !== -1;
      if (edited && row[TEXT_BEFORE] && row[TEXT_AFTER]) {
        var sides = document.createElement("div");
        sides.className = "sides";
        sides.appendChild(side("Was", row[TEXT_BEFORE], "was"));
        sides.appendChild(side("Now", row[TEXT_AFTER], "now"));
        card.appendChild(sides);
      } else {
        var body = document.createElement("div");
        body.className = "c-text";
        withHighlight(body, faceText(row));
        card.appendChild(body);
      }

      var anchor = faceRef(row);
      if (anchor) {
        var quote = document.createElement("div");
        quote.className = "c-anchor";
        withHighlight(quote, anchor);
        card.appendChild(quote);
      }

      host.appendChild(card);
    });
  }

  /* ── Paint ──────────────────────────────────────────────────────────────── */

  var host = $("results");
  var pager = $("pager");
  var countEl = $("result-count");

  function render() {
    var rows = selected();
    var pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    if (state.page >= pages) state.page = pages - 1;
    var slice = rows.slice(state.page * PAGE_SIZE, state.page * PAGE_SIZE + PAGE_SIZE);

    while (host.firstChild) host.removeChild(host.firstChild);

    if (countEl) {
      countEl.textContent = rows.length === ROWS.length
        ? rows.length + " change" + (rows.length === 1 ? "" : "s")
        : rows.length + " of " + ROWS.length + " changes";
    }

    if (!rows.length) {
      var none = document.createElement("p");
      none.className = "empty-state";
      none.textContent = "No changes match these filters.";
      host.appendChild(none);
    } else if (state.view === "table") {
      renderTable(slice, host);
    } else {
      renderCards(slice, host);
    }

    if (pager) {
      pager.hidden = rows.length <= PAGE_SIZE;
      var info = $("page-info");
      if (info) {
        info.textContent = "Page " + (state.page + 1) + " of " + pages +
          " · " + rows.length + " changes";
      }
      $("prev").disabled = state.page === 0;
      $("next").disabled = state.page >= pages - 1;
    }
  }

  /* ── Wiring ─────────────────────────────────────────────────────────────── */

  function bind(id, event, handler) {
    var el = $(id);
    if (el) el.addEventListener(event, handler);
  }

  var debounce;
  bind("f-search", "input", function (evt) {
    clearTimeout(debounce);
    var value = evt.target.value.trim().toLowerCase();
    // Typing is the one filter that fires per keystroke; a short delay keeps a
    // full scan off the critical path of each character.
    debounce = setTimeout(function () { state.q = value; state.page = 0; render(); }, 110);
  });
  bind("f-kind", "change", function (e) { state.kind = e.target.value; state.page = 0; render(); });
  bind("f-author", "change", function (e) { state.author = e.target.value; state.page = 0; render(); });

  bind("f-reset", "click", function () {
    state.q = ""; state.kind = ""; state.author = ""; state.page = 0;
    ["f-search", "f-kind", "f-author"].forEach(function (id) {
      var el = $(id);
      if (el) el.value = "";
    });
    render();
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function (btn) {
    btn.addEventListener("click", function () {
      state.view = btn.getAttribute("data-view");
      state.page = 0;
      Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function (other) {
        other.setAttribute("aria-pressed", String(other === btn));
      });
      render();
    });
  });

  bind("prev", "click", function () {
    if (state.page > 0) { state.page--; render(); window.scrollTo(0, $("changes").offsetTop - 20); }
  });
  bind("next", "click", function () {
    state.page++; render(); window.scrollTo(0, $("changes").offsetTop - 20);
  });

  render();
})();
