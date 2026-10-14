// Blurs sensitive content in a frame before an evidence screenshot; `unmask` restores it.
// Independent of observation refs so it never disturbs what the agent is looking at.
(opts) => {
  __COMMON__;
  const { textOf, valueLabel, layoutLabels, cellCoordinates } = __cua;
  const columnOf = (el) => {
    const cell = el.closest("td");
    const c = cell ? cellCoordinates(cell) : null;
    return c ? c.column : null;
  };
  if (opts.unmask) {
    document.querySelectorAll("[data-cua-mask]").forEach((el) => {
      el.style.filter = el.dataset.cuaMask === "none" ? "" : el.dataset.cuaMask;
      delete el.dataset.cuaMask;
    });
    return 0;
  }
  const known = (opts.known || []).filter((k) => k && k.length >= 4);
  const patterns = [];
  for (const p of opts.patterns || []) {
    try {
      patterns.push(new RegExp(p));
    } catch (e) {
      /* python-only syntax; host-side redaction still applies to text artifacts */
    }
  }
  const labelRx = [];
  for (const p of opts.labels || []) {
    try {
      labelRx.push(new RegExp(p, "i"));
    } catch (e) {
      /* ignore */
    }
  }
  const hit = (t) => !!t && (known.some((k) => t.includes(k)) || patterns.some((r) => r.test(t)));
  const labelHit = (labels) => labels.some((l) => l && labelRx.some((r) => r.test(l)));
  let count = 0;
  const mark = (el) => {
    if (el.dataset.cuaMask !== undefined) return;
    el.dataset.cuaMask = el.style.filter || "none";
    el.style.filter = "blur(6px)";
    count++;
  };
  for (const el of document.querySelectorAll("td,th,dd,span,b,font,div,p,li,a,label")) {
    if (el.querySelector("td,th,div,p,table")) continue;
    const t = textOf(el);
    if (!t || t.length > 300) continue;
    if (hit(t) || labelHit([valueLabel(el), columnOf(el)])) mark(el);
  }
  for (const el of document.querySelectorAll("input,textarea")) {
    if (el.type === "password" || hit(el.value) || labelHit(layoutLabels(el))) mark(el);
  }
  return count;
}
