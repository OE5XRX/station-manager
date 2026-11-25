/* OE5XRX collapsible control-page section — Alpine component `cpCollapsible(key)`
   (SSTV spec §5.6). Repeater list, scanner and SSTV section start collapsed; the
   choice is remembered per browser in localStorage["cp-collapse:" + key]
   ("1" = open, "0" = closed). Storage failures (private mode, quota) only lose
   the memory, never the toggle.

   Used as a nested scope inside a panel's own x-data root, so it only owns
   `open` and `toggle()`. Exported for the Node test suite. */

(function () {
  "use strict";

  var PREFIX = "cp-collapse:";

  function storage() {
    try {
      return window.localStorage || null;
    } catch (_) {
      return null;
    }
  }

  function readOpen(store, key) {
    try {
      return !!store && store.getItem(PREFIX + key) === "1";
    } catch (_) {
      return false;
    }
  }

  function cpCollapsible(key, store) {
    var name = String(key || "");
    var backing = store === undefined ? storage() : store;
    return {
      open: readOpen(backing, name),
      toggle: function () {
        this.open = !this.open;
        try {
          if (backing) backing.setItem(PREFIX + name, this.open ? "1" : "0");
        } catch (_) {
          // memory lost, toggle still works
        }
      },
    };
  }

  var api = { cpCollapsible: cpCollapsible, PREFIX: PREFIX };
  if (typeof window !== "undefined") window.OE5XRXCollapsible = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("alpine:init", function () {
      window.Alpine.data("cpCollapsible", function (key) {
        return cpCollapsible(key);
      });
    });
  }
})();
