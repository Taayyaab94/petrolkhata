/* =========================================================================
   Ledger: save without a full page reload.

   THE DESIGN, in one paragraph, because it is the whole point.

   Not one of the ~26 ledger POST routes knows this file exists. They still
   validate, still flash(), still redirect back to /ledger. What changes is
   only who does the navigating: instead of letting the browser replace the
   document, we POST the form ourselves, then re-GET the page we are already
   on and graft the freshly-rendered DISPLAY regions into the live DOM. The
   server therefore stays the single source of truth - every summary figure,
   every handover variance, every feed row is rendered by exactly the code a
   real reload would have run - so "the dependent figures update correctly"
   is true by construction, not because some JS here re-does the arithmetic.

   It is a progressive enhancement, top to bottom. Turn JavaScript off and
   every form on the page posts and redirects the way it always did.
   ========================================================================= */
(function () {
  "use strict";

  /* Regions we are allowed to replace. Marked in templates/ledger.html with
     data-live-region="<name>"; every one of them is display-only, or (feed /
     handover) holds only collapsed editors, which are protected two ways
     below: a region the user is focused inside is left alone entirely, and
     any open editor in a region that IS swapped is snapshotted and typed
     back in afterwards. Form regions are deliberately NOT marked - swapping
     one would wipe whatever the user is halfway through entering. */
  var REGION_ATTR = "data-live-region";

  /* key -> that form's status element. Keyed by a STRING, not by the form
     node: a form inside a live region is a different node after every swap,
     so a WeakMap on the element would leak one stale error message per
     save. editorKey() is stable across swaps because it is built from the
     action and the hidden fields, which the server re-renders identically. */
  var statusFor = new Map();
  var formSeq = 0;

  function statusKey(form) {
    if (editorContainer(form)) { return editorKey(form); }
    if (!form.dataset.ajaxFormKey) { form.dataset.ajaxFormKey = "form-" + (++formSeq); }
    return form.dataset.ajaxFormKey;
  }

  /* -------------------------------------------------------------------
     Status indicator shown next to the submit button that was pressed.
     ------------------------------------------------------------------- */

  function toastHost() {
    var host = document.getElementById("ledger-ajax-toasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "ledger-ajax-toasts";
      document.body.appendChild(host);
    }
    return host;
  }

  function makeStatus(button) {
    /* One status element per form, reused: a second save replaces the first
       message rather than stacking up a column of them. */
    var el = document.createElement("span");
    el.className = "ajax-status ajax-status-pending";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.textContent = "Saving...";
    button.insertAdjacentElement("afterend", el);
    /* In a .form-grid the button is a grid item, so a bare sibling would be
       dropped into the next cell and read as an orphan. Span the row. */
    var parent = el.parentElement;
    if (parent && /grid/.test(getComputedStyle(parent).display)) {
      el.style.gridColumn = "1 / -1";
    }
    return el;
  }

  /* After a swap the submitted form is a new node. If it is still on the
     page (a failed editor, restored with the user's values), put the message
     back beside its submit button where they are looking, rather than
     letting it drift off to the toast corner. */
  function reanchor(el, key) {
    if (!key || document.body.contains(el)) { return; }
    var target = null;
    Array.prototype.forEach.call(document.querySelectorAll("form"), function (form) {
      if (!target && editorContainer(form) && editorKey(form) === key) { target = form; }
    });
    if (!target) { return; }
    var button = target.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    if (button) { button.insertAdjacentElement("afterend", el); }
  }

  function settleStatus(el, kind, message) {
    /* If the form that was submitted lived inside a region we just swapped,
       the status element went out with it. Re-home it as a floating toast
       so a Delete or an inline Edit still reports what happened. */
    if (!document.body.contains(el)) {
      el.style.gridColumn = "";
      toastHost().appendChild(el);
    }
    el.className = "ajax-status ajax-status-" + kind;
    el.textContent = message;
    if (kind === "ok") {
      /* Success fades; an error must not, or the user loses the reason the
         entry did not save while they are still reading the form. */
      window.setTimeout(function () {
        el.classList.add("ajax-status-fade");
        window.setTimeout(function () {
          if (el.parentElement) { el.parentElement.removeChild(el); }
        }, 600);
      }, 3500);
    }
  }

  /* -------------------------------------------------------------------
     Flash messages.

     WHICH RESPONSE CARRIES THE FLASH: the first one - the POST.

     Flask flashes are one-shot: whichever request renders them pops them
     out of the session. Every ledger route flashes and then 302s back to
     /ledger, and we fetch with redirect:"follow", so fetch #1 transparently
     follows that redirect and the body we get back is an already-rendered
     ledger page WITH the flash markup in it. By the time the second fetch
     (the one we use for the fresh regions) runs, the session no longer has
     any messages left to render. So: read the outcome from response #1,
     take the regions from response #2.

     The fallback to doc2 below is belt-and-braces only, for the case of a
     route that ever renders in place instead of redirecting.
     ------------------------------------------------------------------- */
  function readFlashes(doc) {
    if (!doc) { return { ok: null, message: "" }; }
    var errors = doc.querySelectorAll(".flash-list .flash-error");
    if (errors.length) {
      return { ok: false, message: text(errors[0]) };
    }
    var successes = doc.querySelectorAll(".flash-list .flash-success");
    if (successes.length) {
      return { ok: true, message: text(successes[0]) };
    }
    return { ok: null, message: "" };
  }

  function text(node) {
    return (node.textContent || "").replace(/\s+/g, " ").trim();
  }

  function parse(html) {
    return new DOMParser().parseFromString(html, "text/html");
  }

  /* -------------------------------------------------------------------
     Region swapping.
     ------------------------------------------------------------------- */

  /* An open editor inside a live region is the one thing a swap can destroy
     that the server cannot give back: the user's half-typed correction. So
     before replacing a region we photograph every editor that is currently
     open in it, and afterwards we re-open the same editors in the fresh
     markup and type the values back in.

     Editors are identified by their action plus their hidden fields, which
     is what actually distinguishes them - the per-shift handover forms all
     post to the same /ledger/handover and differ only by shift_id. */
  function editorKey(form) {
    var bits = [formAction(form)];
    Array.prototype.forEach.call(form.querySelectorAll('input[type="hidden"]'), function (el) {
      if (el.name === "csrf_token") { return; }
      bits.push(el.name + "=" + el.value);
    });
    return bits.join("|");
  }

  function editorContainer(form) {
    return form.closest(".feed-edit-panel") || form.closest("tr.edit-row");
  }

  function isEditorOpen(form) {
    var box = editorContainer(form);
    if (!box) { return false; }
    if (box.classList.contains("feed-edit-panel")) { return !box.hidden; }
    return box.style.display !== "none";
  }

  function snapshotEditors(region) {
    var shots = [];
    Array.prototype.forEach.call(region.querySelectorAll("form"), function (form) {
      if (!isEditorOpen(form)) { return; }
      var values = [];
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el.name || el.name === "csrf_token" || el.type === "hidden") { return; }
        if (el.type === "checkbox" || el.type === "radio") {
          values.push({ name: el.name, value: el.value, checked: el.checked, toggle: true });
        } else {
          values.push({ name: el.name, value: el.value, toggle: false });
        }
      });
      shots.push({ key: editorKey(form), values: values });
    });
    return shots;
  }

  function openEditor(form) {
    var box = editorContainer(form);
    if (!box) { return; }
    if (box.classList.contains("feed-edit-panel")) {
      box.hidden = false;
      var toggle = document.querySelector('[data-edit-target="' + box.id + '"]');
      if (toggle) { toggle.setAttribute("aria-expanded", "true"); }
    } else {
      box.style.display = "table-row";
    }
  }

  function restoreEditors(region, shots, skipKey) {
    shots.forEach(function (shot) {
      if (skipKey && shot.key === skipKey) { return; }
      var target = null;
      Array.prototype.forEach.call(region.querySelectorAll("form"), function (form) {
        if (!target && editorKey(form) === shot.key) { target = form; }
      });
      if (!target) { return; }
      openEditor(target);
      shot.values.forEach(function (v) {
        Array.prototype.forEach.call(target.elements, function (el) {
          if (el.name !== v.name) { return; }
          if (v.toggle) {
            if (el.value === v.value) { el.checked = v.checked; }
          } else if (!v.toggle && el.type !== "checkbox" && el.type !== "radio") {
            el.value = v.value;
          }
        });
      });
    });
  }

  function swapRegions(freshDoc, submittedForm, activeAtSubmit, skipKey) {
    var swapped = [];
    var live = document.querySelectorAll("[" + REGION_ATTR + "]");
    Array.prototype.forEach.call(live, function (node) {
      var name = node.getAttribute(REGION_ATTR);
      var fresh = freshDoc.querySelector(
        "[" + REGION_ATTR + '="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]'
      );
      /* Missing on either side: skip rather than throw. A half-swapped page
         is still a correct page; a thrown exception would strand the submit
         button disabled. */
      if (!fresh) { return; }

      /* The typing guard. A region is safe to replace unless the user is
         focused inside it - EXCEPT when it contains the very form they just
         submitted, in which case they are done typing there and the whole
         point is to show them the result. */
      var holdsSubmitted = submittedForm && node.contains(submittedForm);
      if (!holdsSubmitted) {
        if (activeAtSubmit && node.contains(activeAtSubmit)) { return; }
        var now = document.activeElement;
        if (now && now !== document.body && node.contains(now)) { return; }
      }

      var shots = snapshotEditors(node);
      var replacement = document.importNode(fresh, true);
      node.parentNode.replaceChild(replacement, node);
      restoreEditors(replacement, shots, skipKey);
      swapped.push(replacement);
    });
    return swapped;
  }

  function highlight(nodes) {
    nodes.forEach(function (node) {
      if (node.getAttribute(REGION_ATTR) !== "feed") { return; }
      node.classList.add("region-updated");
      window.setTimeout(function () { node.classList.remove("region-updated"); }, 1400);
    });
  }

  /* -------------------------------------------------------------------
     Which submits we take over.
     ------------------------------------------------------------------- */

  /* NEVER read form.method / form.action / form.reset as properties.
     HTMLFormElement has [LegacyOverrideBuiltIns], so a control called
     "method" shadows the property with the control itself - and several
     forms on this page do exactly that (the cash/bank/credit selector on
     sales-return, product-sale and other-income is <select name="method">).
     Reading form.method there yields an element, not the string "post",
     and the whole submit handler would throw. Go through the attribute and
     the prototype instead. */
  function formMethod(form) {
    return (form.getAttribute("method") || "get").toLowerCase();
  }

  function formAction(form) {
    var raw = form.getAttribute("action");
    return raw ? new URL(raw, document.baseURI).href : window.location.href;
  }

  function resetForm(form) {
    HTMLFormElement.prototype.reset.call(form);
  }

  function shouldSkip(form) {
    if (formMethod(form) !== "post") { return true; }                     // the date picker
    if (form.hasAttribute("data-no-ajax")) { return true; }
    if (form.querySelector('input[type="file"]')) { return true; }        // needs a real post
    var url;
    try { url = new URL(formAction(form), window.location.href); } catch (err) { return true; }
    if (url.origin !== window.location.origin) { return true; }
    /* Anything that hands back a file or walks the user off the ledger has
       to stay a real navigation - there is nothing here for us to swap. */
    if (/\/(export|download|print)/.test(url.pathname)) { return true; }
    return false;
  }

  /* -------------------------------------------------------------------
     The one delegated submit listener.
     ------------------------------------------------------------------- */

  document.addEventListener("submit", function (event) {
    /* CRITICAL, and the reason the inline confirm()s keep working: an
       inline onsubmit="return confirm(...)" that returns false cancels the
       default action but does NOT stop the event bubbling up to document.
       Without this check a cancelled "Delete this?" would still be posted
       by us. defaultPrevented is exactly the signal that something upstream
       already said no. */
    if (event.defaultPrevented) { return; }

    var form = event.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    if (shouldSkip(form)) { return; }

    event.preventDefault();

    var button = event.submitter ||
      form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    if (!button) { return; }

    var activeAtSubmit = document.activeElement;
    var key = statusKey(form);
    /* Clear this form's previous result (a persisted error, typically) so
       the user never reads a stale message next to a fresh attempt. */
    var previous = statusFor.get(key) || form.querySelector(".ajax-status");
    if (previous && previous.parentElement) { previous.parentElement.removeChild(previous); }

    var status = makeStatus(button);
    statusFor.set(key, status);
    var label = button.textContent;
    button.disabled = true;
    if (button.tagName === "BUTTON") { button.textContent = "Saving..."; }

    function restore() {
      button.disabled = false;
      if (button.tagName === "BUTTON") { button.textContent = label; }
    }

    var body = new FormData(form);
    /* A submit button with a name/value is part of the submission; FormData
       does not include it, so put it back. */
    if (button.name) { body.append(button.name, button.value); }

    fetch(formAction(form), {
      method: "POST",
      body: body,
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        /* Which ledger view we are on. The server points the save's
           redirect here (see _ajax_save_lands_on_the_viewed_page in
           app.py) so the response we follow is already the page we want
           to graft in - otherwise the handler would send us to the
           entry's own date with no ?shift= and we would have to fetch
           this page separately, rendering the whole ledger twice. */
        "X-Ledger-View": window.location.search
      },
      redirect: "follow",
      credentials: "same-origin"
    }).then(function (response) {
      return response.text().then(function (html) {
        return { response: response, doc: parse(html) };
      });
    }).then(function (posted) {
      var outcome = readFlashes(posted.doc);
      var failed = outcome.ok === false;

      if (outcome.ok === null && !posted.response.ok) {
        settleStatus(status, "err", "Couldn't save - the server rejected that (HTTP " + posted.response.status + ").");
        restore();
        return;
      }

      /* Re-GET the page we are actually looking at - not the POST's redirect
         target, which may have dropped the ?shift= we are viewing - and take
         the regions from that.

         This runs on an ERROR too, deliberately. "flash-error" does not
         reliably mean "nothing was written": /ledger/handover saves the
         count and THEN flashes the variance as an error, so treating a red
         flash as "no change" would leave the handover row reading
         "Not reconciled" until the next reload. Re-rendering from the server
         is the only answer that is right for both kinds of error - and the
         user's typing survives it, because swapRegions photographs open
         editors and types them back in (skipKey below keeps the failed
         form's own values instead of the server's).

         skipKey means "let the server's version of this editor stand".
         On success that is the submitted form (it saved; show what saved
         and let it close). On failure it is nothing, so the submitted
         form's snapshot is put back and the typed values survive. */
      var skipKey = failed || !editorContainer(form) ? null : editorKey(form);

      /* The POST's own response IS the page we want. fetch followed the
         redirect, and the server aimed that redirect at the view we told
         it we are on, so this document is exactly what a separate GET of
         window.location.href would have returned - for one render of the
         ledger instead of two. */
      var fresh = posted.doc;
      /* On failure the submitted form must keep exactly what the user
         typed, so it is snapshotted and restored like any other open
         editor; on success it is left to render fresh from the server. */
      var swapped = swapRegions(fresh, form, activeAtSubmit, skipKey);
      if (!failed) { highlight(swapped); }

      if (failed) {
        reanchor(status, editorContainer(form) ? key : null);
        settleStatus(status, "err", outcome.message || "Couldn't save.");
      } else {
        /* Only on success does the form get cleared, so the next entry
           starts from a clean slate. reset() restores the server-rendered
           defaults, which is right for the prefilled forms (dip, edit). */
        if (document.body.contains(form)) { resetForm(form); }
        var message = outcome.message || readFlashes(fresh).message;
        /* The server's own wording ("Logged expense: ...", "Deleted
           expense.") is better than anything generic we could prefix it
           with; the checkmark already says it worked. */
        settleStatus(status, "ok", message || "Saved");
      }
      restore();
    }).catch(function (err) {
      settleStatus(status, "err", "Couldn't save - check your connection.");
      restore();
      if (window.console) { console.warn("[ledger-ajax]", err); }
    });
  });
})();
