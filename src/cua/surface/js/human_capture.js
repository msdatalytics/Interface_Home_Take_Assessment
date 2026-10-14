// Installed as an init script in every frame. Reports operator interactions (click, change,
// Enter) with a semantic descriptor so a human's manual steps during a handoff can be
// recorded as candidate capability steps. Python decides whether an event came from the
// human (only while the human holds the control lease); values are redacted host-side.
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  __COMMON__;
  const { controlRole, accName, layoutLabels, textOf, cellCoordinates, valueLabel, cssPath } = __cua;

  const describe = (raw, event) => {
    const el =
      (raw.closest && raw.closest("a[href],button,input,select,textarea,[role],[onclick]")) || raw;
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    const type = (el.getAttribute && (el.getAttribute("type") || "").toLowerCase()) || "";
    const role = controlRole(el);
    const cell = el.closest ? el.closest("td,th,dd") : null;
    let value = null;
    if (event === "change") {
      if (tag === "select") value = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : "";
      else if (type === "password") value = "\u2022\u2022\u2022\u2022";
      else if ("value" in el) value = el.value;
    }
    return {
      event, role, tag, name: role ? accName(el) : null,
      labels: role ? layoutLabels(el) : [],
      text: role ? null : textOf(el).slice(0, 200),
      value, sensitive: type === "password",
      cell: cell ? cellCoordinates(cell) : null,
      valueLabel: cell ? valueLabel(cell) : null,
      css: el.nodeType === 1 ? cssPath(el) : null,
      frame: window.name || null, path: location.pathname,
    };
  };

  const send = (payload) => {
    try {
      if (typeof window.__cuaHumanEvent === "function") window.__cuaHumanEvent(payload);
    } catch (e) {
      /* capture must never break the page */
    }
  };

  document.addEventListener("click", (ev) => send(describe(ev.target, "click")), true);
  // Text fields: report the value once typing pauses. ("change" fires on blur, i.e. when the
  // operator merely clicks elsewhere - it would attribute an earlier automated fill to them.)
  const pending = new WeakMap();
  document.addEventListener(
    "input",
    (ev) => {
      const el = ev.target;
      if (!el || el.tagName === "SELECT" || ["checkbox", "radio"].includes(el.type)) return;
      clearTimeout(pending.get(el));
      pending.set(el, setTimeout(() => send(describe(el, "change")), 400));
    },
    true,
  );
  document.addEventListener(
    "change",
    (ev) => {
      const el = ev.target;
      if (el && (el.tagName === "SELECT" || ["checkbox", "radio"].includes(el.type))) send(describe(el, "change"));
    },
    true,
  );
  document.addEventListener(
    "keydown",
    (ev) => {
      if (ev.key === "Enter") send(describe(ev.target, "enter"));
    },
    true,
  );
})();
