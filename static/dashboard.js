/*
 * Petrol Khata - Dashboard interaction layer (Phase 2).
 *
 * Vanilla JS, no framework, no CDN - matches every other page in this app
 * (see templates/settings.html's inline Products-list toggle for the same
 * "localStorage keyed by current_user.id" convention this file follows).
 *
 * Everything here is ADDITIVE: with this script blocked or failing to
 * load, every dashboard card is still visible and expanded (collapse only
 * ever hides a card body via a JS-added class - see initCollapse below),
 * card order is the server-rendered natural order, every link still
 * works, every chart still renders (interactivity is decoration on top of
 * already-complete server-rendered SVG), and the unit/compare toggles
 * simply do nothing. See Phase 2's spec section on progressive
 * enhancement for why that guarantee matters.
 */
(function () {
  "use strict";

  var layout = document.querySelector(".dash-layout");
  if (!layout) return; // this script only ever runs on the Dashboard
  var userId = layout.dataset.userId || "anon";
  var dashMain = document.getElementById("dash-main");

  // ------------------------------------------------------------- collapse --
  function initCollapse() {
    var KEY = "pk-dash-collapsed-" + userId;
    var collapsed = [];
    try {
      collapsed = JSON.parse(localStorage.getItem(KEY) || "[]");
    } catch (e) {
      collapsed = [];
    }
    var collapsedSet = new Set(Array.isArray(collapsed) ? collapsed : []);

    function apply(slug, isCollapsed) {
      var card = document.getElementById("card-" + slug);
      if (!card) return;
      var btn = card.querySelector(".card-toggle");
      var body = document.getElementById("card-body-" + slug);
      if (!btn || !body) return;
      btn.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
      card.classList.toggle("is-collapsed", isCollapsed);
    }

    function save() {
      localStorage.setItem(KEY, JSON.stringify(Array.from(collapsedSet)));
    }

    collapsedSet.forEach(function (slug) {
      apply(slug, true);
    });

    document.querySelectorAll(".dash-card > .panel-head > .card-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var card = btn.closest(".dash-card");
        var slug = card.dataset.card;
        var isCollapsed = card.classList.contains("is-collapsed");
        apply(slug, !isCollapsed);
        if (isCollapsed) {
          collapsedSet.delete(slug);
        } else {
          collapsedSet.add(slug);
        }
        save();
      });
    });

    return { KEY: KEY, collapsedSet: collapsedSet, apply: apply };
  }

  // -------------------------------------------------- reorder + reset --
  function initReorder() {
    if (!dashMain) return null;
    var KEY = "pk-dash-order-" + userId;
    var naturalOrder = Array.from(dashMain.querySelectorAll(":scope > .dash-card")).map(function (c) {
      return c.dataset.card;
    });

    function currentCards() {
      return Array.from(dashMain.querySelectorAll(":scope > .dash-card"));
    }

    // Robust to the card list changing between releases (spec section 3):
    // walk the DOM's natural order once; every slot that WAS part of the
    // saved order gets filled from the saved sequence (so all previously
    // reordered cards collectively land back in their saved order), while
    // any slug the saved list never knew about - a brand-new card, or one
    // that's simply not part of this user's role - stays exactly where it
    // naturally sits instead of disappearing or being pushed to the end.
    function applyOrder(order) {
      var cards = currentCards();
      var bySlug = {};
      cards.forEach(function (c) {
        bySlug[c.dataset.card] = c;
      });
      var natural = cards.map(function (c) {
        return c.dataset.card;
      });
      var validSaved = order.filter(function (s) {
        return bySlug[s];
      });
      var usedFromSaved = new Set(validSaved);
      var savedIdx = 0;
      var finalOrder = natural.map(function (slug) {
        if (usedFromSaved.has(slug)) {
          var next = validSaved[savedIdx];
          savedIdx += 1;
          return next;
        }
        return slug;
      });
      finalOrder.forEach(function (slug) {
        if (bySlug[slug]) dashMain.appendChild(bySlug[slug]);
      });
    }

    function save() {
      var order = currentCards().map(function (c) {
        return c.dataset.card;
      });
      localStorage.setItem(KEY, JSON.stringify(order));
    }

    var saved = [];
    try {
      saved = JSON.parse(localStorage.getItem(KEY) || "[]");
    } catch (e) {
      saved = [];
    }
    if (saved.length) applyOrder(saved);

    // Drag and drop, initiated only from the panel-head (spec section 3) -
    // draggable is switched on for the card on mousedown/touchstart inside
    // its heading, and switched back off on dragend/mouseup so grabbing
    // text or a table row elsewhere in the card never starts a drag.
    var dragSrc = null;
    currentCards().forEach(function (card) {
      var head = card.querySelector(".panel-head");
      if (head) {
        head.addEventListener("mousedown", function () {
          card.draggable = true;
        });
        head.addEventListener("touchstart", function () {
          card.draggable = true;
        }, { passive: true });
      }
      document.addEventListener("mouseup", function () {
        if (!card.classList.contains("is-dragging")) card.draggable = false;
      });
      card.addEventListener("dragstart", function (e) {
        dragSrc = card;
        card.classList.add("is-dragging");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", card.dataset.card);
        }
      });
      card.addEventListener("dragover", function (e) {
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      });
      card.addEventListener("drop", function (e) {
        e.preventDefault();
        if (!dragSrc || dragSrc === card) return;
        var cards = currentCards();
        var srcIdx = cards.indexOf(dragSrc);
        var tgtIdx = cards.indexOf(card);
        if (srcIdx < tgtIdx) {
          card.after(dragSrc);
        } else {
          card.before(dragSrc);
        }
        save();
      });
      card.addEventListener("dragend", function () {
        card.classList.remove("is-dragging");
        card.draggable = false;
        dragSrc = null;
      });
    });

    // Keyboard alternative (spec section 3): Alt+ArrowUp / Alt+ArrowDown
    // on a focused card heading moves that card one position and keeps
    // focus on the SAME button (it's the same DOM node, just relocated),
    // so repeated presses keep working without re-finding anything.
    dashMain.querySelectorAll(":scope > .dash-card > .panel-head > .card-toggle").forEach(function (btn) {
      btn.addEventListener("keydown", function (e) {
        if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
        e.preventDefault();
        e.stopPropagation();
        var card = btn.closest(".dash-card");
        if (e.key === "ArrowUp") {
          var prev = card.previousElementSibling;
          if (prev && prev.classList.contains("dash-card")) dashMain.insertBefore(card, prev);
        } else {
          var next = card.nextElementSibling;
          if (next && next.classList.contains("dash-card")) dashMain.insertBefore(next, card);
        }
        btn.focus();
        save();
      });
    });

    function reset() {
      localStorage.removeItem(KEY);
      applyOrder(naturalOrder);
    }

    return { save: save, reset: reset };
  }

  // ---------------------------------------------------- keyboard date step --
  function initDateStepping() {
    var input = document.querySelector(".date-picker-form input[type='date']");
    if (!input) return;
    document.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var el = document.activeElement;
      if (el) {
        var tag = el.tagName;
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || el.isContentEditable) return;
      }
      if (!input.value) return;
      var parts = input.value.split("-").map(Number);
      var current = new Date(parts[0], parts[1] - 1, parts[2]);
      current.setDate(current.getDate() + (e.key === "ArrowLeft" ? -1 : 1));
      if (input.max) {
        var mparts = input.max.split("-").map(Number);
        var maxDate = new Date(mparts[0], mparts[1] - 1, mparts[2]);
        if (current > maxDate) return;
      }
      var y = current.getFullYear();
      var m = String(current.getMonth() + 1).padStart(2, "0");
      var d = String(current.getDate()).padStart(2, "0");
      var iso = y + "-" + m + "-" + d;
      var url = new URL(window.location.href);
      url.searchParams.set("date", iso);
      window.location.href = url.toString();
    });
  }

  // ------------------------------------------------- chart tooltip module --
  // Binds to every .chart-interactive SVG on the page (any page - this
  // module doesn't know or care that it's only ever loaded on the
  // Dashboard today). Reads series names/colours from the SAME
  // .chart-legend-item elements charts.py already renders next to the
  // SVG, and per-index values from each <rect class="chart-hit"> - no
  // separate data channel, so the tooltip can never drift from what the
  // chart itself is showing.
  function initCharts() {
    var svgs = document.querySelectorAll(".chart-interactive");
    if (!svgs.length) return;

    var tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    tooltip.setAttribute("role", "status");
    tooltip.setAttribute("aria-live", "polite");
    tooltip.hidden = true;
    document.body.appendChild(tooltip);

    // Same rule as formatting.py's format_number() on the server side:
    // comma thousands separators always, but a whole number never shows
    // a trailing ".00" - only genuinely fractional values get 2dp.
    function fmt(v) {
      var n = parseFloat(v);
      if (isNaN(n)) return v;
      var rounded = Math.round(n * 100) / 100;
      var decimals = rounded === Math.trunc(rounded) ? 0 : 2;
      // en-US, not en-IN: the app's own number rule (formatting.py) groups
      // every 3 digits (1,789,359), not the Indian 2-2-3 grouping en-IN
      // would produce (17,89,359) - this must match the server-rendered
      // figures right next to it on the same page.
      return rounded.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }

    svgs.forEach(function (svg) {
      var root = svg.closest(".chart-wrap") || svg.parentNode;
      var legendItems = Array.from(root.querySelectorAll(".chart-legend-item"));
      var hitRects = Array.from(svg.querySelectorAll(".chart-hit"));
      var crosshair = svg.querySelector(".chart-crosshair");
      if (!hitRects.length) return;

      var currentIndex = null;

      function showAt(index, clientX, clientY) {
        index = Math.max(0, Math.min(hitRects.length - 1, index));
        currentIndex = index;
        var rect = hitRects[index];
        var values = (rect.dataset.values || "").split("|");
        var label = rect.dataset.label || "";

        var rows = [];
        legendItems.forEach(function (item, i) {
          if (item.style.display === "none") return; // hidden ghost/alt series
          var name = item.dataset.name || "";
          var color = item.dataset.color || "var(--accent)";
          var val = values[i] !== undefined ? values[i] : "";
          rows.push(
            '<div class="chart-tooltip-row"><span class="chart-tooltip-dot" style="background:' +
              color + '"></span><span class="chart-tooltip-name">' + name +
              '</span><span class="chart-tooltip-value">Rs ' + fmt(val) + "</span></div>"
          );
        });
        tooltip.innerHTML = '<div class="chart-tooltip-label">' + label + "</div>" + rows.join("");
        tooltip.hidden = false;

        var box = rect.getBoundingClientRect();
        var x = clientX !== undefined ? clientX : box.left + box.width / 2;
        var y = clientY !== undefined ? clientY : box.top;
        var ttRect = tooltip.getBoundingClientRect();
        var left = x + 14;
        var top = y - ttRect.height - 14;
        if (left + ttRect.width > window.innerWidth - 8) left = window.innerWidth - ttRect.width - 8;
        if (left < 8) left = 8;
        if (top < 8) top = y + 14;
        tooltip.style.left = Math.round(left + window.scrollX) + "px";
        tooltip.style.top = Math.round(top + window.scrollY) + "px";

        if (crosshair) {
          var xPos = rect.dataset.x;
          crosshair.setAttribute("x1", xPos);
          crosshair.setAttribute("x2", xPos);
          crosshair.classList.add("is-active");
        }
      }

      function hide() {
        tooltip.hidden = true;
        currentIndex = null;
        if (crosshair) crosshair.classList.remove("is-active");
      }

      hitRects.forEach(function (rect, i) {
        rect.addEventListener("pointerenter", function (e) {
          showAt(i, e.clientX, e.clientY);
        });
        rect.addEventListener("pointermove", function (e) {
          showAt(i, e.clientX, e.clientY);
        });
        rect.addEventListener("click", function (e) {
          // Also catches touch taps (spec: "a tap should show the tooltip
          // too") since a tap without drag resolves to a click event.
          showAt(i, e.clientX, e.clientY);
        });
      });

      svg.addEventListener("pointerleave", hide);
      svg.addEventListener("blur", hide);
      svg.addEventListener("focus", function () {
        showAt(currentIndex === null ? hitRects.length - 1 : currentIndex);
      });

      svg.addEventListener("keydown", function (e) {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        // Stop this from also reaching the document-level date-stepper
        // listener (initDateStepping) - a focused chart's arrow keys move
        // the chart's own readout, never the page's selected date.
        e.stopPropagation();
        var idx = currentIndex === null ? hitRects.length - 1 : currentIndex;
        idx += e.key === "ArrowRight" ? 1 : -1;
        showAt(idx);
      });
    });
  }

  // ------------------------------------------------------- unit toggle --
  function initUnitToggle() {
    var KEY = "pk-dash-fuel-unit-" + userId;
    document.querySelectorAll(".unit-toggle").forEach(function (group) {
      var card = group.closest(".dash-card");
      if (!card) return;
      var views = Array.from(card.querySelectorAll("[data-unit-view]"));
      var buttons = Array.from(group.querySelectorAll(".unit-toggle-btn"));
      if (!views.length || !buttons.length) return;

      function applyUnit(unit) {
        views.forEach(function (v) {
          v.style.display = v.dataset.unitView === unit ? "" : "none";
        });
        buttons.forEach(function (b) {
          b.setAttribute("aria-pressed", b.dataset.unit === unit ? "true" : "false");
        });
      }

      var saved = null;
      try {
        saved = localStorage.getItem(KEY);
      } catch (e) {
        saved = null;
      }
      if (saved === "liters" || saved === "rupees") applyUnit(saved);

      buttons.forEach(function (b) {
        b.addEventListener("click", function () {
          applyUnit(b.dataset.unit);
          try {
            localStorage.setItem(KEY, b.dataset.unit);
          } catch (e) {
            /* localStorage unavailable - toggle still works for this view */
          }
        });
      });
    });
  }

  // --------------------------------------------- compare-to-previous --
  function initCompareToggle() {
    var checkbox = document.getElementById("compare-toggle");
    if (!checkbox) return;
    var card = checkbox.closest(".dash-card");
    var scope = (card && card.querySelector('[data-chart-role="profit"]')) || document;
    checkbox.addEventListener("change", function () {
      var show = checkbox.checked;
      scope.querySelectorAll(".chart-line-ghost, .chart-legend-ghost").forEach(function (el) {
        el.style.display = show ? "" : "none";
      });
    });
  }

  // --------------------------------------------------------------- init --
  var collapsedApi = initCollapse();
  var reorderApi = initReorder();

  var resetBtn = document.getElementById("dash-reset-layout");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      if (reorderApi) reorderApi.reset();
      if (collapsedApi) {
        localStorage.removeItem(collapsedApi.KEY);
        var slugs = Array.from(collapsedApi.collapsedSet);
        collapsedApi.collapsedSet.clear();
        slugs.forEach(function (slug) {
          collapsedApi.apply(slug, false);
        });
      }
    });
  }

  initDateStepping();
  initCharts();
  initUnitToggle();
  initCompareToggle();
})();
