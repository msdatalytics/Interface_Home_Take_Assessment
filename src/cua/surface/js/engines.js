// Custom Playwright selector engines. Each is registered as `__ENGINE__` with a URI-encoded JSON
// body, e.g.  cua-label=%7B%22label%22%3A%22Member%20Number%22%2C%22role%22%3A%22textbox%22%7D
// Only rendered elements are returned so hidden duplicates never make a locator ambiguous.
(() => {
  __COMMON__;
  const { norm, textOf, rendered, controlRole, layoutLabels, findColumn, cellAt } = __cua;
  const parse = (body) => JSON.parse(decodeURIComponent(body));

  const engines = {
    "cua-label"(root, body) {
      const { label, role } = parse(body);
      const want = norm(label);
      return Array.from(root.querySelectorAll(__cua.CONTROL_SELECTOR)).filter(
        (el) =>
          rendered(el) &&
          controlRole(el) === role &&
          layoutLabels(el).some((l) => norm(l) === want),
      );
    },

    "cua-cell"(root, body) {
      const { row, column } = parse(body);
      const wantRow = norm(row);
      const out = [];
      for (const table of root.querySelectorAll("table")) {
        const hit = findColumn(table, column);
        if (!hit) continue;
        const rows = Array.from(table.rows);
        for (let i = hit.headerIndex + 1; i < rows.length; i++) {
          if (!Array.from(rows[i].cells).some((c) => norm(textOf(c)) === wantRow)) continue;
          const cell = cellAt(rows[i], hit.col);
          if (cell && rendered(cell)) out.push(cell);
        }
      }
      return out;
    },

    "cua-value"(root, body) {
      const { label } = parse(body);
      const want = norm(label);
      const out = new Set();
      for (const cell of root.querySelectorAll("td,th,dt")) {
        if (norm(textOf(cell)) !== want) continue;
        if (cell.matches("dt")) {
          const dd = cell.nextElementSibling;
          if (dd && dd.matches("dd") && rendered(dd)) out.add(dd);
          continue;
        }
        let next = cell.nextElementSibling;
        while (next && !norm(textOf(next))) next = next.nextElementSibling;
        if (next && rendered(next)) out.add(next);
      }
      return Array.from(out);
    },
  };

  const impl = engines["__ENGINE__"];
  return {
    queryAll(root, body) {
      return impl(root, body);
    },
    query(root, body) {
      return impl(root, body)[0] || null;
    },
  };
})()
