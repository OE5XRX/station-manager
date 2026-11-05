/* OE5XRX Metric history — browser glue for the station Metrics tab.

   Each metric group renders a <details class="mx-hist"> (see
   stations/_metrics_panel.html). Nothing is fetched until a block is opened;
   then the group's keys are loaded from /api/v1/stations/<id>/metrics/history/
   (session auth) and drawn as one small uPlot chart per metric: avg trace
   plus a translucent min…max envelope.

   All parsing/formatting lives in metrics-history-logic.js (Node-tested).
   CSP: no inline styles via markup — DOM is built with createElement +
   textContent; colours are read from CSS tokens through probe elements so
   the canvas follows the light/dark theme. */

(function () {
  "use strict";

  var L = window.OE5XRXMetricsHistoryLogic;
  if (!L) return;

  var CHART_HEIGHT = 150;

  // ---------------------------------------------------------------------------
  // Theme colours (canvas cannot resolve var(); a probe element can)
  // ---------------------------------------------------------------------------

  function readPalette(host) {
    var names = ["line", "band", "edge", "axis", "grid"];
    var palette = {};
    names.forEach(function (n) {
      var probe = document.createElement("span");
      probe.className = "mx-hist-probe is-" + n;
      host.appendChild(probe);
      palette[n] = getComputedStyle(probe).color;
      host.removeChild(probe);
    });
    var rootStyle = getComputedStyle(document.documentElement);
    palette.font = (rootStyle.getPropertyValue("--font-mono") || "monospace").trim();
    return palette;
  }

  // ---------------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------------

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function readoutCell(kind, label) {
    var cell = el("span", "mx-agg-cell" + (kind === "avg" ? " is-avg" : ""));
    cell.appendChild(el("span", "mx-agg-k", label));
    var v = el("span", "mx-agg-v t-mono t-num");
    cell.appendChild(v);
    return { cell: cell, value: v };
  }

  // ---------------------------------------------------------------------------
  // One history block
  // ---------------------------------------------------------------------------

  function HistoryBlock(root) {
    this.root = root;
    this.station = root.getAttribute("data-history-station");
    this.keys = L.parseKeys(root.getAttribute("data-history-keys"));
    this.status = root.querySelector("[data-history-status]");
    this.chartsHost = root.querySelector("[data-history-charts]");
    this.meta = root.querySelector("[data-history-meta]");
    // Ranges longer than the API's max window (data-history-max-span) are
    // disabled; if that rules out the default, start on the largest allowed.
    this.allowed = L.allowedRanges(root.getAttribute("data-history-max-span"));
    this.range = this.allowed.indexOf(L.DEFAULT_RANGE) >= 0
      ? L.DEFAULT_RANGE
      : this.allowed[this.allowed.length - 1];
    this.loaded = false;
    this.seq = 0;
    this.abort = null;
    this.plots = [];
    this.lastPrepared = null;
    this.bind();
    this.markRange();
  }

  HistoryBlock.prototype.msg = function (name) {
    return this.root.getAttribute("data-msg-" + name) || "";
  };

  HistoryBlock.prototype.bind = function () {
    var self = this;
    this.root.addEventListener("toggle", function () {
      if (self.root.open && !self.loaded) self.load();
      if (self.root.open) self.resize();
    });
    this.root.querySelectorAll("[data-history-range]").forEach(function (btn) {
      if (self.allowed.indexOf(btn.getAttribute("data-history-range")) < 0) {
        btn.disabled = true;
        btn.setAttribute("aria-disabled", "true");
      }
      btn.addEventListener("click", function () {
        self.setRange(btn.getAttribute("data-history-range"));
      });
    });
    if (typeof ResizeObserver === "function") {
      this.observer = new ResizeObserver(function () { self.resize(); });
      this.observer.observe(this.chartsHost);
    }
  };

  HistoryBlock.prototype.setRange = function (range) {
    if (this.allowed.indexOf(range) < 0) return;
    this.range = range;
    this.markRange();
    this.load();
  };

  HistoryBlock.prototype.markRange = function () {
    var range = this.range;
    this.root.querySelectorAll("[data-history-range]").forEach(function (btn) {
      var on = btn.getAttribute("data-history-range") === range;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  };

  HistoryBlock.prototype.setStatus = function (text, kind) {
    this.status.textContent = text || "";
    this.status.className = "mx-hist-status" + (kind ? " is-" + kind : "");
    this.status.hidden = !text;
  };

  HistoryBlock.prototype.load = function () {
    var self = this;
    var seq = ++this.seq;
    if (this.abort) this.abort.abort();
    this.abort = typeof AbortController === "function" ? new AbortController() : null;
    this.loaded = true;
    if (!this.keys.length) {
      this.render(L.prepareCharts(null, []));
      return;
    }
    this.root.setAttribute("aria-busy", "true");
    this.chartsHost.classList.add("is-loading");
    this.setStatus(this.msg("loading"), "loading");

    // The API caps keys per request; a group may hold more. One request per
    // chunk, in parallel, all on the same AbortController + seq; any failing
    // chunk rejects the whole load (inline error). Same ``now`` for every
    // chunk so the windows match.
    var now = Date.now();
    var signal = this.abort ? this.abort.signal : undefined;
    var requests = L.chunkKeys(this.keys, L.MAX_KEYS_PER_REQUEST).map(function (chunk) {
      return fetch(L.buildUrl(self.station, chunk, self.range, now), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: signal,
      }).then(function (resp) {
        if (!resp.ok) {
          var err = new Error("HTTP " + resp.status);
          err.status = resp.status;
          throw err;
        }
        return resp.json();
      });
    });
    Promise.all(requests)
      .then(function (bodies) {
        if (seq !== self.seq) return;
        self.render(L.prepareCharts(L.mergeResponses(bodies), self.keys));
      })
      .catch(function (err) {
        if (seq !== self.seq || (err && err.name === "AbortError")) return;
        self.fail(err);
      })
      .then(function () {
        if (seq !== self.seq) return;
        self.root.removeAttribute("aria-busy");
        self.chartsHost.classList.remove("is-loading");
      });
  };

  HistoryBlock.prototype.fail = function (err) {
    var detail = err && err.status ? " (HTTP " + err.status + ")" : "";
    this.destroyPlots();
    this.chartsHost.replaceChildren();
    this.meta.textContent = "";
    this.lastPrepared = null;
    // Allow the next open/range click to retry.
    this.loaded = false;
    this.setStatus(this.msg("error") + detail + ". " + this.msg("retry"), "error");
  };

  HistoryBlock.prototype.destroyPlots = function () {
    this.plots.forEach(function (p) { p.plot.destroy(); });
    this.plots = [];
  };

  HistoryBlock.prototype.width = function () {
    var w = this.chartsHost.clientWidth;
    return w > 0 ? w : 0;
  };

  HistoryBlock.prototype.resize = function () {
    var w = this.width();
    if (!w) return;
    this.plots.forEach(function (p) {
      var pw = p.host.clientWidth || w;
      if (Math.abs(p.plot.width - pw) > 1) p.plot.setSize({ width: pw, height: CHART_HEIGHT });
    });
  };

  HistoryBlock.prototype.render = function (prepared) {
    this.destroyPlots();
    this.chartsHost.replaceChildren();
    this.lastPrepared = prepared;
    var span = L.rangeSeconds(this.range);

    this.meta.textContent = prepared.resolution
      ? this.range + " / " + this.msg("resolution") + " " + prepared.resolution
      : "";

    if (prepared.empty || typeof window.uPlot !== "function") {
      // Review Focus 5: new station / nothing recorded yet → empty state, no axes.
      this.setStatus(this.msg("empty"), "empty");
      return;
    }
    this.setStatus("", null);

    var palette = readPalette(this.chartsHost);
    var self = this;
    prepared.charts.forEach(function (chart) {
      self.chartsHost.appendChild(self.buildChart(chart, prepared, span, palette));
    });
  };

  HistoryBlock.prototype.buildChart = function (chart, prepared, span, palette) {
    var figure = el("figure", "mx-hist-chart");
    var head = el("figcaption", "mx-hist-head");
    var title = el("div", "mx-hist-title");
    title.appendChild(el("span", "mx-label", chart.label));
    var when = el("span", "mx-hist-when t-mono-sm");
    title.appendChild(when);
    head.appendChild(title);

    var readout = el("div", "mx-agg mx-hist-readout");
    var rMin = readoutCell("min", "min");
    var rAvg = readoutCell("avg", "avg");
    var rMax = readoutCell("max", "max");
    readout.appendChild(rMin.cell);
    readout.appendChild(rAvg.cell);
    readout.appendChild(rMax.cell);
    head.appendChild(readout);
    figure.appendChild(head);

    var host = el("div", "mx-hist-plot");
    figure.appendChild(host);

    var unit = chart.unit;
    var prec = chart.precision;
    function show(idx) {
      var d = chart.data;
      var ok = idx !== null && idx !== undefined && idx >= 0 && idx < d[0].length;
      rMin.value.textContent = ok ? L.formatValue(d[1][idx], prec) : "—";
      rAvg.value.textContent = ok ? L.formatValue(d[2][idx], prec) : "—";
      if (unit) rAvg.value.appendChild(el("span", "mx-unit", unit));
      rMax.value.textContent = ok ? L.formatValue(d[3][idx], prec) : "—";
      when.textContent = ok ? L.formatTime(d[0][idx], span) : "";
    }
    var latest = L.lastIndex(chart.data);
    // A point between two gaps has no line segment; draw it as a dot.
    var isolated = L.isolatedIndices(chart.data[2]);
    show(latest);

    if (!chart.count) {
      host.classList.add("is-empty");
      host.appendChild(el("p", "mx-hist-nodata", this.msg("nodata")));
      return figure;
    }

    var xMin = prepared.sinceS;
    var xMax = prepared.untilS;
    var font = "10.5px " + palette.font;
    var axisCommon = {
      stroke: palette.axis,
      font: font,
      grid: { stroke: palette.grid, width: 1 },
      ticks: { stroke: palette.grid, width: 1, size: 4 },
    };
    var opts = {
      width: this.width() || 300,
      height: CHART_HEIGHT,
      legend: { show: false },
      cursor: {
        y: false,
        points: { size: 6, fill: palette.line, stroke: palette.line },
        drag: { x: false, y: false },
      },
      scales: {
        x: {
          time: true,
          range: function (u, lo, hi) {
            return xMin !== null && xMax !== null ? [xMin, xMax] : [lo, hi];
          },
        },
        y: {
          range: function (u, lo, hi) {
            if (lo === null || hi === null) return [0, 1];
            if (lo === hi) {
              var pad = Math.abs(lo) * 0.05 || 1;
              return [lo - pad, hi + pad];
            }
            var p = (hi - lo) * 0.08;
            return [lo - p, hi + p];
          },
        },
      },
      axes: [
        Object.assign({
          space: 72,
          values: function (u, splits) { return L.axisTimeValues(splits, span); },
        }, axisCommon),
        Object.assign({
          size: 52,
          space: 22,
          values: function (u, splits) { return L.axisValues(splits, prec); },
        }, axisCommon),
      ],
      series: [
        {},
        { label: "min", stroke: palette.edge, width: 1, points: { show: false } },
        {
          label: "avg",
          stroke: palette.line,
          width: 1.75,
          points: {
            show: chart.count < 3,
            size: 5,
            fill: palette.line,
            stroke: palette.line,
            // uPlot 1.6.32: when markers are hidden, still draw these indices.
            // null when shown (an index list would restrict them) or none.
            filter: function (u, si, show) {
              return show || !isolated.length ? null : isolated;
            },
          },
        },
        { label: "max", stroke: palette.edge, width: 1, points: { show: false } },
      ],
      bands: [{ series: [3, 1], fill: palette.band }],
      hooks: {
        setCursor: [
          function (u) {
            var idx = u.cursor.idx;
            show(idx === null || idx === undefined ? latest : idx);
          },
        ],
      },
    };

    var plot = new window.uPlot(opts, chart.data, host);
    this.plots.push({ plot: plot, host: host });
    return figure;
  };

  HistoryBlock.prototype.rerender = function () {
    if (this.lastPrepared) this.render(this.lastPrepared);
  };

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------

  function init() {
    var blocks = [];
    document.querySelectorAll("details[data-history-station]").forEach(function (root) {
      if (root.__mxHistory) return;
      root.__mxHistory = new HistoryBlock(root);
      blocks.push(root.__mxHistory);
      if (root.open) root.__mxHistory.load();
    });
    if (!blocks.length) return;
    // Theme switch (data-theme on <html>) → redraw with the new token colours.
    if (typeof MutationObserver === "function") {
      new MutationObserver(function () {
        blocks.forEach(function (b) { b.rerender(); });
      }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
