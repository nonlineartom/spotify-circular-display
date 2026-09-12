"""Behavior regressions for library exploration, without a browser or Spotify.

The exported helpers are the same ones used to build the cover browser. Running
them in Node catches ordering, filtering and stale metadata mistakes without
asserting implementation strings or requiring live account credentials.
"""

import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPLORER = ROOT / "static" / "explore.js"


def _run_explorer(source: str) -> None:
    script = "\n".join((
        'const assert = require("node:assert/strict");',
        f"const lib = require({json.dumps(str(EXPLORER))});",
        source,
    ))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _explorer_function(name: str) -> str:
    source = EXPLORER.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    brace = source.index("{", start)
    depth = 0
    for position in range(brace, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start:position + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def _run_lifecycle(source: str) -> None:
    """Exercise real async functions with deferred I/O and minimal output nodes.

    These stubs only collect rendered values. They do not model selectors,
    events or layout, which are checked in the real browser separately.
    """
    setup = """
      const {buildLibraryItems, formatAlbumMetadata} = lib;
      function node() { return {
        hidden: false, disabled: false, textContent: '', children: [],
        replaceChildren(...children) {this.children = children;},
        append(...children) {this.children.push(...children);},
        querySelectorAll() {return this.children;},
        setAttribute() {}, focus() {},
      }; }
      const elements = new Map();
      const $ = id => {if (!elements.has(id)) elements.set(id, node()); return elements.get(id);};
      const document = {querySelectorAll: () => [], createElement: node};
      const explore = node(), controls = node(), grid = node(), belt = node();
      let mode = 'explore', restoreFocus = null, selected = null, selectedFocusUri = null;
      let items = [], loading = false, loadError = '', refreshing = null;
      let requestEpoch = 0, libraryEpoch = 0, controlEpoch = 0;
      let detailAbort = null, refreshAbort = null, refreshTimer = null, controlTimer = null;
      let playPending = false, playController = null, controlPending = false;
      let pointers = new Map(), drag = null, libraryFingerprint = '', libraryProfileEpoch = null;
      let section = 'discover', page = 0, mapCovers = [], gridScrollTop = 0;
      let detailMotion = null, selectedMetadata = null, restoringCoverFocus = false, personalStatus = null;
      let played = 0;
      const bridge = {setModal() {}, onPlayed() {played++;}};
      const collectionEditor = {close() {}};
      let renders = 0, capturedElement = null, capturedCover = null, cancelledDetailMotions = 0;
      function render() {renders++;} function renderDetailHero() {} function renderQuickLinks() {}
      function captureCover(element) {capturedElement = element; return capturedCover;}
      function cancelDetailMotion() {cancelledDetailMotions++; detailMotion = null;}
      function animateDetailIn() {} function animateDetailOut() {}
      function hideSearchKeyboard() {}
      let mapStops = 0;
      function stopMapMotion() {mapStops++;}
      function button() { return node(); }
      let timerId = 0;
      const timers = new Map();
      function setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, delay}); return id;}
      function clearTimeout(id) {timers.delete(id);}
      const clearInterval = clearTimeout;
      const requests = [];
      function fetch(url, options = {}) {
        return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
      }
      const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
      const aborted = () => Object.assign(new Error('old request aborted'), {name: 'AbortError'});
    """
    functions = "\n".join(_explorer_function(name) for name in (
        "close", "clearSelection", "backToBrowser", "refreshLibrary",
        "loadDetail", "playSelection", "setPlayDisabled", "formatTime",
        "invalidateProfile", "acceptLibraryProfile",
    ))
    _run_explorer("\n".join((
        setup, functions,
        "(async () => {", source,
        "})().catch(error => { console.error(error); process.exitCode = 1; });",
    )))


def test_one_record_keeps_every_source_when_library_sections_overlap():
    _run_explorer("""
        const shared = {uri: 'spotify:album:shared', title: 'Mezzanine', subtitle: 'Massive Attack'};
        const payload = {sections: [
          {id: 'saved', title: 'Your albums', items: [shared]},
          {id: 'recent', title: 'Recently spun', items: [{...shared}]},
          {id: 'deeper', title: 'Deeper cuts', items: [{...shared},
            {uri: 'spotify:album:other', title: 'Protection', subtitle: 'Massive Attack'}]},
        ]};
        const original = JSON.stringify(payload);
        const records = lib.buildLibraryItems(payload);
        assert.equal(records.length, 2, 'each record should appear once');
        const record = records.find(item => item.uri === shared.uri);
        assert.deepEqual(new Set(record.sections), new Set(['saved', 'recent', 'deeper']));
        for (const section of ['saved', 'recent', 'deeper']) {
          assert(lib.filterLibraryItems(records, {section}).some(item => item.uri === shared.uri),
            `the same album must remain discoverable in ${section}`);
        }
        assert.equal(JSON.stringify(payload), original, 'browsing must not mutate the shared shelf payload');
    """)


def test_personal_playlist_refresh_requests_fresh_account_data_before_loading_crate():
    _run_lifecycle("""
      bridge.getProfile = () => ({epoch:'profile-a', revision:1});
      bridge.acceptProfile = data => data.profile_epoch === 'profile-a';
      libraryProfileEpoch = 'profile-a';
      const task = refreshLibrary(true);
      assert.equal(requests[0].url, '/api/crate/refresh');
      assert.equal(requests[0].options.method, 'POST');
      assert.equal(JSON.parse(requests[0].options.body).profile_epoch, 'profile-a');
      requests[0].resolve(response({profile_epoch:'profile-a', building:true}, 202));
      await new Promise(setImmediate);
      assert.equal(requests[1].url, '/api/crate');
      requests[1].resolve(response({profile_epoch:'profile-a', made_for_you:{status:'ready'}, sections:[
        {id:'made_for_you',items:[{uri:'spotify:playlist:weekly',title:'Discover Weekly',quick_kind:'discover_weekly'}]}
      ]}));
      await task;
      assert.equal(items[0].title, 'Discover Weekly');
      assert.equal(personalStatus.status, 'ready');
      assert.equal(loading, false);
    """)


def test_profile_change_during_manual_refresh_does_not_fetch_old_account_crate():
    _run_lifecycle("""
      bridge.getProfile = () => ({epoch:'profile-a', revision:1});
      bridge.acceptProfile = () => true;
      const task = refreshLibrary(true);
      invalidateProfile();
      assert(requests[0].options.signal.aborted);
      requests[0].resolve(response({profile_epoch:'profile-a', building:true},202));
      await task;
      assert.equal(requests.length,1);
      assert.equal(items.length,0);
    """)


def test_household_top_listening_is_a_distinct_browsable_source():
    _run_explorer("""
      const records = lib.buildLibraryItems({sections: [{id: 'rotation', items: [
        {uri: 'spotify:album:rotation', title: 'A favourite album'}
      ]}]});
      assert.equal(lib.filterLibraryItems(records, {section: 'rotation'}).length, 1);
      assert.equal(lib.discoveryReason(records[0]), 'From your top listening');
    """)


def test_personal_playlist_links_keep_their_kind_and_library_membership():
    _run_explorer("""
      const weekly = {uri: 'spotify:playlist:weekly', title: 'Discover Weekly', quick_kind: 'discover_weekly'};
      const radar = {uri: 'spotify:playlist:radar', title: 'Release Radar', quick_kind: 'release_radar'};
      const mix = {uri: 'spotify:playlist:mix', title: 'Daily Mix 1', quick_kind: 'mix'};
      const records = lib.buildLibraryItems({sections: [
        {id: 'made_for_you', items: [weekly, radar, mix]},
        {id: 'yours', items: [weekly, mix, {uri: 'spotify:playlist:ordinary', title: 'My driving songs'}]},
      ]});
      assert.equal(records.length, 4, 'a quick link must not duplicate its saved playlist');
      const personal = lib.filterLibraryItems(records, {section: 'made_for_you'});
      assert.deepEqual(personal.map(item => item.quick_kind), ['discover_weekly', 'release_radar', 'mix']);
      assert.deepEqual(lib.filterLibraryItems(records, {section: 'made_for_you', query: 'daily'}).map(item => item.uri), [mix.uri]);
      assert(lib.filterLibraryItems(records, {section: 'yours'}).some(item => item.uri === weekly.uri));
      assert.equal(lib.discoveryReason(personal[0]), 'Your weekly discoveries');
      assert.equal(lib.discoveryReason(personal[1]), 'New music from artists you follow');
      assert.equal(lib.discoveryReason(personal[2]), 'Made for your Spotify account');
    """)


def test_album_passage_preserves_release_precision_and_omits_unknown_facts():
    _run_explorer("""
      const describe = lib.formatAlbumMetadata;
      assert.equal(describe({release_date: '1998', release_date_precision: 'year'}), 'Released 1998.');
      assert.equal(describe({release_date: '1998-04', release_date_precision: 'month'}), 'Released April 1998.');
      assert.equal(describe({release_date: '1998-04-20', release_date_precision: 'day',
        total_tracks: 11, duration_ms: 3780000, label: 'Virgin Records'}),
        'Released 20 April 1998 on Virgin Records. 11 tracks, 63 minutes.');
      assert.equal(describe({release_date: '1998-04-20', release_date_precision: 'year'}), 'Released 1998.',
        'a low-precision date must not imply that its placeholder month or day is known');
      assert.equal(describe({release_date: '2025-09', release_date_precision: 'day'}), '',
        'a missing day must not silently become the first of the month');
      assert.equal(describe({release_date: '2025-02-30', release_date_precision: 'day'}), '');
      assert.equal(describe({total_tracks: 1, duration_ms: 60000}), '1 track, 1 minute.');
      assert.equal(describe({total_tracks: -2, duration_ms: NaN}), '');
      assert.equal(describe({label: '  Warp  '}), 'Label: Warp.');
      for (const missing of [undefined, null, {}, 'unknown']) assert.equal(describe(missing), '');
    """)


def test_receiver_profile_handoff_clears_explorer_and_cancels_stale_detail_and_play():
    _run_lifecycle("""
      bridge.getProfile = () => ({epoch: 'profile-a', revision: 1});
      bridge.acceptProfile = (data, revision) => revision === 1 && data.profile_epoch === 'profile-a';
      const refresh = refreshLibrary();
      requests[0].resolve(response({profile_state: 'linked', profile_epoch: 'profile-a', sections: [{id: 'saved', items: [
        {uri: 'spotify:album:private', title: 'Private album'}
      ]}]}));
      await refresh;
      assert.equal(libraryProfileEpoch, 'profile-a');
      selected = items[0];
      $('detail-title').textContent = selected.title;
      selectedMetadata = {release_date: '1998', release_date_precision: 'year', label: 'Private label'};
      $('detail-metadata').textContent = formatAlbumMetadata(selectedMetadata);
      $('explore-search').value = 'Private search';
      belt.replaceChildren(node()); grid.replaceChildren(node()); $('search-matches').replaceChildren(node());
      const detail = loadDetail();
      assert.equal(new URL(requests[1].url, 'http://display.test').searchParams.get('profile_epoch'), 'profile-a');
      const play = playSelection();
      assert.equal(JSON.parse(requests[2].options.body).profile_epoch, 'profile-a');
      bridge.getProfile = () => ({epoch: 'profile-b', revision: 2});
      invalidateProfile();
      assert.equal(items.length, 0);
      assert.equal(selected, null);
      assert.equal(libraryProfileEpoch, null);
      assert.equal($('detail-title').textContent, '');
      assert.equal($('detail-metadata').textContent, '');
      assert.equal(selectedMetadata, null);
      assert.equal($('explore-search').value, '');
      assert.equal(belt.children.length + grid.children.length + $('search-matches').children.length, 0);
      assert(requests[1].options.signal.aborted && requests[2].options.signal.aborted);
      requests[1].resolve(response({profile_state: 'linked', profile_epoch: 'profile-a', album_metadata: {label: 'Stale private label'}, tracks: [{name: 'Private track', uri: 'spotify:track:old'}]}));
      requests[2].resolve(response({status: 'ok', profile_state: 'linked', profile_epoch: 'profile-a'}));
      await Promise.all([detail, play]);
      assert.equal($('detail-tracks').children.length, 0);
      assert.equal($('detail-metadata').textContent, '', 'a stale response must not republish another account’s album facts');
      assert.equal(played, 0, 'a superseded profile response must not animate successful playback');
    """)


def test_missing_receiver_context_clears_private_library_and_bounds_retries():
    _run_lifecycle("""
      bridge.getProfile = () => ({epoch: 'profile-a', revision: 1});
      bridge.acceptProfile = () => false;
      items = [{uri: 'spotify:album:private', title: 'Private album', sections: ['saved']}];
      const refresh = refreshLibrary();
      requests[0].resolve(response({sections: [{id: 'saved', items: [{uri: 'spotify:album:stale', title: 'Stale album'}]}]}));
      await refresh;
      assert.equal(items.length, 0, 'missing profile context must fail closed');
      assert.equal(timers.size, 1);
      assert.equal(timers.get(refreshTimer).delay, 3000, 'malformed responses must not create a tight fetch loop');
    """)


def test_profile_changed_play_response_retires_selected_record():
    _run_lifecycle("""
      bridge.getProfile = () => ({epoch: 'profile-a', revision: 1});
      let observed = null;
      bridge.acceptProfile = (data, revision) => {observed = {data, revision}; return true;};
      libraryProfileEpoch = 'profile-a';
      selected = {uri: 'spotify:album:private', title: 'Private album'};
      items = [{...selected, sections: ['saved']}];
      const play = playSelection();
      requests[0].resolve(response({code: 'profile_changed', profile_state: 'linked', profile_epoch: 'profile-b'}, 409));
      await play;
      assert.equal(observed.revision, 1);
      assert.equal(observed.data.profile_epoch, 'profile-b');
      assert.equal(items.length, 0);
      assert.equal(selected, null);
      assert.equal(played, 0);
      assert.equal(playPending, false);
    """)


def test_discover_promotes_unfamiliar_records_and_keeps_personal_music():
    _run_explorer("""
        const payload = {sections: [
          {id: 'yours', items: [{uri: 'spotify:playlist:mine', title: 'My playlist'}]},
          {id: 'saved', items: [{uri: 'spotify:album:saved', title: 'Saved album'}]},
          {id: 'recent', items: [{uri: 'spotify:album:recent', title: 'Recent album'}]},
          {id: 'deeper', items: [{uri: 'spotify:album:deep', title: 'Another record by a familiar artist'}]},
          {id: 'house', items: [{uri: 'spotify:playlist:house', title: 'House selection'}]},
        ]};
        const records = lib.buildLibraryItems(payload);
        const naturalOrder = records.map(item => item.uri);
        const discover = lib.filterLibraryItems(records, {section: 'discover', sort: 'recommended'});
        const uris = discover.map(item => item.uri);
        assert.equal(uris[0], 'spotify:album:deep');
        assert(uris.indexOf('spotify:playlist:house') < uris.indexOf('spotify:playlist:mine'));
        assert(uris.indexOf('spotify:album:recent') < uris.indexOf('spotify:album:saved'));
        assert.deepEqual(new Set(uris), new Set(naturalOrder), 'discovery must keep the whole collection reachable');
        assert.deepEqual(records.map(item => item.uri), naturalOrder, 'ranking must not reorder shared records');
        assert.deepEqual(lib.filterLibraryItems(records, {section: 'all'}).map(item => item.uri), naturalOrder);
    """)


def test_search_finds_titles_and_artists_across_library_sources():
    _run_explorer("""
        const records = lib.buildLibraryItems({sections: [
          {id: 'saved', items: [{uri: 'spotify:album:a', title: 'Mezzanine', subtitle: 'Massive Attack'}]},
          {id: 'deeper', items: [{uri: 'spotify:album:b', title: 'Protection', subtitle: 'Massive Attack'}]},
          {id: 'house', items: [{uri: 'spotify:playlist:c', title: 'Late night', subtitle: 'Nocturnal listening'}]},
        ]});
        const search = (query, section = 'all') => lib.filterLibraryItems(records, {query, section}).map(item => item.uri);
        assert.deepEqual(new Set(search('  MASSIVE  ')), new Set(['spotify:album:a', 'spotify:album:b']));
        assert.deepEqual(search('mezzanine', 'discover'), ['spotify:album:a']);
        assert.deepEqual(search('NOCTURNAL'), ['spotify:playlist:c']);
        assert.deepEqual(search('massive', 'saved'), ['spotify:album:a']);
        assert.deepEqual(search('no matching record'), []);
    """)


def test_title_and_artist_sorting_keep_search_results_and_leave_input_intact():
    _run_explorer("""
        const records = lib.buildLibraryItems({sections: [{id: 'saved', items: [
          {uri: 'spotify:album:z', title: 'Zed', subtitle: 'Alpha artist'},
          {uri: 'spotify:album:a', title: 'alpha', subtitle: 'Zed artist'},
          {uri: 'spotify:album:b', title: 'Beta', subtitle: 'Middle artist'},
        ]}]});
        const original = JSON.stringify(records);
        const sorted = sort => lib.filterLibraryItems(records, {section: 'all', query: '', sort}).map(item => item.uri);
        assert.deepEqual(sorted('title'), ['spotify:album:a', 'spotify:album:b', 'spotify:album:z']);
        assert.deepEqual(sorted('artist'), ['spotify:album:z', 'spotify:album:b', 'spotify:album:a']);
        const filtered = lib.filterLibraryItems(records, {section: 'saved', query: 'Beta', sort: 'title'});
        assert.equal(filtered.length, 1);
        assert.equal(filtered[0].uri, 'spotify:album:b');
        assert.equal(JSON.stringify(records), original);
    """)


def test_paging_clamps_after_mosaic_filters_shrink_results():
    _run_explorer("""
        const records = Array.from({length: 40}, (_, id) => ({uri: `spotify:album:${id}`}));
        const first = lib.pageLibraryItems(records, -2, 37);
        assert.equal(first.page, 0);
        assert.equal(first.pageCount, 2);
        assert.equal(first.total, 40);
        assert.deepEqual(first.items, records.slice(0, 37));
        const last = lib.pageLibraryItems(records, 99, 37);
        assert.equal(last.page, 1);
        assert.deepEqual(last.items, records.slice(37));
        const honeycomb = lib.pageLibraryItems(records, 1, 19);
        assert.equal(honeycomb.pageCount, 3);
        assert.deepEqual(honeycomb.items, records.slice(19, 38));
        const filtered = lib.pageLibraryItems(records.slice(0, 2), 6);
        assert.equal(filtered.page, 0, 'a narrower search must not strand the user on an empty page');
        assert.equal(filtered.items.length, 2);
        const empty = lib.pageLibraryItems([], 6);
        assert.equal(empty.page, 0);
        assert.equal(empty.total, 0);
        assert.deepEqual(empty.items, []);
    """)


def test_grid_exposes_the_whole_collection_and_restores_scroll_after_details():
    _run_explorer("\n".join((
        """
        const {pageLibraryItems, libraryHoneycombPosition, discoveryReason, filterLibraryItems} = lib;
        function node() { return {
          hidden: false, disabled: false, textContent: '', children: [], dataset: {}, scrollTop: 0,
          classList: {toggle() {}}, replaceChildren(...children) {this.children = children; this.scrollTop = 0;},
          append(...children) {this.children.push(...children);}, appendChild(child) {this.children.push(child);},
          setAttribute() {}, addEventListener() {}, focus() {},
        }; }
        const elements = new Map();
        const $ = id => {if (!elements.has(id)) elements.set(id, node()); return elements.get(id);};
        const grid = node(), belt = node(), map = node();
        const document = {createElement: node, querySelectorAll: () => [...grid.children, ...belt.children]};
        function button(text, className, action) {return {...node(), className, click: action};}
        function artwork() {return node();}
        function updateTabs() {} function stopMapMotion() {} function positionMap() {} function clampMap() {}
        function renderDetailHero() {} function loadDetail() {} function status() {} function hideSearchKeyboard() {}
        function renderQuickLinks() {} function captureCover() {return null;}
        function cancelDetailMotion() {} function animateDetailIn() {} function animateDetailOut() {}
        function clearSelection() {selected = null; $('explore-browser').hidden = false;}
        let mode = 'explore', layout = 'grid', selected = null, selectedFocusUri = null, playPending = false;
        let section = 'all', page = 2, gridScrollTop = 720, drag = null, mapCovers = [], movedUntil = 0;
        let loading = false, loadError = '', zoom = 1, panX = 0, panY = 0;
        let selectedMetadata = null, detailMotion = null, restoringCoverFocus = false, personalStatus = null;
        const pointers = new Map(), searchKeyboard = {isOpen: () => false};
        const items = Array.from({length: 120}, (_, index) => ({uri: `spotify:album:${index}`, title: `Record ${index}`, sections: ['saved']}));
        function results() {return filterLibraryItems(items, {section, query: $('explore-search').value});}
        """,
        _explorer_function("render"),
        _explorer_function("showDetail"),
        _explorer_function("backToBrowser"),
        """
        render();
        assert.equal(grid.children.length, 120, 'every record must be reachable by scrolling without changing page');
        assert.equal(grid.children.at(-1).dataset.uri, 'spotify:album:119');
        assert($('explore-prev').hidden && $('explore-next').hidden);
        assert($('explore-prev').disabled && $('explore-next').disabled);
        assert.equal($('explore-count').textContent, '120 records');
        assert.equal(grid.scrollTop, 720);
        grid.scrollTop = 1460;
        grid.children.at(-1).click({currentTarget: grid.children.at(-1)});
        assert.equal(selected.uri, 'spotify:album:119', 'a record beyond the old page limit must open normally');
        grid.scrollTop = 0;
        backToBrowser();
        assert.equal(grid.scrollTop, 1460, 'returning from details must retain the browsing position');
        render();
        assert.equal(grid.scrollTop, 1460, 'a collection refresh must not send the user back to the top');
        $('explore-search').value = 'Record 119'; gridScrollTop = 0;
        render();
        assert.equal(grid.children.length, 1);
        assert.equal($('explore-count').textContent, '1 record');
        $('explore-search').value = ''; layout = 'mosaic'; page = 2;
        render();
        assert.equal(belt.children.length, 37, 'mosaic retains its bounded cover field');
        assert(!$('explore-prev').hidden && !$('explore-next').hidden);
        assert.equal($('explore-count').textContent, '120 records · 3 of 4');
        """,
    )))


def test_metadata_refreshes_use_current_titles_artwork_and_source_membership():
    _run_explorer("""
        const uri = 'spotify:album:same';
        const before = lib.buildLibraryItems({sections: [{id: 'saved', items: [
          {id: 'unchanged-id', uri, title: 'Old title', subtitle: 'Old artist', image: '/old.png'}
        ]}]});
        const after = lib.buildLibraryItems({sections: [{id: 'recent', items: [
          {id: 'unchanged-id', uri, title: 'New title', subtitle: 'New artist', image: '/new.png'}
        ]}]});
        assert.equal(after[0].title, 'New title');
        assert.equal(after[0].subtitle, 'New artist');
        assert.equal(after[0].image, '/new.png');
        assert.deepEqual(after[0].sections, ['recent']);
        assert.equal(lib.filterLibraryItems(after, {section: 'all', query: 'Old title'}).length, 0);
        assert.equal(lib.filterLibraryItems(after, {section: 'all', query: 'New title'}).length, 1);
        assert.equal(before[0].title, 'Old title');
        assert.deepEqual(lib.buildLibraryItems({sections: []}), [], 'an empty account library must clear the explorer');
    """)


def test_honeycomb_gives_each_visible_cover_a_stable_finite_position():
    _run_explorer("""
        const positions = Array.from({length: 37}, (_, index) => lib.libraryHoneycombPosition(index));
        assert(positions.every(({x, y}) => Number.isFinite(x) && Number.isFinite(y)));
        assert.equal(new Set(positions.map(({x, y}) => `${x}:${y}`)).size, 37,
          'every cover needs its own selectable location');
        positions.forEach((point, index) => assert.deepEqual(lib.libraryHoneycombPosition(index), point));
    """)


def test_mosaic_taps_select_once_but_drags_and_cancelled_pointers_do_not():
    _run_explorer("\n".join((
        """
        let movedUntil = 0, panX = 0, panY = 0, zoom = 1, drag = null, mapMotion = null;
        const pointers = new Map();
        const map = {classList: {remove() {}, add() {}}, setPointerCapture() {}};
        const mapCovers = [], coasts = [];
        let stops = 0;
        function stopMapMotion() {stops++; mapMotion = null;}
        function startMapMotion(vx,vy) {coasts.push({vx,vy}); mapMotion = {vx,vy};}
        function clampMap() {} function positionMap() {}
        function mapPoint(event) {return {x: event.clientX, y: event.clientY};}
        const items = [{uri: 'spotify:album:one'}];
        const opened = [];
        function showDetail(item) {opened.push(item.uri);}
        const performance = {now: () => 1000};
        """,
        _explorer_function("pointerGeometry"),
        _explorer_function("beginMapPointer"),
        _explorer_function("endMapPointer"),
        """
        pointers.set(1, {x: 10, y: 10});
        drag = {moved: false, uri: items[0].uri};
        endMapPointer({pointerId: 1, type: 'pointerup'});
        assert.deepEqual(opened, [items[0].uri]);
        assert(movedUntil > performance.now(), 'the native click following pointerup must be suppressed');
        endMapPointer({pointerId: 1, type: 'lostpointercapture'});
        assert.equal(opened.length, 1);
        for (const [type, moved] of [['pointerup', true], ['pointercancel', false], ['lostpointercapture', false]]) {
          const previousCoasts = coasts.length;
          movedUntil = 0;
          pointers.set(2, {x: 10, y: 10});
          drag = {moved, uri: items[0].uri};
          endMapPointer({pointerId: 2, type});
          assert.equal(opened.length, 1, `${type} with moved=${moved} must not select a record`);
          assert.equal(coasts.length, previousCoasts + (type === 'pointerup' ? 1 : 0),
            'cancelled input must never start inertia');
        }
        movedUntil = 0;
        pointers.set(3, {x: 10, y: 10});
        pointers.set(4, {x: 30, y: 30});
        drag = {moved: true, uri: items[0].uri};
        endMapPointer({pointerId: 3, type: 'pointerup'});
        endMapPointer({pointerId: 4, type: 'pointerup'});
        assert.equal(opened.length, 1, 'the last finger of a pinch must not activate a cover');
        assert.deepEqual(coasts.at(-1), {vx: 0, vy: 0}, 'a pinch release must not fling the covers');

        movedUntil = 0;
        mapMotion = {vx: 600, vy: 0};
        const cover = {dataset: {uri: items[0].uri}, classList: {add() {}}};
        beginMapPointer({pointerId: 5, clientX: 0, clientY: 0, target: {closest: () => cover}, preventDefault() {}});
        assert.equal(mapMotion, null, 'touching a coasting view must stop it immediately');
        assert(drag.moved, 'a touch that catches moving covers must not become a tap');
        endMapPointer({pointerId: 5, type: 'pointerup'});
        assert.equal(opened.length, 1);
        assert.deepEqual(coasts.at(-1), {vx: 0, vy: 0});

        pointers.set(6, {x: 0, y: 0});
        drag = {moved: true, pinched: false, lastTime: 990, vx: 400, vy: -200};
        endMapPointer({pointerId: 6, type: 'pointerup'});
        assert.deepEqual(coasts.at(-1), {vx: 400, vy: -200}, 'a recent one-finger flick keeps its release velocity');
        const motion = mapMotion;
        endMapPointer({pointerId: 6, type: 'lostpointercapture'});
        assert.equal(mapMotion, motion, 'automatic capture loss after pointerup must not cancel the new coast');

        pointers.set(7, {x: 0, y: 0});
        drag = {moved: true, pinched: false, lastTime: 0, vx: 1200, vy: -800};
        endMapPointer({pointerId: 7, type: 'pointerup'});
        assert.deepEqual(coasts.at(-1), {vx: 0, vy: 0}, 'holding a drag before release must discard old flick velocity');
        """,
    )))


def test_close_and_immediate_reopen_start_a_fresh_library_request():
    _run_lifecycle("""
      const old = refreshLibrary();
      assert.equal(requests.length, 1);
      close(false);
      assert.equal(mapStops, 1, 'closing the modal must stop cover motion');
      assert(requests[0].options.signal.aborted);
      mode = 'explore';
      const fresh = refreshLibrary();
      assert.equal(requests.length, 2, 'reopening must not wait for the abandoned request');
      requests[0].reject(aborted());
      await old;
      assert(loading, 'an old finally block must not finish the new loading state');
      assert(refreshing, 'an old finally block must not detach the new request');
      assert.equal(loadError, '', 'an intentional close must not become a timeout error on reopen');
      requests[1].resolve(response({sections: [{id: 'saved', items: [
        {uri: 'spotify:album:fresh', title: 'Fresh album'}
      ]}]}));
      await fresh;
      assert.equal(items[0].title, 'Fresh album');
      assert.equal(loading, false);
      assert.equal(refreshing, null);
      assert.equal(timers.size, 1, 'only the current periodic refresh should remain scheduled');
    """)


def test_building_library_retry_resumes_periodic_refresh_after_it_finishes():
    _run_lifecycle("""
      const building = refreshLibrary();
      requests[0].resolve(response({sections: [], building: true}));
      await building;
      assert.equal(timers.size, 1);
      const timer = timers.get(refreshTimer);
      assert.equal(timer.delay, 3000);
      timers.delete(refreshTimer);
      timer.callback();
      assert.equal(requests.length, 2);
      requests[1].resolve(response({sections: [], building: false}));
      await refreshing;
      assert.equal(timers.size, 1);
      assert.equal(timers.get(refreshTimer).delay, 30000,
        'the short building timer must not prevent future library updates');
    """)


def test_switching_selected_records_prevents_late_track_and_album_fact_publication():
    _run_lifecycle("""
      selected = {uri: 'spotify:album:old', title: 'Old record'};
      const old = loadDetail();
      selected = {uri: 'spotify:album:new', title: 'New record'};
      const fresh = loadDetail();
      assert(requests[0].options.signal.aborted);
      requests[0].resolve(response({kind: 'album', album_metadata: {label: 'Old label'}, tracks: [{uri: 'spotify:track:old', name: 'Old track'}]}));
      await old;
      assert.equal($('detail-tracks').children.length, 0);
      assert.equal($('detail-status').textContent, 'Loading tracks…');
      assert.equal($('detail-metadata').textContent, '');
      requests[1].resolve(response({kind: 'album', album_metadata: {release_date: '2025', release_date_precision: 'year', total_tracks: 1}, tracks: [{uri: 'spotify:track:new', name: 'New track'}]}));
      await fresh;
      assert.equal($('detail-tracks').children.length, 1);
      assert.equal($('detail-tracks').children[0].children[1].textContent, 'New track');
      assert.equal($('detail-metadata').textContent, 'Released 2025. 1 track.');
      backToBrowser();
      assert.equal($('detail-metadata').textContent, '');
      assert.equal(selectedMetadata, null);
    """)


@pytest.mark.parametrize("library_status", [200, 401, 403])
def test_library_removal_clears_private_details_during_pending_playback(library_status):
    _run_lifecycle(f"const libraryStatus = {library_status};\n" + """
      selected = {uri: 'spotify:album:private', title: 'Private record'};
      selectedFocusUri = selected.uri;
      items = [{...selected, sections: ['saved']}];
      libraryFingerprint = JSON.stringify(items);
      $('detail-title').textContent = selected.title;
      $('detail-subtitle').textContent = 'Private artist';
      $('explore-detail').hidden = false;
      const oldPlay = playSelection();
      assert(playPending);
      backToBrowser();
      assert(selected, 'ordinary back remains blocked until playback settles');
      const refresh = refreshLibrary();
      requests[1].resolve(response({sections: []}, libraryStatus));
      await refresh;
      assert.equal(selected, null, 'account removal must clear selection even during playback');
      assert.equal(selectedFocusUri, null);
      assert(requests[0].options.signal.aborted);
      assert.equal($('detail-title').textContent, '');
      assert.equal($('detail-subtitle').textContent, '');
      assert.equal($('detail-tracks').children.length, 0);
      assert($('explore-detail').hidden);

      selected = {uri: 'spotify:album:next', title: 'Next record'};
      const newPlay = playSelection();
      assert(playPending);
      requests[0].reject(aborted());
      await oldPlay;
      assert(playPending, 'an old play request must not unlock the newer attempt');
      assert($('detail-play').disabled);
      requests[2].resolve(response({error: 'Player is reconnecting'}, 503));
      await newPlay;
      assert.equal(playPending, false);
      assert.equal($('detail-play').disabled, false);
      assert.equal($('detail-status').textContent, 'Player is reconnecting');
      assert.equal(played, 0);
    """)


def test_vinyl_filter_keeps_owned_saved_albums_in_both_layout_data_sources():
    _run_explorer("""
      const owned = {uri:'spotify:album:owned', title:'White Pony', subtitle:'Deftones'};
      const records = lib.buildLibraryItems({sections:[
        {id:'saved', items:[owned, {uri:'spotify:album:digital',title:'Digital only'}]},
        {id:'vinyls',items:[owned,{uri:'spotify:album:goose',title:'Untitled Goose Game',subtitle:'Dan Golding',playable:false}]}
      ]});
      const vinyls=lib.filterLibraryItems(records,{section:'vinyls'});
      assert.equal(vinyls.length,2);
      assert.equal(lib.filterLibraryItems(records,{section:'vinyls',query:'deftones white'}).length,1);
      assert.equal(lib.discoveryReason(vinyls[0]),'From your vinyl collection');
      assert.deepEqual(lib.pageLibraryItems(vinyls,0,37).items,vinyls);
      assert.equal(vinyls[1].playable,false);
    """)


def test_queue_album_keeps_browser_open_and_blocks_duplicate_taps():
    _run_lifecycle("""
      selected={uri:'spotify:album:owned',title:'White Pony'};
      $('explore-browser').hidden=true;$('explore-detail').hidden=false;
      const pending=playSelection(undefined,true);
      await playSelection(undefined,true);
      assert.equal(requests.length,1);
      assert.equal(requests[0].url,'/api/library/queue');
      requests[0].resolve(response({status:'ok',queued:12,of:12}));
      await pending;
      assert.equal(mode,'explore');assert.equal(played,0);
      assert.equal(selected.title,'White Pony');
      assert.equal($('explore-browser').hidden,true);
      assert.equal($('explore-detail').hidden,false);
      assert.equal(renders,0,'queueing must not return through the cover mosaic');
      assert.match($('detail-status').textContent,/Album queued.*12 tracks/);
      assert.equal(playPending,false);
      assert.equal($('detail-queue').disabled,false);
    """)


@pytest.mark.parametrize("track_uri", [None, "spotify:track:chosen"])
@pytest.mark.parametrize("in_flight_cover", [False, True])
def test_play_carries_selected_art_directly_to_player_after_handoff(track_uri, in_flight_cover):
    _run_lifecycle("\n".join((
        f"const trackUri = {json.dumps(track_uri)};",
        f"const inFlightCover = {json.dumps(in_flight_cover)};",
        """
      const item={uri:'spotify:album:owned',title:'White Pony'};
      selected=item;
      $('explore-browser').hidden=true;$('explore-detail').hidden=false;
      const sleeve=node();
      capturedCover={rect:{left:160,top:330,width:360,height:360},radius:.04,art:sleeve};
      const sourceElement=inFlightCover ? node() : $('detail-art');
      if(inFlightCover) detailMotion={ghost:sourceElement};
      let handoff=null,finishHandoff;
      bridge.onPlayed=payload=>{handoff=payload;played++;return new Promise(resolve=>{finishHandoff=resolve;});};
      const pending=playSelection(trackUri || undefined);
      assert.equal(requests[0].url,'/api/library/play');
      assert.deepEqual(JSON.parse(requests[0].options.body),{uri:item.uri,...(trackUri?{track_uri:trackUri}:{})});
      requests[0].resolve(response({status:'ok'}));
      await new Promise(setImmediate);
      assert.equal(played,1);
      assert.equal(capturedElement,sourceElement,'Play during the detail arrival must use the visible sleeve');
      assert.equal(handoff.source,capturedCover);
      assert.equal(handoff.source.art,sleeve);
      assert.equal(handoff.item,item);
      assert.equal(handoff.trackUri,trackUri || undefined);
      assert.equal(handoff.signal,requests[0].options.signal);
      assert.equal(cancelledDetailMotions,1,'the detail and player must not fly two copies simultaneously');
      assert.equal(selected,item,'keep the detail view until the player accepts the artwork');
      assert.equal(mode,'explore');assert.equal(playPending,true);
      assert.equal($('explore-browser').hidden,true);
      assert.equal($('explore-detail').hidden,false);
      assert.equal(timers.size,0,'the completed request must not time out during its artwork handoff');
      await playSelection(trackUri || undefined);
      assert.equal(requests.length,1,'a second tap during the animation must not replay the album');
      finishHandoff();await pending;
      assert.equal(mode,null);assert.equal(selected,null);
      assert.equal(explore.hidden,true);
      assert.equal(playPending,false);
      assert.equal(renders,0,'successful Play must close directly without rebuilding the mosaic');
    """)))


@pytest.mark.parametrize("queue", [False, True])
def test_failed_play_or_queue_keeps_selected_detail_and_never_hands_off_art(queue):
    _run_lifecycle("\n".join((f"const queue = {json.dumps(queue)};", """
      const item={uri:'spotify:album:owned',title:'White Pony'};selected=item;
      $('explore-browser').hidden=true;$('explore-detail').hidden=false;
      const pending=playSelection(undefined,queue);
      requests[0].resolve(response({error:'Choose Pi Display in Spotify first.'},503));
      await pending;
      assert.equal(selected,item);assert.equal(mode,'explore');
      assert.equal($('explore-browser').hidden,true);
      assert.equal($('explore-detail').hidden,false);
      assert.equal(played,0);assert.equal(capturedElement,null);
      assert.equal(cancelledDetailMotions,0);assert.equal(renders,0);
      assert.match($('detail-status').textContent,/Choose Pi Display/);
      assert.equal(playPending,false);
      assert.equal($('detail-play').disabled,false);
      assert.equal($('detail-back').disabled,false);
    """)))


def test_profile_change_cancels_pending_play_handoff_without_closing_a_new_selection():
    _run_lifecycle("""
      bridge.getProfile=()=>({epoch:'profile-a',revision:1});
      bridge.acceptProfile=(data,revision)=>revision===1 && data.profile_epoch==='profile-a';
      libraryProfileEpoch='profile-a';
      selected={uri:'spotify:album:private',title:'Private album'};
      $('explore-browser').hidden=true;$('explore-detail').hidden=false;
      let handoff=null,finishHandoff;
      bridge.onPlayed=payload=>{handoff=payload;return new Promise(resolve=>{finishHandoff=resolve;});};
      const pending=playSelection();
      requests[0].resolve(response({status:'ok',profile_state:'linked',profile_epoch:'profile-a'}));
      await new Promise(setImmediate);
      assert(handoff);assert.equal(handoff.signal.aborted,false);
      bridge.getProfile=()=>({epoch:'profile-b',revision:2});
      invalidateProfile();
      assert.equal(handoff.signal.aborted,true,'profile changes must immediately cancel the flying private artwork');
      assert.equal(selected,null);assert.equal(playPending,false);
      const newItem={uri:'spotify:album:new-profile',title:'New profile album'};
      selected=newItem;libraryProfileEpoch='profile-b';
      $('explore-browser').hidden=true;$('explore-detail').hidden=false;
      const rendersAfterChange=renders;
      finishHandoff();await pending;
      assert.equal(selected,newItem,'the old animation completion must not clear a new selection');
      assert.equal(mode,'explore');assert.equal(explore.hidden,false);
      assert.equal($('explore-browser').hidden,true);
      assert.equal($('explore-detail').hidden,false);
      assert.equal(renders,rendersAfterChange);
    """)


def test_partial_queue_and_unavailable_vinyl_never_report_successful_playback():
    _run_lifecycle("""
      selected={uri:'spotify:album:owned',title:'White Pony'};
      const pending=playSelection(undefined,true);
      requests[0].resolve(response({status:'partial',queued:2,of:12}));await pending;
      assert.match($('detail-status').textContent,/Added 2 of 12 tracks/);
      assert.equal(mode,'explore');assert.equal(played,0);
      selected={uri:'spotify:album:unavailable',playable:false,availability_note:'Unavailable in the UK'};
      await loadDetail();await playSelection();await playSelection(undefined,true);
      assert.equal(requests.length,1);
      assert.match($('detail-status').textContent,/Unavailable in the UK/);
    """)
