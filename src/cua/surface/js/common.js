// Shared helpers injected into every page-side script (observer, selector engines, human capture).
// Keep `norm` in sync with cua/textutil.py::normalize_label.

const __cua = (() => {
  const norm = (s) =>
    (s || "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[\s:*]+$/, "")
      .toLowerCase();

  const clean = (s) => (s || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();

  const textOf = (el) => clean(el ? (el.innerText !== undefined ? el.innerText : el.textContent) : "");

  const rendered = (el) => {
    if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
    const st = el.ownerDocument.defaultView.getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  };

  const CONTROL_SELECTOR =
    "a[href],button,input,select,textarea,[role],[onclick],[contenteditable=true]";

  const INTERACTIVE_ROLES = new Set([
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "checkbox", "radio",
    "switch", "tab", "menuitem", "option", "spinbutton", "slider",
  ]);

  function controlRole(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (INTERACTIVE_ROLES.has(explicit)) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "hidden") return null;
      if (["button", "submit", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "search") return "searchbox";
      if (t === "number") return "spinbutton";
      return "textbox";
    }
    if (
      el.hasAttribute("onclick") &&
      !el.querySelector("a[href],button,input,select,textarea") &&
      textOf(el).length > 0 &&
      textOf(el).length <= 80
    )
      return "clickable";
    return null;
  }

  function accName(el) {
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by
        .split(/\s+/)
        .map((id) => textOf(el.ownerDocument.getElementById(id)))
        .join(" ");
      if (clean(t)) return clean(t);
    }
    const al = el.getAttribute("aria-label");
    if (clean(al)) return clean(al);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && ["button", "submit", "reset"].includes(type)) return clean(el.value);
    if (tag === "input" && type === "image") return clean(el.getAttribute("alt"));
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map(textOf).join(" "));
    if (tag === "a" || tag === "button" || el.getAttribute("role")) {
      const t = textOf(el);
      if (t) return t;
    }
    return clean(el.getAttribute("title") || el.getAttribute("placeholder") || "");
  }

  // Labels a human would read for a control, programmatic or purely positional.
  function layoutLabels(el) {
    const out = [];
    const push = (t) => {
      t = clean(t);
      if (t && !out.some((o) => norm(o) === norm(t))) out.push(t);
    };
    if (el.labels) Array.from(el.labels).forEach((l) => push(textOf(l)));
    push(el.getAttribute("aria-label"));
    const by = el.getAttribute("aria-labelledby");
    if (by) by.split(/\s+/).forEach((id) => push(textOf(el.ownerDocument.getElementById(id))));
    const cell = el.closest("td,th");
    if (cell) {
      for (let prev = cell.previousElementSibling; prev; prev = prev.previousElementSibling) {
        if (prev.querySelector("input,select,textarea,button")) continue;
        if (norm(textOf(prev))) {
          push(textOf(prev));
          break;
        }
      }
    }
    for (let sib = el.previousSibling; sib; sib = sib.previousSibling) {
      if (sib.nodeType === 3) {
        if (norm(sib.textContent)) {
          push(sib.textContent);
          break;
        }
        continue;
      }
      if (sib.nodeType !== 1) continue;
      if (sib.matches("input,select,textarea,button,br,hr")) break;
      if (norm(textOf(sib))) {
        push(textOf(sib));
        break;
      }
    }
    push(el.getAttribute("placeholder"));
    push(el.getAttribute("title"));
    return out;
  }

  // ---- tables -------------------------------------------------------------------------------

  function cellsWithColumns(row) {
    let col = 0;
    return Array.from(row.cells).map((c) => {
      const entry = { cell: c, col };
      col += c.colSpan || 1;
      return entry;
    });
  }

  // Header row = first row of the table containing a cell whose text equals `column`.
  function findColumn(table, column) {
    const want = norm(column);
    const rows = Array.from(table.rows);
    for (let i = 0; i < rows.length; i++) {
      const hit = cellsWithColumns(rows[i]).find((e) => norm(textOf(e.cell)) === want);
      if (hit) return { headerIndex: i, col: hit.col };
    }
    return null;
  }

  function cellAt(row, col) {
    const hit = cellsWithColumns(row).find((e) => e.col === col);
    return hit ? hit.cell : null;
  }

  // (row keys, column header) describing a data cell, for the table_cell locator.
  function cellCoordinates(cell) {
    if (!cell || !cell.matches("td,th") || valueLabel(cell)) return null; // key/value layouts use label_value
    const row = cell.parentElement;
    const table = row && row.closest("table");
    if (!table) return null;
    const rows = Array.from(table.rows);
    const rIdx = rows.indexOf(row);
    const myCol = cellsWithColumns(row).find((e) => e.cell === cell);
    if (rIdx <= 0 || !myCol) return null;
    const header = rows[0];
    const hdrCell = cellAt(header, myCol.col);
    const column = hdrCell ? textOf(hdrCell) : "";
    if (!norm(column)) return null;
    const keys = cellsWithColumns(row)
      .filter((e) => e.cell !== cell)
      .map((e) => {
        const h = cellAt(header, e.col);
        return { text: textOf(e.cell), column: h ? textOf(h) : "" };
      })
      .filter((k) => norm(k.text) && !/^[$()\-\d.,\s]+$/.test(k.text));
    // Prefer human words ("Primary Savings") over codes ("S00").
    keys.sort((a, b) => (/\d/.test(a.text) ? 1 : 0) - (/\d/.test(b.text) ? 1 : 0));
    // Keys carry their column so the caller can drop keys from sensitive columns (e.g. "Name").
    return { column, rowKeys: keys.slice(0, 4) };
  }

  // Label of a key/value pair this cell is the value of (previous non-empty sibling cell).
  function valueLabel(cell) {
    if (!cell || !cell.matches("td,th,dd")) return null;
    if (cell.matches("dd")) {
      const dt = cell.previousElementSibling;
      return dt && dt.matches("dt") ? textOf(dt) : null;
    }
    for (let prev = cell.previousElementSibling; prev; prev = prev.previousElementSibling) {
      const t = textOf(prev);
      if (norm(t)) return /:\s*$/.test(t) || prev.className === "lbl" ? t : null;
    }
    return null;
  }

  // ---- structural fallback ------------------------------------------------------------------

  function cssPath(el) {
    const doc = el.ownerDocument;
    const esc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&"));
    const name = el.getAttribute("name");
    if (name) {
      const sel = `${el.tagName.toLowerCase()}[name="${name.replace(/"/g, '\\"')}"]`;
      if (doc.querySelectorAll(sel).length === 1) return sel;
    }
    if (el.id && doc.querySelectorAll(`#${esc(el.id)}`).length === 1) return `#${esc(el.id)}`;
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== doc.body; node = node.parentElement) {
      let idx = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling)
        if (s.tagName === node.tagName) idx++;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${idx})`);
    }
    return "body > " + parts.join(" > ");
  }

  return {
    norm, clean, textOf, rendered, controlRole, accName, layoutLabels,
    findColumn, cellAt, cellsWithColumns, cellCoordinates, valueLabel, cssPath, CONTROL_SELECTOR,
  };
})();
