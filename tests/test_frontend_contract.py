"""Lightweight regressions for the single-file kiosk client.

These assertions intentionally cover safety contracts that are easy to lose in
an otherwise visual template. Browser-level visual tests remain a separate Pi
deployment check.
"""

from html.parser import HTMLParser
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JOIN = (ROOT / "templates" / "join.html").read_text(encoding="utf-8")
CONNECT = (ROOT / "templates" / "connect.html").read_text(encoding="utf-8")


class _ControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags_by_id: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.tags_by_id[element_id] = (tag, attributes)


def _controls():
    parser = _ControlParser()
    parser.feed(INDEX)
    return parser.tags_by_id


def _js_function(name: str) -> str:
    """Extract one simple top-level function for a small Node behavior test."""
    start = INDEX.index(f"function {name}(")
    brace = INDEX.index("{", start)
    depth = 0
    for position in range(brace, len(INDEX)):
        if INDEX[position] == "{":
            depth += 1
        elif INDEX[position] == "}":
            depth -= 1
            if depth == 0:
                return INDEX[start : position + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(source: str) -> None:
    subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)


def test_primary_controls_are_semantic_buttons():
    controls = _controls()
    for element_id in ("skip-prev", "skip-next", "pause-btn", "wled-chip"):
        tag, attributes = controls[element_id]
        assert tag == "button"
        assert attributes.get("type") == "button"
        assert attributes.get("aria-label") or element_id == "wled-chip"

    # Button conversion must not introduce Chromium's native padding/surface.
    control_reset = INDEX.split(".skip-btn,", 1)[1].split("}", 1)[0]
    assert "appearance: none" in control_reset
    assert "padding: 0" in control_reset
    assert "background: transparent" in control_reset


def test_playback_transport_is_single_flight_with_sse_fallback():
    assert 'new EventSource("/api/events")' in INDEX
    assert "if (pollInFlight)" in INDEX
    assert "pollQueued = true" in INDEX
    assert "setInterval(poll" not in INDEX
    assert 'result.kind === "idle"' in INDEX
    assert 'result.kind === "error"' in INDEX


def test_cancelled_primary_pointer_does_not_finalize_gesture():
    cancel_block = INDEX.split(
        'viewport.addEventListener("pointercancel"', 1
    )[1].split("});", 1)[0]
    assert "finalizeMtGesture" not in cancel_block
    assert "abortMtGesture" in cancel_block
    assert "finishBrightnessGesture(true)" in cancel_block
    assert "setBrightness(gesture.base" in INDEX


def test_single_finger_swipe_survives_capture_transfer_and_keeps_physical_direction():
    source = "\n".join((
        _js_function("swipeActionForDelta"),
        "if (swipeActionForDelta(200) !== 'next') throw new Error('right swipe no longer skips next');",
        "if (swipeActionForDelta(-200) !== 'previous') throw new Error('left swipe no longer skips previous');",
    ))
    _run_node(source)
    pointer_move = INDEX.split(
        'viewport.addEventListener("pointermove"', 1
    )[1].split("function releasePointer", 1)[0]
    assert "viewport.setPointerCapture(e.pointerId)" in pointer_move

    capture_loss = INDEX.split(
        'viewport.addEventListener("lostpointercapture"', 1
    )[1].split("});", 1)[0]
    assert "if (e.target !== viewport) return" in capture_loss
    assert "swipeGesture.captured = false" in capture_loss
    assert "activePointers.delete" not in capture_loss
    assert "cancelSwipe" not in capture_loss
    assert "abortMtGesture" not in capture_loss
    assert "finishBrightnessGesture" not in capture_loss

    pointer_up = INDEX.split(
        'viewport.addEventListener("pointerup"', 1
    )[1].split('viewport.addEventListener("pointercancel"', 1)[0]
    assert "swipeActionForDelta(dx)" in pointer_up
    assert pointer_up.index("swipeGesture = null") < pointer_up.index(
        "viewport.releasePointerCapture(e.pointerId)"
    )


def test_three_finger_backlight_gesture_has_a_mirrored_radial_hud():
    controls = _controls()
    for element_id in (
        "brightness-hud",
        "bright-backing",
        "bright-track",
        "bright-fill",
        "bright-knob",
        "bright-label",
    ):
        assert element_id in controls

    _, hud = controls["brightness-hud"]
    assert hud.get("role") == "status"
    assert hud.get("aria-label") == "Display brightness"
    assert "#bright-label { left: 152px; }" in INDEX
    assert "const BRIGHT_A0 = 125, BRIGHT_A1 = 235" in INDEX
    assert "BRIGHT_A0 + (pct / 100) * (BRIGHT_A1 - BRIGHT_A0)" in INDEX
    assert "brightArcPath(BRIGHT_A0, a)" in INDEX


def test_three_finger_promotion_is_exclusive_and_drains_contacts():
    start = INDEX.split("function startBrightnessGesture()", 1)[1].split(
        "function processBrightnessGesture", 1
    )[0]
    assert "ids.length !== 3" in start
    assert "abortMtGesture()" in start
    assert "cancelSwipe()" in start

    abort = INDEX.split("function abortMtGesture()", 1)[1].split(
        "function startBrightnessGesture", 1
    )[0]
    assert 'mtGesture.mode === "vdrag"' in abort
    assert "setVolume(mtGesture.volBase)" in abort

    pointer_down = INDEX.split(
        'viewport.addEventListener("pointerdown"', 1
    )[1].split('viewport.addEventListener("pointermove"', 1)[0]
    assert "activePointers.size === 3" in pointer_down
    assert pointer_down.index("activePointers.size === 3") < pointer_down.index("state.browsing")

    assert "gesture.moved.size === 3" in INDEX
    assert "Math.abs(dCy) >= BRIGHTNESS_START_PX" in INDEX
    assert "multiTouchDrain = activePointers.size > 0" in INDEX
    assert "three-finger vertical drag → display backlight" in INDEX


def test_backlight_requests_are_safe_semantic_and_idle_restores_active_value():
    assert 'fetch("/api/backlight", { cache: "no-store" })' in INDEX
    assert 'scheduleBrightnessMode(screenDimmed ? "idle" : "active", true)' in INDEX
    assert 'data.mode === "idle"' in INDEX
    assert "state.activeBrightness" in INDEX
    assert "brightSendInFlight" in INDEX
    assert "brightPendingIntent" in INDEX
    assert "body: JSON.stringify(intent.command)" in INDEX
    # The browser sends only logical percent/mode commands; the raw HID report
    # remains an implementation detail of the local Python controller.
    assert "report_id" not in INDEX.lower()
    assert "hidraw" not in INDEX.lower()


def test_backlight_retry_is_bounded_and_never_resurrects_stale_intent():
    assert "const BRIGHTNESS_RETRY_DELAYS_MS = [250, 750, 1500]" in INDEX
    assert "intent.retryCount < BRIGHTNESS_RETRY_DELAYS_MS.length" in INDEX
    assert "intent.revision === brightnessIntentRevision" in INDEX
    assert "brightPendingIntent === null" in INDEX
    assert 'brightTimerKind !== "retry"' in INDEX
    assert "response.status === 429 || response.status >= 500" in INDEX


def test_wake_landing_is_drained_until_every_contact_is_released():
    assert "#dimmer.wake-drain" in INDEX
    assert "const wakeContactPointers = new Set()" in INDEX
    assert 'dimmer.classList.add("wake-drain")' in INDEX
    assert "wakeContactPointers.delete(e.pointerId)" in INDEX
    assert 'dimmer.classList.remove("wake-drain")' in INDEX
    assert 'dimmer.addEventListener("pointercancel", finishWakeContact)' in INDEX
    assert 'dimmer.addEventListener("lostpointercapture", finishWakeContact)' in INDEX
    wake = INDEX.split("function wakeScreen", 1)[1].split(
        "function finishWakeContact", 1
    )[0]
    assert wake.index('dimmer.classList.add("wake-drain")') < wake.index(
        "setScreenDimmed(false)"
    )
    finish = INDEX.split("function finishWakeContact", 1)[1].split(
        "function clearWakeContactDrain", 1
    )[0]
    assert "if (wakeContactPointers.size) return" in finish
    dim = INDEX.split("function setScreenDimmed", 1)[1].split(
        "function observeFrame", 1
    )[0]
    assert "if (screenDimmed === dimmed) return" in dim


def test_lrc_fraction_is_normalized_to_milliseconds():
    assert '.padEnd(3, "0").slice(0, 3)' in INDEX
    assert "parseInt(m[3]) * 10" not in INDEX


def test_modal_and_tracklist_expose_accessible_state():
    controls = _controls()
    _, tracklist = controls["tracklist"]
    _, modal = controls["wled-modal"]
    assert tracklist.get("role") == "dialog"
    assert tracklist.get("aria-hidden") == "true"
    assert modal.get("aria-modal") == "true"
    assert modal.get("aria-labelledby") == "wled-title"


def test_join_page_describes_receiver_bound_private_pairing():
    assert "link a household spotify account" in JOIN.lower()
    assert "one-use link expires quickly" in JOIN.lower()
    assert "bound to the account currently playing" in JOIN.lower()
    assert "household link itself stays saved" in JOIN.lower()
    assert "spotify asks that member to reauthorize" in JOIN.lower()
    assert "never another person’s library" in JOIN.lower()
    assert 'href="/login"' in JOIN
    assert "/login?playlist=" not in JOIN
    assert "your crate is linked" in CONNECT.lower()
    assert "safe house picks" in CONNECT.lower()


def test_wled_refresh_is_slow_and_sleeps_with_the_screen():
    assert "const WLED_STATUS_INTERVAL = 15000" in INDEX
    assert "const WLED_DEVICES_INTERVAL = 30000" in INDEX
    assert "setInterval(refreshWled" not in INDEX
    assert "if (wledRefreshTimer) clearTimeout(wledRefreshTimer)" in INDEX


def test_empty_crate_payload_is_authoritative_and_clears_private_ui():
    apply_payload = INDEX.split("function applyCratePayload", 1)[1].split(
        "function refreshCrateData", 1
    )[0]
    clear = INDEX.split("function clearCrateUi", 1)[1].split(
        "function renderCrateData", 1
    )[0]
    assert "Array.isArray(data.sections)" in apply_payload
    assert "crateData = data" in apply_payload
    assert "data.sections.length" not in apply_payload
    for stale_surface in (
        "crateItems = []",
        "crateCards = []",
        "crateChips.replaceChildren()",
        "crateBelt.replaceChildren()",
        'capTitle.textContent = showEmptyMessage ? "No records available"',
    ):
        assert stale_surface in clear


def test_building_crate_is_transient_preserves_stale_data_and_retries_quickly():
    source = "\n".join((
        "let crateData = { sections: [{ title: 'Private old shelf' }] };",
        "const previous = crateData;",
        "let crateBuilding = false, shelfRetryAt = 0, shelfFetchedAt = 0, shelfRetryTimer = null;",
        "const CRATE_BUILD_RETRY_MS = 3000, CRATE_CACHE_MS = 120000;",
        'let crateProfileState = "linked";',
        "const scheduled = []; let loading = 0, preloaded = 0, rendered = 0;",
        "function applyCrateProfileSignal() {}",
        "function crateProfileNeedsLink() { return false; }",
        "function refreshCratePairingLink() {}",
        "function scheduleCrateRetry(delay) { scheduled.push(delay); }",
        "function renderCrateLoading() { loading++; }",
        "function preloadCrateImages() { preloaded++; }",
        "function renderCrateData() { rendered++; }",
        _js_function("applyCratePayload"),
        'if (applyCratePayload({ sections: [], building: true, profile_state: "linked", profile_epoch: "epoch-a" }, 100)) throw new Error("building accepted as ready");',
        "if (crateData !== previous || !crateBuilding) throw new Error('completed shelf was not preserved');",
        "if (loading !== 0 || scheduled[0] !== 3000 || shelfRetryAt !== 3100) throw new Error('warm building state handled incorrectly');",
        "crateData = null;",
        'applyCratePayload({ sections: [], building: true, profile_state: "linked", profile_epoch: "epoch-a" }, 200);',
        "if (loading !== 1) throw new Error('cold building state did not show loading');",
        'const ready = { sections: [], building: false, profile_state: "linked", profile_epoch: "epoch-a" };',
        "if (!applyCratePayload(ready, 300)) throw new Error('ready empty payload rejected');",
        "if (crateData !== ready || crateBuilding || shelfFetchedAt !== 300) throw new Error('ready payload not committed');",
        "if (preloaded !== 1 || rendered !== 1) throw new Error('ready payload not rendered');",
    ))
    _run_node(source)
    assert "const CRATE_BUILD_RETRY_MS = 3000" in INDEX
    assert "scheduleCrateRetry(CRATE_BUILD_RETRY_MS)" in INDEX


def test_profile_epoch_handoff_synchronously_invalidates_private_crate():
    source = "\n".join((
        "const CRATE_PROFILE_STATES = new Set(['linked', 'unlinked', 'reauth_required', 'no_receiver']);",
        "let crateProfileSeen = true, crateProfileState = 'linked', crateProfileEpoch = 'epoch-a';",
        "let crateProfileRevision = 4, crateJoinUrl = 'https://display.test/join?pair=old';",
        "let cratePairingUnavailable = false, clears = 0, renders = 0;",
        "let crateData = { sections: [{ title: 'Private shelf' }] };",
        "function refreshCratePairingLink() { return Promise.resolve(false); }",
        "function invalidateCrateContents() { clears++; crateData = null; }",
        "function renderCrateProfilePrompt() { renders++; }",
        _js_function("crateProfileSignal"),
        _js_function("currentCrateProfileMatches"),
        _js_function("crateProfileNeedsLink"),
        _js_function("applyCrateProfileSignal"),
        "if (applyCrateProfileSignal({profile_state: 'linked', profile_epoch: 'epoch-a'})) throw new Error('same profile changed');",
        "if (clears !== 0 || crateData === null) throw new Error('same profile was cleared');",
        "if (!applyCrateProfileSignal({profile_state: 'unlinked', profile_epoch: 'epoch-b'})) throw new Error('handoff missed');",
        "if (clears !== 1 || crateData !== null) throw new Error('private crate was not synchronously cleared');",
        "if (crateProfileEpoch !== 'epoch-b' || crateProfileState !== 'unlinked' || crateProfileRevision !== 5) throw new Error('new context not committed');",
        "if (crateJoinUrl !== null) throw new Error('old pairing URL crossed handoff');",
        "if (!applyCrateProfileSignal({profile_state: 'reauth_required', profile_epoch: 'epoch-c'})) throw new Error('reauthorization transition missed');",
        "if (clears !== 2 || crateProfileState !== 'reauth_required' || crateProfileEpoch !== 'epoch-c') throw new Error('reauthorization context not committed');",
    ))
    _run_node(source)
    apply_payload = INDEX.split("function applyCratePayload", 1)[1].split(
        "function refreshCrateData", 1
    )[0]
    assert "applyCrateProfileSignal(data, { required: true })" in apply_payload
    assert "profile_name" not in INDEX
    assert "username" not in INDEX.lower()
    assert "account_id" not in INDEX.lower()


def test_profile_context_arrives_on_playback_sse_and_idle_headers():
    assert 'response.headers.get("X-Spotify-Profile-State")' in INDEX
    assert 'response.headers.get("X-Spotify-Profile-Epoch")' in INDEX
    assert "observeReceiverProfile(result.data)" in INDEX
    assert "observeReceiverProfile(result.data || result.profile)" in INDEX
    assert "const data = JSON.parse(event.data)" in INDEX
    assert "observeReceiverProfile(data)" in INDEX
    poll_apply = INDEX.split('lastPlaybackSuccessAt = diagnostics.lastSuccessAt;', 1)[1].split(
        "renderDiagnostics();", 1
    )[0]
    assert poll_apply.index("observeReceiverProfile") < poll_apply.index("applyPlaybackData")


def test_crate_launch_is_epoch_bound_and_recovers_from_stale_409():
    launch = INDEX.split("async function launchItem(item)", 1)[1].split(
        "// Warm only the likely-visible sleeves", 1
    )[0]
    assert "profile_epoch: launchProfileEpoch" in launch
    assert 'r.status === 409 && responseData.code === "profile_changed"' in launch
    assert "applyCrateProfileSignal(responseData, { required: true, forceClear: true })" in launch
    assert "queueCrateRefreshForProfile()" in launch
    assert "launchProfileRevision !== crateProfileRevision" in launch


def test_unlinked_crate_uses_safe_generic_copy_and_pairing_cta():
    controls = _controls()
    tag, prompt = controls["crate-profile-prompt"]
    assert tag == "div"
    assert prompt.get("role") == "status"
    link_tag, link = controls["crate-profile-link"]
    assert link_tag == "span"
    assert "hidden" in link
    assert "House picks are showing" in INDEX
    assert "Link this household Spotify account to show its playlists and albums" in INDEX
    assert "Household Spotify linking is temporarily unavailable" in INDEX
    assert "Linked household crates return automatically; everyone else sees House picks" in INDEX
    assert 'fetch("/api/idle/playlists"' in INDEX
    assert 'url.protocol === "http:" || url.protocol === "https:"' in INDEX
    assert "crateProfileCopy.textContent = message" in INDEX
    assert 'crateProfileState === "reauth_required" ? "link again at " : "open "' in INDEX
    assert "crateProfileLink.href" not in INDEX


def test_reauthorization_state_falls_back_to_house_picks_and_links_again_privately():
    assert '"reauth_required"' in INDEX
    assert "This household Spotify account needs to be linked again" in INDEX
    assert "Spotify reauthorization is temporarily unavailable" in INDEX
    assert "Link this household Spotify account again to restore its albums and playlists" in INDEX
    assert "function crateProfileNeedsLink()" in INDEX
    assert 'crateProfileState === "unlinked" || crateProfileState === "reauth_required"' in INDEX
    assert "profile_name" not in INDEX
    assert "username" not in INDEX.lower()
    assert "account_id" not in INDEX.lower()


def test_rotation_is_explicitly_labelled_as_top_listening_approximation():
    source = "\n".join((
        "function stableSectionKey(section, index) { return String(section && section.id || section && section.title || 'section-' + index); }",
        _js_function("crateSectionIsTopListening"),
        _js_function("crateSectionDisplayTitle"),
        "const rotation = { id: 'rotation', title: 'Your rotation' };",
        "if (!crateSectionIsTopListening(rotation, 0)) throw new Error('rotation section not recognized');",
        "if (crateSectionDisplayTitle(rotation, 0) !== 'Your rotation · top listening') throw new Error('rotation approximation label missing');",
        "if (crateSectionDisplayTitle({ id: 'house', title: 'House picks' }, 1) !== 'House picks') throw new Error('house label changed');",
    ))
    _run_node(source)
    assert '"Top-listening approximation"' in INDEX


def test_missing_profile_context_fails_closed_and_pairing_does_not_wait_for_crate():
    fetcher = INDEX.split("async function fetchNowPlaying", 1)[1].split(
        "function noteActivity", 1
    )[0]
    assert "missing receiver profile context" in fetcher
    assert "crateProfileSignal(data)" in fetcher
    assert "markCrateProfileUnknown()" in INDEX
    assert "else markCrateProfileUnknown()" in INDEX
    assert "if (!trusted) markCrateProfileUnknown()" in INDEX
    apply_signal = INDEX.split("function applyCrateProfileSignal", 1)[1].split(
        "function markCrateProfileUnknown", 1
    )[0]
    assert "changed && crateProfileNeedsLink()" in apply_signal
    assert "refreshCratePairingLink()" in apply_signal


def test_idle_transition_closes_tracklist_and_neutralises_artwork():
    idle = INDEX.split("function applyIdlePlayback()", 1)[1].split(
        "function shouldUse45Rpm", 1
    )[0]
    assert "if (state.tracklisting) closeTracklist()" in idle
    assert "tlPicker.replaceChildren()" in idle
    assert "stageNeutralArtwork(null)" in idle
    assert idle.index('state.trackName = ""') < idle.index("stageNeutralArtwork(null)")
    assert 'state.artUrl = ""' in INDEX
    assert "else if (!artUrl && trackChanged)" in INDEX
    assert "stageNeutralArtwork(newKey)" in INDEX
    assert "epoch invalidates every older image/palette promise" in INDEX


def test_playback_transition_retires_wled_setup_and_serialises_mutations():
    playback = INDEX.split("function applyPlaybackData(data)", 1)[1].split(
        "function handlePlaybackError", 1
    )[0]
    assert 'wledModal.classList.contains("open")' in playback
    assert "closeWledModal()" in playback
    assert "updateWledChipVisibility()" in playback
    _, chip = _controls()["wled-chip"]
    assert chip.get("tabindex") == "-1"
    assert "let wledMutationQueue = Promise.resolve()" in INDEX
    assert "serialiseWledMutation" in INDEX


def test_reduced_motion_and_static_scenes_do_not_run_full_rate():
    assert "if (REDUCED_MOTION) {\n      tlOffset = target" in INDEX
    assert "if (REDUCED_MOTION) {\n        crateScroll = crateTarget" in INDEX
    assert "minimizeProg = minTarget" in INDEX
    assert "function animationNeedsFullRate()" in INDEX
    assert "animationWakeTimer = setTimeout" in INDEX
    assert "REDUCED_MOTION && state.isPlaying ? 250 : 1000" in INDEX
    assert "!state.tracklisting && !state.browsing" in INDEX
    assert "scheduleAnimation(true)" in INDEX.split("function startRecordFlip", 1)[1].split(
        "function stageArtworkBundle", 1
    )[0]


def test_wled_dialog_traps_initial_focus_and_inerts_background():
    assert 'setModalLayersInert("wled", wledBackgroundLayers, true)' in INDEX
    assert 'setModalLayersInert("wled", wledBackgroundLayers, false)' in INDEX
    assert 'setModalLayersInert("tracklist", tracklistBackgroundLayers, true)' in INDEX
    assert 'setModalLayersInert("tracklist", tracklistBackgroundLayers, false)' in INDEX
    assert "record.owners.add(owner)" in INDEX
    assert "layer.inert = record.initial" in INDEX
    assert "document.activeElement === panel" in INDEX
    assert "(e.shiftKey ? last : first).focus()" in INDEX
    _run_node("\n".join((
        "const modalInertRecords = new Map();",
        _js_function("setModalLayersInert"),
        "const shared = { inert: false }; const preInert = { inert: true };",
        'setModalLayersInert("tracklist", [shared, preInert], true);',
        'setModalLayersInert("wled", [shared], true);',
        'setModalLayersInert("tracklist", [shared, preInert], false);',
        'if (!shared.inert || !preInert.inert) throw new Error("overlap/original inert state lost");',
        'setModalLayersInert("wled", [shared], false);',
        'if (shared.inert || !preInert.inert) throw new Error("final inert state not restored");',
    )))


def test_failed_current_artwork_commits_neutral_without_losing_retry_url():
    source = "\n".join((
        'let artworkEpoch = 7;',
        'let artworkLoadFailedAt = -Infinity;',
        'const state = { playbackKey: "track-7", artUrl: "https://cdn.example/art?x=1&sig=abc" };',
        'const NEUTRAL_ART_URL = "neutral-art";',
        'const commits = [];',
        'function commitArtworkBundle(url, palette) { commits.push([url, palette]); }',
        _js_function("artworkRequestIsCurrent"),
        _js_function("failArtworkBundle"),
        'if (!failArtworkBundle(7, "track-7", state.artUrl)) throw new Error("current failure ignored");',
        'if (state.artUrl !== "https://cdn.example/art?x=1&sig=abc") throw new Error("desired URL lost");',
        'if (commits.length !== 1 || commits[0][0] !== NEUTRAL_ART_URL || commits[0][1] !== null) throw new Error("neutral not committed");',
        'if (!(artworkLoadFailedAt > 0)) throw new Error("retry timestamp missing");',
        'if (failArtworkBundle(6, "track-7", state.artUrl)) throw new Error("stale failure committed");',
        'if (commits.length !== 1) throw new Error("stale request changed artwork");',
    ))
    _run_node(source)
    assert "artworkLoadFailedAt > 0 && now - artworkLoadFailedAt > 15000" in INDEX
    load = INDEX.split("async function loadArtworkBundle", 1)[1].split(
        "function setTrackTitle", 1
    )[0]
    assert "failArtworkBundle(epoch, playbackKey, url)" in load


def test_css_url_serialization_preserves_ampersands_and_quotes_at_runtime():
    source = "\n".join((
        _js_function("cssUrl"),
        'const input = "https://cdn.example/cover?size=300&sig=one\\\"two";',
        'const output = cssUrl(input);',
        'if (output.includes("&amp;")) throw new Error("HTML entity leaked into CSS URL");',
        'if (!output.includes("&sig=")) throw new Error("query separator lost");',
        'if (JSON.parse(output.slice(4, -1)) !== input) throw new Error("CSS URL did not round-trip");',
    ))
    _run_node(source)
    assert "backgroundImage = cssUrl(el._imageUrl)" in INDEX
    assert "backgroundImage = cssUrl(img)" in INDEX
