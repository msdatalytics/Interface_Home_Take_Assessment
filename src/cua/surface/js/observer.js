// Builds a compact, semantic view of one frame: interactive controls plus visible text in
// reading order, each tagged with an ephemeral `data-cua-ref`. Runs in every frame.
(opts) => {
  __COMMON__;
  const { norm, clean, textOf, rendered, controlRole, accName, layoutLabels } = __cua;
  const maxItems = (opts && opts.maxItems) || 400;
  const prefix = (opts && opts.prefix) || "";

  document.querySelectorAll("[data-cua-ref]").forEach((e) => {
    e.removeAttribute("data-cua-ref");
    e.removeAttribute("data-cua-kind");
  });
  const root = document.body || document.documentElement;
  if (!root) return { items: [], heading: null, truncated: false };

  const items = [];
  const rowIds = new WeakMap();
  let rowSeq = 0;
  let n = 0;
  let truncated = false;

  const rowOf = (el) => {
    const tr = el.closest("tr");
    if (!tr) return null;
    if (!rowIds.has(tr)) rowIds.set(tr, ++rowSeq);
    return rowIds.get(tr);
  };

  const box = (el) => {
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };

  const ownText = (el) =>
    Array.from(el.childNodes).some((c) => c.nodeType === 3 && norm(c.textContent));

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
    acceptNode(el) {
      const tag = el.tagName.toLowerCase();
      if (["script", "style", "noscript", "template", "head"].includes(tag)) return NodeFilter.FILTER_REJECT;
      // A control owns its subtree (e.g. spans inside a button); a text block does not - controls
      // nested in a text block (buttons inside a modal's message box) must stay visible.
      const ctl = el.parentElement && el.parentElement.closest('[data-cua-kind="control"]');
      if (ctl) return NodeFilter.FILTER_REJECT;
      const st = window.getComputedStyle(el);
      if (st.display === "none" || st.visibility === "hidden") return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  for (let el = walker.nextNode(); el; el = walker.nextNode()) {
    if (items.length >= maxItems) {
      truncated = true;
      break;
    }
    const role = controlRole(el);
    if (role) {
      if (!rendered(el)) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      const item = {
        ref: prefix + String(++n), kind: "control", role, tag, name: accName(el),
        labels: layoutLabels(el), row: rowOf(el), box: box(el),
        disabled: !!el.disabled,
      };
      if (tag === "select") {
        item.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
        item.options = Array.from(el.options).slice(0, 40).map((o) => clean(o.text));
      } else if (tag === "input" || tag === "textarea") {
        if (["checkbox", "radio"].includes(type)) item.checked = !!el.checked;
        else if (type === "password") item.value = el.value ? "\u2022\u2022\u2022\u2022" : "";
        else if (!["button", "submit", "reset", "image"].includes(type)) item.value = el.value;
        item.sensitive = type === "password";
      }
      el.setAttribute("data-cua-ref", item.ref);
      el.setAttribute("data-cua-kind", "control");
      items.push(item);
      continue;
    }
    if (!ownText(el) || el.closest("a[href],button,select,option,textarea,label")) continue;
    if (el.parentElement && el.parentElement.closest('[data-cua-kind="text"]')) continue;
    // Attribute text to the enclosing cell when it is the cell's only content (td > b > text).
    let target = el;
    const cell = el.closest("td,th,dd");
    if (cell && cell !== el && norm(textOf(cell)) === norm(textOf(el))) target = cell;
    if (target.hasAttribute("data-cua-ref") || !rendered(target)) continue;
    const text = textOf(target);
    if (!text) continue;
    const st = window.getComputedStyle(el);
    const item = {
      ref: prefix + String(++n), kind: "text", tag: target.tagName.toLowerCase(), text: text.slice(0, 300),
      row: rowOf(target), box: box(target),
      emphasis: parseFloat(st.fontSize) + (parseInt(st.fontWeight, 10) >= 600 ? 2 : 0),
      valueLabel: __cua.valueLabel(target),
    };
    const coords = target.matches("td,th") ? __cua.cellCoordinates(target) : null;
    if (coords) item.column = coords.column;
    target.setAttribute("data-cua-ref", item.ref);
    target.setAttribute("data-cua-kind", "text");
    items.push(item);
  }

  // Heading: the most emphasised text near the top of the frame (legacy pages rarely use <h1>).
  let heading = null;
  for (const it of items) {
    if (it.kind !== "text" || it.box[1] > 160 || it.text.length > 80) continue;
    if (!heading || it.emphasis > heading.emphasis) heading = it;
  }
  return {
    items,
    heading: heading ? { ref: heading.ref, text: heading.text } : null,
    title: document.title,
    truncated,
  };
}
