/* docx-comment-parser review report — inlined into every generated file.
 *
 * There is no backend and no network: the comments are embedded in the page as
 * JSON and every filter, search and page turn happens here.  The report must
 * stay responsive with 5,000+ comments, so:
 *
 *   - rows are stored as compact arrays with the authors and document names
 *     de-duplicated into lookup tables, which roughly halves the file size;
 *   - filtering is a single linear scan over that array, debounced on keystroke;
 *   - only one page of results is ever in the DOM, so the browser never lays
 *     out thousands of rows at once.
 *
 * Text from the document is inserted with textContent, never innerHTML, so a
 * comment containing markup is displayed rather than interpreted.
 */
(function () {
  "use strict";

  var node = document.getElementById("report-data");
  if (!node) return;

  var DATA = JSON.parse(node.textContent);
  var AUTHORS = DATA.authors;
  var DOCS = DATA.documents;
  var ROWS = DATA.rows;

  /* Column offsets in a packed row. */
  var ID = 0, AUTHOR = 1, DATE_DISP = 2, DATE_ISO = 3, TEXT = 4,
      REF = 5, RESOLVED = 6, IS_REPLY = 7, DEPTH = 8, ROOT = 9, DOC = 10;

  var PAGE_SIZE = 50;
  var THREAD_PAGE_SIZE = 25;

  var state = { q: "", author: "", status: "", from: "", to: "", view: "threads", page: 0 };

  var $ = function (id) { return document.getElementById(id); };

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

  /* ── Chart granularity toggle ───────────────────────────────────────────── */

  var segButtons = document.querySelectorAll("[data-chart-scale]");
  Array.prototype.forEach.call(segButtons, function (btn) {
    btn.addEventListener("click", function () {
      var want = btn.getAttribute("data-chart-scale");
      Array.prototype.forEach.call(segButtons, function (other) {
        other.setAttribute("aria-pressed", String(other === btn));
      });
      Array.prototype.forEach.call(
        document.querySelectorAll("[data-chart-panel]"),
        function (panel) {
          panel.hidden = panel.getAttribute("data-chart-panel") !== want;
        }
      );
    });
  });

  /* ── Chart tooltip ──────────────────────────────────────────────────────── */

  var tip = $("tooltip");

  function showTip(evt, target) {
    if (!tip) return;
    while (tip.firstChild) tip.removeChild(tip.firstChild);

    var head = document.createElement("b");
    head.textContent = target.getAttribute("data-label");
    tip.appendChild(head);

    [["k1", "Resolved", target.getAttribute("data-resolved")],
     ["k2", "Open", target.getAttribute("data-open")]].forEach(function (item) {
      var row = document.createElement("div");
      row.className = "row";
      var swatch = document.createElement("i");
      swatch.style.background = "var(--series-" + (item[0] === "k1" ? "1" : "2") + ")";
      row.appendChild(swatch);
      row.appendChild(document.createTextNode(item[1] + ": " + item[2]));
      tip.appendChild(row);
    });

    tip.setAttribute("data-show", "1");
    // Keep the tooltip inside the viewport rather than letting it push the page.
    var box = tip.getBoundingClientRect();
    var x = Math.min(evt.clientX + 14, window.innerWidth - box.width - 8);
    var y = Math.max(8, evt.clientY - box.height - 12);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  document.addEventListener("mousemove", function (evt) {
    var hit = evt.target.closest ? evt.target.closest(".viz-hit") : null;
    if (hit) showTip(evt, hit);
    else if (tip) tip.removeAttribute("data-show");
  });

  /* ── Filtering ──────────────────────────────────────────────────────────── */

  function matches(row) {
    if (state.author && AUTHORS[row[AUTHOR]] !== state.author) return false;
    if (state.status === "resolved" && !row[RESOLVED]) return false;
    if (state.status === "open" && row[RESOLVED]) return false;
    if (state.from && (!row[DATE_ISO] || row[DATE_ISO].slice(0, 10) < state.from)) return false;
    if (state.to && (!row[DATE_ISO] || row[DATE_ISO].slice(0, 10) > state.to)) return false;
    if (state.q) {
      var q = state.q;
      if (row[TEXT].toLowerCase().indexOf(q) === -1 &&
          row[REF].toLowerCase().indexOf(q) === -1 &&
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

  function badge(resolved) {
    var span = document.createElement("span");
    span.className = "badge " + (resolved ? "done" : "open");
    // The word is the signal; the coloured dot only reinforces it.
    span.textContent = resolved ? "Resolved" : "Open";
    return span;
  }

  function cell(row, className) {
    var td = document.createElement("td");
    if (className) td.className = className;
    row.appendChild(td);
    return td;
  }

  /* ── Table view ─────────────────────────────────────────────────────────── */

  function renderTable(rows, host) {
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var hrow = document.createElement("tr");
    var cols = ["ID", "Author", "Date", "Status", "Comment", "Anchored to"];
    if (DOCS.length > 1) cols.push("Document");
    cols.forEach(function (name, index) {
      var th = document.createElement("th");
      th.textContent = name;
      if (index === 0) th.className = "num";
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      cell(tr, "num").textContent = row[ID];
      withHighlight(cell(tr), AUTHORS[row[AUTHOR]]);
      cell(tr).textContent = row[DATE_DISP];
      cell(tr).appendChild(badge(row[RESOLVED]));

      var text = cell(tr);
      if (row[IS_REPLY]) {
        var mark = document.createElement("span");
        mark.style.color = "var(--text-muted)";
        mark.textContent = "↳ ";
        text.appendChild(mark);
      }
      withHighlight(text, row[TEXT]);

      withHighlight(cell(tr), row[REF]);
      if (DOCS.length > 1) cell(tr).textContent = DOCS[row[DOC]];
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  /* ── Thread view ────────────────────────────────────────────────────────── */

  function message(row) {
    var wrap = document.createElement("div");
    wrap.className = "msg";
    if (row[DEPTH] > 0) wrap.style.marginLeft = Math.min(row[DEPTH], 4) * 16 + "px";

    var head = document.createElement("div");
    head.className = "m-head";
    var who = document.createElement("span");
    who.className = "m-author";
    withHighlight(who, AUTHORS[row[AUTHOR]]);
    head.appendChild(who);
    var when = document.createElement("span");
    when.textContent = row[DATE_DISP];
    head.appendChild(when);
    head.appendChild(badge(row[RESOLVED]));
    wrap.appendChild(head);

    var body = document.createElement("div");
    body.className = "m-text";
    withHighlight(body, row[TEXT]);
    wrap.appendChild(body);

    if (row[REF]) {
      var anchor = document.createElement("div");
      anchor.className = "m-anchor";
      withHighlight(anchor, row[REF]);
      wrap.appendChild(anchor);
    }
    return wrap;
  }

  /* Group the filtered rows into threads, preserving document order. A reply
   * whose root was filtered out still forms its own group, so nothing vanishes
   * silently. */
  function groupThreads(rows) {
    var index = {}, order = [];
    rows.forEach(function (row) {
      var key = row[DOC] + ":" + row[ROOT];
      if (!index[key]) { index[key] = []; order.push(key); }
      index[key].push(row);
    });
    return order.map(function (key) { return index[key]; });
  }

  function renderThreads(threads, host) {
    threads.forEach(function (members) {
      var det = document.createElement("details");
      det.className = "thread";
      if (threads.length <= 8) det.open = true;

      var sum = document.createElement("summary");
      var title = document.createElement("span");
      title.className = "t-title";
      var lead = members[0];
      withHighlight(title, lead[TEXT].length > 150 ? lead[TEXT].slice(0, 150) + "…" : lead[TEXT]);
      sum.appendChild(title);

      var meta = document.createElement("span");
      meta.className = "t-meta";
      var openCount = members.filter(function (m) { return !m[RESOLVED]; }).length;
      meta.textContent = AUTHORS[lead[AUTHOR]] + " · " +
        members.length + (members.length === 1 ? " comment" : " comments") +
        " · " + (openCount ? openCount + " open" : "all resolved");
      sum.appendChild(meta);
      det.appendChild(sum);

      var body = document.createElement("div");
      body.className = "t-body";
      members.forEach(function (row) { body.appendChild(message(row)); });
      det.appendChild(body);
      host.appendChild(det);
    });
  }

  /* ── Paint ──────────────────────────────────────────────────────────────── */

  var host = $("results");
  var pager = $("pager");
  var countEl = $("result-count");

  function render() {
    var rows = selected();
    var units = state.view === "threads" ? groupThreads(rows) : rows;
    var size = state.view === "threads" ? THREAD_PAGE_SIZE : PAGE_SIZE;
    var pages = Math.max(1, Math.ceil(units.length / size));
    if (state.page >= pages) state.page = pages - 1;

    var slice = units.slice(state.page * size, state.page * size + size);

    while (host.firstChild) host.removeChild(host.firstChild);

    if (countEl) {
      var openCount = 0;
      for (var i = 0; i < rows.length; i++) if (!rows[i][RESOLVED]) openCount++;
      countEl.textContent = rows.length === ROWS.length
        ? rows.length + " comments · " + openCount + " open"
        : rows.length + " of " + ROWS.length + " comments · " + openCount + " open";
    }

    if (!rows.length) {
      var none = document.createElement("p");
      none.className = "empty-state";
      none.textContent = "No comments match these filters.";
      host.appendChild(none);
    } else if (state.view === "threads") {
      renderThreads(slice, host);
    } else {
      renderTable(slice, host);
    }

    if (pager) {
      pager.hidden = units.length <= size;
      var info = $("page-info");
      if (info) {
        info.textContent = "Page " + (state.page + 1) + " of " + pages +
          " · " + units.length + (state.view === "threads" ? " threads" : " rows");
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
    // 5,000-row scan off the critical path of each character.
    debounce = setTimeout(function () { state.q = value; state.page = 0; render(); }, 110);
  });
  bind("f-author", "change", function (e) { state.author = e.target.value; state.page = 0; render(); });
  bind("f-status", "change", function (e) { state.status = e.target.value; state.page = 0; render(); });
  bind("f-from", "change", function (e) { state.from = e.target.value; state.page = 0; render(); });
  bind("f-to", "change", function (e) { state.to = e.target.value; state.page = 0; render(); });

  bind("f-reset", "click", function () {
    state.q = ""; state.author = ""; state.status = ""; state.from = ""; state.to = ""; state.page = 0;
    ["f-search", "f-author", "f-status", "f-from", "f-to"].forEach(function (id) {
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

  bind("prev", "click", function () { if (state.page > 0) { state.page--; render(); window.scrollTo(0, $("comments").offsetTop - 20); } });
  bind("next", "click", function () { state.page++; render(); window.scrollTo(0, $("comments").offsetTop - 20); });

  render();
})();
