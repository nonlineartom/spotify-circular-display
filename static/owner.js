/* Owner controls. Credentials and pairing links stay in this page's memory. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let overview = null;
  let diagnosticState = null;
  let lightingSaved = null;
  let lightingDraft = [];
  let lightingDirty = false;
  let lightingBusy = false;
  let discoveryDevices = [];
  let discoveryTimer = null;
  let refreshTimer = null;
  let refreshing = false;
  let authorised = false;
  let pairingExpires = 0;
  let pairingBusy = false;
  let pairingProfileEpoch = null;
  let disconnectAccountId = null;
  let pairingTimer = null;
  let toastTimer = null;
  let deviceSequence = 0;
  let editingInput = null;
  let touchKeyboard = null;
  let restoringInputFocus = false;
  let keyboardBackgroundState = [];

  function text(element, value) { element.textContent = value; }
  function message(id, value) {
    text($(id), value || "");
    $(id).hidden = !value;
  }
  function toast(value) {
    clearTimeout(toastTimer);
    message("toast", value);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }
  function errorMessage(error) {
    return error && error.message ? error.message : "The display could not be reached. Please try again.";
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        credentials: "same-origin", cache: "no-store", ...options,
        headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
        signal: controller.signal,
      });
      let data = null;
      if (response.status !== 204) {
        try { data = await response.json(); } catch (_) { /* Handle non-JSON failure responses below. */ }
      }
      if (!response.ok) {
        const error = new Error(data && data.error || `The request failed (${response.status}). Please try again.`);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The display took too long to respond. Please try again.");
      if (error instanceof TypeError) throw new Error("The display could not be reached. Check your connection and try again.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function post(path, body = {}) { return api(path, { method: "POST", body: JSON.stringify(body) }); }
  function safeWebURL(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, location.origin);
      return /^https?:$/.test(url.protocol) && !url.username && !url.password ? url : null;
    } catch (_) { return null; }
  }
  function dateLabel(timestamp) {
    const value = Number(timestamp);
    if (!Number.isFinite(value) || value <= 0) return "";
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }
  function durationLabel(seconds) {
    if (!Number.isFinite(Number(seconds))) return "Unavailable";
    const minutes = Math.floor(Math.max(0, Number(seconds)) / 60);
    if (minutes < 1) return "Just started";
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return hours < 24 ? `${hours} hr ${minutes % 60} min` : `${Math.floor(hours / 24)} days ${hours % 24} hr`;
  }
  function setLogin(required) {
    authorised = !required;
    $("loading").hidden = true;
    $("owner-login").hidden = !required;
    $("owner-content").hidden = required;
    if (required) {
      clearTimeout(refreshTimer);
      clearTimeout(discoveryTimer);
      clearPairing();
      disconnectAccountId = null;
      $("disconnect-confirm").hidden = true;
      closeInputEditor(false);
    }
  }
  function handleAuth(error) {
    if (error.status !== 401 && error.status !== 403) return false;
    setLogin(true);
    message("login-error", "Unlock owner settings to continue.");
    return true;
  }

  function renderAccount() {
    const account = overview.account || {};
    const oauth = overview.oauth || {};
    const connected = !!account.connected && !account.expired && !account.reauth_required;
    const guest = account.kind === "guest";
    if (pairingProfileEpoch && pairingProfileEpoch !== overview.profile_epoch) clearPairing();
    text($("account-badge"), account.reauth_required ? "Reconnect" : connected ? "Connected" : "Not connected");
    $("account-badge").classList.toggle("muted", !connected);
    text($("account-name"), account.display_name || (connected ? (guest ? "A guest is on the decks" : "Your household library") : "Bring your music along"));
    text($("account-detail"), account.reauth_required ? "Reconnect this Spotify profile to browse its library" : connected ? (guest ? "Guest library · expires automatically" : "Household library · remembered on this display") : account.expired ? "This guest library has expired" : "Playlists, saved albums and new discoveries");
    text($("account-guidance"), "The shelf follows the Spotify account playing on this display. To link another household member or a guest, first play on Pi Display from their account, then create a phone link below.");
    const expiry = dateLabel(account.expires_at);
    const renewal = dateLabel(account.reauthorize_at);
    message("account-expiry", account.reauth_required ? "Spotify needs you to sign in again. Create a household or guest link to reconnect this profile." : connected && guest && expiry ? `Guest access ends ${expiry}.` : connected && renewal ? `Spotify sign-in will need renewing ${renewal}.` : "");
    $("disconnect-show").hidden = !account.account_id;
    text($("pairing-create"), account.reauth_required ? "Reconnect household library" : "Link household library");
    text($("pairing-guest"), "Invite a guest");
    $("pairing-create").disabled = pairingBusy || !oauth.pairing_ready;
    $("pairing-guest").disabled = pairingBusy || !oauth.pairing_ready;
    const login = oauth.login_url ? safeWebURL(oauth.login_url) : null;
    // Owner cookies and APIs are scoped to this origin. The display uses phone
    // pairing; a public reverse proxy need not expose owner administration.
    const directLogin = !!login && login.origin === location.origin && oauth.ready && !overview.local_kiosk;
    $("connect-account").hidden = !directLogin || connected;
    if (directLogin) $("connect-account").href = login.href;
    $("pairing-create").classList.toggle("button-primary", !directLogin || connected);
    $("pairing-create").classList.toggle("button-quiet", directLogin && !connected);
    const oauthError = oauth.reason || oauth.error;
    message("oauth-notice", !oauth.pairing_ready ? (oauthError || "Phone pairing needs the display’s public Spotify connection address configured during setup.") : "");
    if (oauth.pairing_ready) {
      const count = Number(account.profile_count);
      message("oauth-notice", `Household libraries stay linked. Guest links create expiring access.${Number.isInteger(count) && count > 0 ? ` ${count} ${count === 1 ? "profile is" : "profiles are"} remembered on this display.` : ""}`);
    }
    text($("access-note"), overview.local_kiosk ? "Owner access · this display" : "Owner access · this browser");
    document.body.classList.toggle("kiosk", !!overview.local_kiosk);
  }

  function healthRow(list, label, value, state = "") {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    text(term, label);
    text(detail, value);
    detail.className = state;
    row.append(term, detail);
    list.appendChild(row);
  }
  function renderHealth() {
    const health = overview && overview.health || {};
    const receiver = health.receiver || {};
    const config = health.configuration || {};
    const lighting = health.lighting || {};
    const list = $("health-list");
    list.replaceChildren();
    $("health-summary").classList.toggle("attention", !health.ok);
    text($("health-summary-text"), health.summary || (health.ok ? "Ready for the next record" : "Your display needs attention"));
    healthRow(list, "Spotify receiver", receiver.label || (receiver.available ? "Ready" : "Unavailable"), receiver.available ? "good" : "attention");
    healthRow(list, "Configuration", config.label || (config.ok ? "Ready" : "Needs attention"), config.ok ? "good" : "attention");
    healthRow(list, "Lights", lighting.label || (lighting.enabled ? `${lighting.configured_devices || 0} synced` : "Sync off"));
    healthRow(list, "Running for", durationLabel(health.uptime_seconds));
    const details = $("health-details-list");
    details.replaceChildren();
    if (diagnosticState) {
      const system = diagnosticState.system || {};
      const crate = diagnosticState.crate || {};
      const runtime = diagnosticState.wled && diagnosticState.wled.runtime || {};
      const temperature = Number(system.cpu_temperature_c);
      healthRow(details, "CPU temperature", system.cpu_temperature_c == null || !Number.isFinite(temperature) ? "Unavailable on this device" : `${temperature.toFixed(1)} °C`);
      healthRow(details, "Free storage", Number.isFinite(Number(system.disk_free_bytes)) && system.disk_free_bytes != null ? `${(Number(system.disk_free_bytes) / 1073741824).toFixed(1)} GB` : "Unavailable");
      healthRow(details, "Record shelf", crate.building ? "Refreshing" : crate.age_seconds == null ? "Waiting for first refresh" : "Ready");
      healthRow(details, "Live display updates", `${diagnosticState.events && diagnosticState.events.clients || 0} connected`);
      if (lighting.enabled) healthRow(details, "Lighting service", runtime.ok ? (runtime.rendering ? "Following the music" : "Ready") : runtime.stale ? "Status is out of date" : "Status unavailable", runtime.ok ? "good" : "attention");
      const lyrics = diagnosticState.lyrics || {};
      healthRow(details, "Lyrics", lyrics.circuit_open ? "Waiting before retrying" : "Ready");
    } else {
      healthRow(details, "More details", "Temporarily unavailable");
    }
    text($("health-updated"), `Updated ${new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`);
  }

  async function refreshOverview(initial = false) {
    if (refreshing) return;
    refreshing = true;
    $("health-refresh").disabled = true;
    try {
      overview = await api("/api/owner/overview");
      setLogin(false);
      message("page-notice", "");
      renderAccount();
      renderHealth();
      const results = await Promise.allSettled([
        api("/api/diagnostics"),
        initial || !lightingSaved ? loadLighting() : Promise.resolve(),
      ]);
      if (results[0].status === "fulfilled") {
        diagnosticState = results[0].value;
        message("health-notice", "");
      } else if (!handleAuth(results[0].reason)) {
        message("health-notice", "Some extra health details are unavailable. Refresh to try again.");
      }
      renderHealth();
    } catch (error) {
      $("loading").hidden = true;
      if (!handleAuth(error)) {
        const notice = $("page-notice");
        message("page-notice", errorMessage(error));
        const retry = document.createElement("button");
        retry.className = "button button-quiet";
        retry.type = "button";
        text(retry, "Try again");
        retry.addEventListener("click", () => refreshOverview(!overview));
        notice.appendChild(retry);
      }
    } finally {
      refreshing = false;
      $("health-refresh").disabled = false;
      scheduleRefresh();
    }
  }
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (authorised && !document.hidden) refreshTimer = setTimeout(() => refreshOverview(), 30000);
  }

  function drawPairingQR(value) {
    const root = $("pairing-qr");
    root.replaceChildren();
    if (typeof window.qrcode !== "function") {
      text(root, "QR unavailable. Use the pairing link below.");
      return;
    }
    try {
      const code = window.qrcode(0, "M");
      code.addData(value, "Byte");
      code.make();
      const count = code.getModuleCount();
      const size = count + 8;
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("shape-rendering", "crispEdges");
      const background = document.createElementNS(svg.namespaceURI, "rect");
      background.setAttribute("width", String(size));
      background.setAttribute("height", String(size));
      background.setAttribute("fill", "#fff");
      const path = document.createElementNS(svg.namespaceURI, "path");
      let cells = "";
      for (let row = 0; row < count; row++) for (let col = 0; col < count; col++) {
        if (code.isDark(row, col)) cells += `M${col + 4},${row + 4}h1v1h-1z`;
      }
      path.setAttribute("d", cells);
      path.setAttribute("fill", "#000");
      svg.append(background, path);
      root.appendChild(svg);
    } catch (_) { text(root, "QR unavailable. Use the pairing link below."); }
  }
  function clearPairing() {
    clearInterval(pairingTimer);
    pairingTimer = null;
    pairingExpires = 0;
    pairingProfileEpoch = null;
    $("pairing-url").value = "";
    $("pairing-open").removeAttribute("href");
    $("pairing-qr").replaceChildren();
    $("pairing-panel").hidden = true;
  }
  function updatePairingExpiry() {
    const seconds = Math.max(0, Math.ceil((pairingExpires - Date.now()) / 1000));
    if (!seconds) {
      clearInterval(pairingTimer);
      pairingTimer = null;
      $("pairing-panel").classList.add("expired");
      text($("pairing-qr"), "This pairing code has expired.");
      text($("pairing-expiry"), "Create a fresh pairing link to connect.");
      $("pairing-url").value = "";
      $("pairing-url").hidden = true;
      $("pairing-open").removeAttribute("href");
      $("pairing-open").hidden = true;
      $("pairing-copy").disabled = true;
      return;
    }
    text($("pairing-expiry"), `One use · expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`);
  }
  async function createPairing(profileKind = "household") {
    if (pairingBusy) return;
    if (!["household", "guest"].includes(profileKind)) return;
    pairingBusy = true;
    clearPairing();
    $("pairing-create").disabled = true;
    $("pairing-guest").disabled = true;
    const button = $(profileKind === "guest" ? "pairing-guest" : "pairing-create");
    button.disabled = true;
    text(button, "Creating link…");
    try {
      const expectedEpoch = overview && overview.profile_epoch;
      if (typeof expectedEpoch !== "string" || !expectedEpoch) throw new Error("Refresh settings before creating a pairing link.");
      const pairing = await post("/api/auth/pairing", { profile_kind: profileKind, profile_epoch: expectedEpoch });
      const url = pairing && safeWebURL(pairing.join_url);
      if (!url || !Number.isFinite(Number(pairing.expires_in)) || Number(pairing.expires_in) <= 0) throw new Error("The display did not return a valid pairing link. Please try again.");
      if (pairing.profile_kind !== profileKind || pairing.profile_epoch !== expectedEpoch || overview.profile_epoch !== expectedEpoch) throw new Error("The Spotify account changed. Refresh settings, then create a fresh pairing link.");
      clearPairing();
      pairingProfileEpoch = pairing.profile_epoch;
      text($("pairing-title"), profileKind === "guest" ? "Invite a guest for this session." : "Link your household library.");
      text($("pairing-description"), profileKind === "guest" ? "Scan with a phone and sign in to the Spotify account currently playing on Pi Display. This creates expiring guest access for that profile; other household libraries stay linked." : "Scan with a phone and sign in to the Spotify account currently playing on Pi Display. This household library will be remembered for that account, without a local expiry.");
      $("pairing-panel").classList.remove("expired");
      $("pairing-panel").hidden = false;
      $("pairing-url").hidden = false;
      $("pairing-url").value = url.href;
      $("pairing-open").href = url.href;
      $("pairing-open").hidden = false;
      $("pairing-copy").disabled = false;
      pairingExpires = Date.now() + Number(pairing.expires_in) * 1000;
      drawPairingQR(url.href);
      updatePairingExpiry();
      pairingTimer = setInterval(updatePairingExpiry, 1000);
      $("pairing-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
      toast("Pairing link ready. Scan it with your phone.");
    } catch (error) {
      if (!handleAuth(error)) toast(errorMessage(error));
    } finally {
      pairingBusy = false;
      if (overview) renderAccount();
    }
  }

  function cloneDevices(devices) {
    return (Array.isArray(devices) ? devices : []).map(device => ({ ...device, _key: ++deviceSequence }));
  }
  function collectDevices() {
    return lightingDraft.map(device => {
      const card = $("lighting-devices").querySelector(`[data-device-key="${device._key}"]`);
      const value = name => card.querySelector(`[name="${name}"]`).value;
      return {
        host: value("host").trim(), name: value("name").trim(), pixel_count: Number(value("pixel_count")),
        reverse: card.querySelector('[name="reverse"]').checked,
        phase_offset: Number(value("phase_offset")), brightness: Number(value("brightness")) / 100,
        gamma: Number(value("gamma")),
      };
    });
  }
  function snapshotDraft() {
    const values = collectDevices();
    lightingDraft = values.map((value, index) => ({ ...value, _key: lightingDraft[index]._key }));
  }
  function markLightingDirty() {
    lightingDirty = true;
    $("lighting-save").disabled = false;
    $("lighting-revert").disabled = false;
    $("lighting-save-status").classList.add("unsaved");
    text($("lighting-save-status"), "Unsaved changes. Save to apply them to your lights.");
  }
  function setLightingClean() {
    lightingDirty = false;
    $("lighting-save").disabled = true;
    $("lighting-revert").disabled = true;
    $("lighting-save-status").classList.remove("unsaved");
    text($("lighting-save-status"), "Your settings are up to date.");
  }
  function createField(device, name, label, options = {}) {
    const wrap = document.createElement("div");
    wrap.className = `field${options.full ? " full-width" : ""}`;
    const title = document.createElement("label");
    const input = document.createElement("input");
    input.id = `light-${device._key}-${name}`;
    title.htmlFor = input.id;
    text(title, label);
    input.name = name;
    input.type = options.type || "text";
    input.required = true;
    if (options.min != null) input.min = options.min;
    if (options.max != null) input.max = options.max;
    if (options.step != null) input.step = options.step;
    if (options.maxLength) input.maxLength = options.maxLength;
    if (options.placeholder) input.placeholder = options.placeholder;
    input.value = options.value != null ? options.value : device[name] == null ? "" : device[name];
    if (name === "host") {
      input.autocapitalize = "none";
      input.spellcheck = false;
      input.pattern = "[A-Za-z0-9._\\-]+";
      input.title = "Use a local IP address or hostname, without http:// or a port.";
    }
    wrap.append(title, input);
    return wrap;
  }
  function usesTouchKeyboard() {
    // Phones use their native keyboard. The local, square display needs keys
    // inside its physical circle, even when Chromium reports a fine pointer.
    return !!(overview && overview.local_kiosk && window.createTouchKeyboard &&
      window.matchMedia("(min-width: 1000px) and (min-aspect-ratio: 9/10) and (max-aspect-ratio: 11/10)").matches);
  }
  function closeInputEditor(restoreFocus = true) {
    const original = editingInput;
    editingInput = null;
    if (touchKeyboard) touchKeyboard.close();
    $("owner-input-dialog").hidden = true;
    $("owner-input-value").value = "";
    keyboardBackgroundState.forEach(([element, inert]) => { element.inert = inert; });
    keyboardBackgroundState = [];
    if (restoreFocus && original && original.isConnected && !original.disabled) {
      restoringInputFocus = true;
      original.focus({ preventScroll: true });
      restoringInputFocus = false;
    }
  }
  function applyInputEditor() {
    if (!editingInput || !editingInput.isConnected) return closeInputEditor(false);
    const original = editingInput;
    const nextValue = $("owner-input-value").value;
    if (original.type === "number" && (!/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(nextValue.trim()) || !Number.isFinite(Number(nextValue)))) {
      message("owner-input-error", "Enter a number using digits, a decimal point or a minus sign.");
      return false;
    }
    const previousValue = original.value;
    original.value = nextValue;
    if (!original.checkValidity()) {
      const validation = original.validationMessage;
      original.value = previousValue;
      message("owner-input-error", validation || "Check this value and try again.");
      return false;
    }
    original.dispatchEvent(new Event("input", { bubbles: true }));
    original.dispatchEvent(new Event("change", { bubbles: true }));
    closeInputEditor();
    return true;
  }
  function openInputEditor(input) {
    if (restoringInputFocus || editingInput || !usesTouchKeyboard() || input.disabled || input.readOnly || !input.matches('input[type="text"], input[type="number"]')) return;
    if (!touchKeyboard) touchKeyboard = window.createTouchKeyboard({ element: $("owner-touch-keyboard"), onDone: applyInputEditor });
    editingInput = input;
    const label = input.labels && input.labels[0] ? input.labels[0].textContent : "Light setting";
    text($("owner-input-title"), label);
    text($("owner-input-label"), label);
    const numeric = input.type === "number";
    text($("owner-input-hint"), numeric ? `${input.min ? `Minimum ${input.min}. ` : ""}${input.max ? `Maximum ${input.max}. ` : ""}Tap Done to use this value.` : input.name === "host" ? "Use an IP address or hostname, without http:// or a port." : "Use the keyboard below, then tap Done.");
    const editor = $("owner-input-value");
    editor.value = input.value;
    if (input.maxLength > 0) editor.maxLength = input.maxLength;
    else editor.removeAttribute("maxlength");
    message("owner-input-error", "");
    $("owner-input-dialog").hidden = false;
    keyboardBackgroundState = [$("main"), document.querySelector(".topbar")].map(element => [element, element.inert]);
    keyboardBackgroundState.forEach(([element]) => { element.inert = true; });
    touchKeyboard.open(editor, { numeric, doneLabel: "Done" });
    editor.focus({ preventScroll: true });
    editor.setSelectionRange(0, editor.value.length);
  }
  function renderLighting() {
    const list = $("lighting-devices");
    list.replaceChildren();
    if (!lightingDraft.length) {
      const empty = document.createElement("div");
      empty.className = "empty-lights";
      const icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      text(icon, "✧");
      const copy = document.createElement("p");
      text(copy, "No lights added yet. Find WLED lights on your network, or add one by its address.");
      empty.append(icon, copy);
      list.appendChild(empty);
    }
    for (const device of lightingDraft) {
      const card = document.createElement("article");
      card.className = "device-card";
      card.dataset.deviceKey = device._key;
      const heading = document.createElement("div");
      heading.className = "device-heading";
      const title = document.createElement("h3");
      text(title, device.name || "New light");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-device";
      remove.setAttribute("aria-label", `Remove ${device.name || "new light"}`);
      text(remove, "Remove");
      remove.addEventListener("click", () => {
        snapshotDraft();
        lightingDraft = lightingDraft.filter(item => item._key !== device._key);
        renderLighting();
        markLightingDirty();
      });
      heading.append(title, remove);
      const fields = document.createElement("div");
      fields.className = "device-fields";
      fields.append(
        createField(device, "name", "Light name", { full: true, maxLength: 64, placeholder: "Behind the record player" }),
        createField(device, "host", "IP address or hostname", { maxLength: 253, placeholder: "wled.local" }),
        createField(device, "pixel_count", "LED count", { type: "number", min: 1, max: 2048, step: 1, value: device.pixel_count || 46 }),
      );
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      text(summary, "Fine-tune this light");
      const fineFields = document.createElement("div");
      fineFields.className = "device-fields";
      fineFields.append(
        createField(device, "brightness", "Brightness (%)", { type: "number", min: 5, max: 100, step: "any", value: Math.round((device.brightness == null ? 1 : device.brightness) * 10000) / 100 }),
        createField(device, "phase_offset", "Position offset (turns)", { type: "number", min: -1, max: 1, step: "any", value: device.phase_offset || 0 }),
        createField(device, "gamma", "Colour response (gamma)", { type: "number", min: 0.5, max: 3, step: "any", value: device.gamma == null ? 1 : device.gamma }),
      );
      const reverseLabel = document.createElement("label");
      reverseLabel.className = "checkbox-label";
      const reverse = document.createElement("input");
      reverse.type = "checkbox";
      reverse.name = "reverse";
      reverse.checked = !!device.reverse;
      reverseLabel.append(reverse, document.createTextNode("Reverse direction"));
      fineFields.appendChild(reverseLabel);
      const fineHint = document.createElement("p");
      fineHint.className = "hint";
      text(fineHint, "Offset 0.5 moves the pattern halfway around the strip. Gamma 1 keeps the original colour response.");
      details.append(summary, fineFields, fineHint);
      card.append(heading, fields, details);
      list.appendChild(card);
    }
    text($("lighting-count"), `${lightingDraft.length} of 16 lights`);
    $("lighting-add").disabled = lightingDraft.length >= 16;
    renderDiscovered();
  }
  async function loadLighting() {
    $("lighting-fields").disabled = true;
    $("lighting-retry").hidden = true;
    try {
      const status = await api("/api/wled/status");
      lightingSaved = status;
      lightingDraft = cloneDevices(status.devices);
      $("lighting-enabled").checked = !!status.enabled;
      renderLighting();
      setLightingClean();
      text($("lighting-badge"), status.enabled ? "Sync on" : "Sync off");
      $("lighting-badge").classList.toggle("muted", !status.enabled);
      message("lighting-error", "");
      $("lighting-fields").disabled = false;
    } catch (error) {
      if (!handleAuth(error)) message("lighting-error", errorMessage(error));
      $("lighting-retry").hidden = false;
      text($("lighting-badge"), "Unavailable");
      $("lighting-badge").classList.add("muted");
    }
  }
  function addLighting(device = {}) {
    if (lightingDraft.length >= 16) return toast("You can add up to 16 lights.");
    snapshotDraft();
    lightingDraft.push({ name: "", host: "", pixel_count: 46, brightness: 1, phase_offset: 0, gamma: 1, reverse: false, ...device, _key: ++deviceSequence });
    if (lightingDraft.length === 1) $("lighting-enabled").checked = true;
    renderLighting();
    markLightingDirty();
    const card = $("lighting-devices").lastElementChild;
    card.querySelector('input[name="name"]').focus();
  }
  function renderDiscovered() {
    const target = $("discovery-devices");
    target.replaceChildren();
    const hosts = new Set(lightingDraft.map(device => device.host));
    for (const device of discoveryDevices) {
      if (hosts.has(device.ip)) continue;
      const row = document.createElement("div");
      row.className = "discovered-device";
      const label = document.createElement("p");
      text(label, device.name || device.ip);
      const address = document.createElement("small");
      text(address, `${device.ip}${device.pixel_count ? ` · ${device.pixel_count} LEDs` : ""}`);
      label.appendChild(address);
      const add = document.createElement("button");
      add.type = "button";
      add.className = "button button-quiet";
      text(add, "Add");
      add.disabled = lightingDraft.length >= 16;
      add.setAttribute("aria-label", `Add ${device.name || device.ip}`);
      add.addEventListener("click", () => addLighting({ name: device.name || device.ip, host: device.ip, pixel_count: device.pixel_count || 46 }));
      row.append(label, add);
      target.appendChild(row);
    }
    if (discoveryDevices.length && !target.children.length) text($("discovery-status"), "All discovered lights have been added. You can still add one manually.");
  }
  async function discoverLighting(secondPass = false) {
    $("lighting-discovery").hidden = false;
    $("lighting-discover").disabled = true;
    text($("discovery-status"), "Looking for WLED lights on your network…");
    clearTimeout(discoveryTimer);
    try {
      const result = await api("/api/wled/discovered");
      discoveryDevices = Array.isArray(result.devices) ? result.devices : [];
      text($("discovery-status"), discoveryDevices.length ? "Lights found nearby. Add one, then save your lighting settings." : secondPass ? "No lights found yet. Check they’re on the same network, or add an address manually." : "The network scan is running. Checking again in a moment…");
      renderDiscovered();
      if (!secondPass && !discoveryDevices.length) discoveryTimer = setTimeout(() => { if (authorised && !document.hidden) discoverLighting(true); }, 4500);
    } catch (error) {
      if (!handleAuth(error)) text($("discovery-status"), errorMessage(error));
    } finally { $("lighting-discover").disabled = false; }
  }
  async function saveLighting(event) {
    event.preventDefault();
    if (lightingBusy || !lightingDirty || !$("lighting-form").reportValidity()) return;
    const devices = collectDevices();
    const enabled = $("lighting-enabled").checked;
    if (new Set(devices.map(device => device.host.toLowerCase())).size !== devices.length) return message("lighting-error", "Each light needs its own IP address or hostname. Remove the duplicate before saving.");
    lightingBusy = true;
    $("lighting-fields").disabled = true;
    text($("lighting-save"), "Saving…");
    try {
      await post("/api/wled/devices", { devices, enabled });
      lightingSaved = { devices, enabled };
      lightingDraft = cloneDevices(devices);
      renderLighting();
      setLightingClean();
      text($("lighting-badge"), enabled ? "Sync on" : "Sync off");
      $("lighting-badge").classList.toggle("muted", !enabled);
      message("lighting-error", "");
      toast("Lighting saved.");
      refreshOverview();
    } catch (error) {
      if (!handleAuth(error)) message("lighting-error", errorMessage(error));
    } finally {
      lightingBusy = false;
      $("lighting-fields").disabled = false;
      text($("lighting-save"), "Save lighting");
    }
  }

  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const input = $("owner-token");
    const token = input.value.trim();
    if (!token) return;
    $("login-submit").disabled = true;
    message("login-error", "");
    try {
      await post("/api/auth/owner", { token });
      input.value = "";
      await refreshOverview(true);
    } catch (error) { message("login-error", errorMessage(error)); }
    finally { input.value = ""; $("login-submit").disabled = false; }
  });
  $("pairing-create").addEventListener("click", () => createPairing("household"));
  $("pairing-guest").addEventListener("click", () => createPairing("guest"));
  $("pairing-copy").addEventListener("click", async () => {
    const input = $("pairing-url");
    if (!input.value || Date.now() >= pairingExpires) return updatePairingExpiry();
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(input.value);
      toast("Pairing link copied.");
    } catch (_) {
      input.focus(); input.select();
      toast("Link selected. Copy it using your browser’s copy action.");
    }
  });
  $("disconnect-show").addEventListener("click", () => {
    const account = overview && overview.account || {};
    if (!account.account_id) return toast("Refresh settings before disconnecting a library.");
    disconnectAccountId = account.account_id;
    text($("disconnect-confirm-copy"), `Disconnect ${account.display_name ? `${account.display_name}’s library` : "this library"}? Its playlists and saved albums will leave this profile’s shelf. Other household libraries stay linked.`);
    $("disconnect-confirm").hidden = false;
    $("disconnect-cancel").focus();
  });
  $("disconnect-cancel").addEventListener("click", () => {
    disconnectAccountId = null;
    $("disconnect-confirm").hidden = true;
    $("disconnect-show").focus();
  });
  $("disconnect-confirm-button").addEventListener("click", async () => {
    if (!disconnectAccountId) return;
    const button = $("disconnect-confirm-button");
    button.disabled = true;
    try {
      await post("/api/auth/disconnect", { account_id: disconnectAccountId });
      disconnectAccountId = null;
      $("disconnect-confirm").hidden = true;
      clearPairing();
      toast("Library disconnected.");
      await refreshOverview();
    } catch (error) { if (!handleAuth(error)) toast(errorMessage(error)); }
    finally { button.disabled = false; }
  });
  $("health-refresh").addEventListener("click", () => refreshOverview());
  $("lighting-retry").addEventListener("click", loadLighting);
  $("lighting-add").addEventListener("click", () => addLighting());
  $("lighting-discover").addEventListener("click", () => discoverLighting());
  $("lighting-form").addEventListener("input", markLightingDirty);
  $("lighting-form").addEventListener("change", markLightingDirty);
  $("lighting-form").addEventListener("submit", saveLighting);
  $("lighting-form").addEventListener("focusin", event => {
    if (event.target instanceof HTMLInputElement) openInputEditor(event.target);
  });
  $("lighting-form").addEventListener("click", event => {
    if (event.target instanceof HTMLInputElement) openInputEditor(event.target);
  });
  $("owner-input-cancel").addEventListener("click", () => closeInputEditor());
  $("owner-input-apply").addEventListener("click", applyInputEditor);
  $("owner-input-value").addEventListener("input", () => message("owner-input-error", ""));
  $("owner-input-dialog").addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeInputEditor(); }
    if (event.key === "Enter") { event.preventDefault(); applyInputEditor(); }
    if (event.key === "Tab") {
      const controls = [...$("owner-input-dialog").querySelectorAll('button:not(:disabled), input')].filter(element => element.getClientRects().length);
      if (!controls.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener("resize", () => {
    if (editingInput && !usesTouchKeyboard()) closeInputEditor();
  });
  $("lighting-revert").addEventListener("click", () => {
    if (!lightingSaved) return;
    lightingDraft = cloneDevices(lightingSaved.devices);
    $("lighting-enabled").checked = !!lightingSaved.enabled;
    renderLighting();
    setLightingClean();
    message("lighting-error", "");
    toast("Unsaved lighting changes discarded.");
  });
  function updateSectionLinks() {
    document.querySelectorAll(".section-nav a").forEach(link => {
      const current = link.hash === (location.hash || "#library");
      link.classList.toggle("current", current);
      if (current) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }
  window.addEventListener("hashchange", updateSectionLinks);
  updateSectionLinks();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearTimeout(refreshTimer); clearTimeout(discoveryTimer); }
    else {
      if (pairingExpires) updatePairingExpiry();
      if (authorised) refreshOverview();
    }
  });
  window.addEventListener("beforeunload", event => {
    if (!lightingDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(refreshTimer); clearTimeout(discoveryTimer); clearInterval(pairingTimer);
    $("owner-token").value = "";
    closeInputEditor(false);
  });
  window.addEventListener("pageshow", event => {
    if (!event.persisted) return;
    if (pairingExpires) {
      updatePairingExpiry();
      clearInterval(pairingTimer);
      if (Date.now() < pairingExpires) pairingTimer = setInterval(updatePairingExpiry, 1000);
    }
    if (authorised) refreshOverview();
  });
  refreshOverview(true);
})();
