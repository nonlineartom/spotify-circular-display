"""Numerical contracts for cover magnification, anchored zoom and momentum."""

import json
from pathlib import Path
import subprocess


EXPLORER = Path(__file__).resolve().parents[1] / "static" / "explore.js"
PLAYER = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def _run_motion(source):
    script = "\n".join((
        'const assert = require("node:assert/strict");',
        f"const lib = require({json.dumps(str(EXPLORER))});",
        "function near(a,b,tolerance=1e-3) {assert(Math.abs(a-b)<=tolerance, `${a} differs from ${b}`);}",
        source,
    ))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def _motion_function(name, path=EXPLORER):
    source = path.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    brace = source.index("{", source.index(")", start))
    depth = 0
    for position in range(brace, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start:position + 1]
    raise AssertionError(f"Unterminated function: {name}")


def _run_player_motion(source):
    """Run the player handoff with deferred browser animations and no network."""
    setup = """
      let libraryPlaybackMotion=null,REDUCED_MOTION=false,clock=0,prepared=0,ready=true;
      const performance={now:()=>clock},polls=[],animations=[],ghosts=[];
      function poll(reason){polls.push(reason);}
      function prepareExplorerPlayback(){prepared++;}
      function explorerPlaybackArtworkReady(){return ready;}
      function animation(keyframes,options){
        let finish,reject;
        const finished=new Promise((resolve,fail)=>{finish=resolve;reject=fail;});
        finished.catch(()=>{});
        return {keyframes,options,finished,finish,cancelled:false,cancel(){
          this.cancelled=true;reject(Object.assign(new Error('Cancelled'),{name:'AbortError'}));
        }};
      }
      function node(){return {style:{},children:[],removed:false,classes:new Set(),
        setAttribute(){},appendChild(child){this.children.push(child);},remove(){this.removed=true;},
        animate(keyframes,options){const active=animation(keyframes,options);animations.push(active);return active;},
        get classList(){const classes=this.classes;return {
          add(...values){values.forEach(value=>classes.add(value));},
          remove(...values){values.forEach(value=>classes.delete(value));},
          contains(value){return classes.has(value);},
        };},
      };}
      const viewport=node(),modal=node(),drawer=node();
      viewport.clientWidth=1080;viewport.clientHeight=1080;
      const rect={left:20,top:40,width:1080,height:1080};
      viewport.getBoundingClientRect=()=>rect;
      const $=id=>id==='explore-modal'?modal:drawer;
      const document={createElement(){const ghost=node();ghosts.push(ghost);return ghost;}};
      let timerId=0;const timers=new Map();
      function setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,delay});return id;}
      function tickTimer(delay){
        const entry=[...timers].find(([,timer])=>timer.delay===delay);
        assert(entry,`expected a ${delay}ms timer`);
        timers.delete(entry[0]);clock+=delay;entry[1].callback();
      }
      const item={uri:'spotify:album:chosen'};
      function sleeve(scale=1){return {rect:{left:20+160*scale,top:40+330*scale,width:360*scale,height:360*scale},
        radius:15/360,art:{privateArtwork:'selected sleeve'}};}
      const flush=()=>new Promise(setImmediate);
    """
    _run_motion("\n".join((setup, _motion_function("transitionExplorerPlayback", PLAYER),
        "(async()=>{", source,
        "})().catch(error=>{console.error(error);process.exitCode=1;});",
    )))


def test_lens_centre_is_finite_and_mirrored_covers_have_equal_size():
    _run_motion("""
      const centre = lib.projectLibraryCover({x: 0, y: 0});
      assert.deepEqual(centre, {x: 0, y: 0, scale: 1, visible: true});
      for (const point of [{x: 100, y: 80}, {x: 250, y: 110}, {x: 600, y: 400}]) {
        const right = lib.projectLibraryCover(point);
        const left = lib.projectLibraryCover({x: -point.x, y: -point.y});
        for (const property of ['x', 'y', 'scale']) assert(Number.isFinite(right[property]));
        near(left.x, -right.x);
        near(left.y, -right.y);
        near(left.scale, right.scale);
        assert.equal(left.visible, right.visible);
        assert(right.scale >= 0 && right.scale <= 1);
      }
    """)


def test_the_same_cover_grows_smoothly_as_panning_brings_it_to_the_centre():
    _run_motion("""
      const point = {x: 420, y: 0};
      let previousScale = -1;
      let previousX = Infinity;
      for (let panX = 0; panX >= -420; panX -= 10) {
        const cover = lib.projectLibraryCover(point, {panX});
        assert(cover.scale >= previousScale, 'approaching the centre must not make a cover smaller');
        assert(cover.x <= previousX, 'panning must move the cover continuously toward the centre');
        if (previousScale >= 0) assert(cover.scale - previousScale < .06, 'magnification must not jump between frames');
        previousScale = cover.scale;
        previousX = cover.x;
      }
      const edge = lib.projectLibraryCover(point);
      const centred = lib.projectLibraryCover(point, {panX: -420});
      assert(centred.scale > edge.scale * 1.5, 'the lens must visibly magnify the selected region');
      near(centred.x, 0);
      near(centred.scale, 1);
      assert.equal(lib.projectLibraryCover({x: 5000, y: 5000}).visible, false);
    """)


def test_visible_cover_projection_can_be_inverted_for_drag_and_pinch_anchors():
    _run_motion("""
      for (const lens of [{width: 760, height: 450}, {width: 728, height: 418}, {width: 1020, height: 740}, {width: 988, height: 708}]) for (const view of [{panX: 0, panY: 0, zoom: 1}, {panX: -80, panY: 35, zoom: .6}, {panX: 90, panY: -55, zoom: 1.5}]) {
        for (let index = 0; index < 19; index++) {
          const point = lib.libraryHoneycombPosition(index);
          const projected = lib.projectLibraryCover(point, {...view, ...lens});
          if (!projected.visible) continue;
          const raw = lib.unprojectLibraryPoint(projected, {...view, ...lens});
          near(raw.x, point.x * view.zoom + view.panX);
          near(raw.y, point.y * view.zoom + view.panY);
        }
      }
    """)


def test_visible_circles_stay_separate_as_the_honeycomb_moves_and_magnifies():
    _run_motion("""
      for (const lens of [{width: 760, height: 450}, {width: 728, height: 418}, {width: 1020, height: 740}, {width: 988, height: 708}]) for (const zoom of [.6, 1, 1.5]) for (const panX of [-332, -166, 0, 166, 332]) for (const panY of [-288, -144, 0, 144, 288]) {
        const covers = Array.from({length: 37}, (_, index) =>
          lib.projectLibraryCover(lib.libraryHoneycombPosition(index), {panX, panY, zoom, ...lens}));
        for (let i = 0; i < covers.length; i++) for (let j = i + 1; j < covers.length; j++) {
          const a = covers[i], b = covers[j];
          if (!a.visible || !b.visible) continue;
          const distance = Math.hypot(a.x - b.x, a.y - b.y);
          const combinedRadius = 72 * (a.scale + b.scale); // Each cover button is 144px across.
          assert(distance >= combinedRadius * .98,
            `covers ${i} and ${j} overlap at zoom ${zoom} and pan ${panX},${panY}`);
        }
      }
    """)


def test_zoom_keeps_the_off_centre_content_anchor_fixed_even_at_zoom_limits():
    _run_motion("""
      const view = {panX: -50, panY: 20, zoom: 1};
      const point = {x: 235, y: 72};
      for (const lens of [{width: 760, height: 450}, {width: 728, height: 418}, {width: 1020, height: 740}, {width: 988, height: 708}]) for (const reduceMotion of [false, true]) {
        const options = {...lens, reduceMotion};
        const anchor = lib.projectLibraryCover(point, {...view, ...options});
        for (const nextZoom of [.01, .8, 1.3, 10]) {
          const next = lib.zoomLibraryAt(view, nextZoom, anchor, options);
          assert(next.zoom >= .6 && next.zoom <= 1.5);
          const projected = lib.projectLibraryCover(point, {...next, ...options});
          near(projected.x, anchor.x);
          near(projected.y, anchor.y);
        }
      }
    """)


def test_reduced_motion_keeps_cover_size_uniform_while_preserving_panning():
    _run_motion("""
      const options = {panX: 30, panY: -20, zoom: .8, reduceMotion: true};
      for (const point of [{x: 0, y: 0}, {x: 300, y: 150}]) {
        const cover = lib.projectLibraryCover(point, options);
        near(cover.x, point.x * .8 + 30);
        near(cover.y, point.y * .8 - 20);
        near(cover.scale, .8);
        const inverse = lib.unprojectLibraryPoint(cover, options);
        near(inverse.x, cover.x);
        near(inverse.y, cover.y);
      }
    """)


def test_inertial_travel_is_consistent_across_fast_and_slow_frame_rates():
    _run_motion("""
      const bounds = {minX: -10000, maxX: 10000, minY: -10000, maxY: 10000};
      function advance(frame) {
        let motion = {x: 0, y: 0, vx: 400, vy: -250};
        let elapsed = 0;
        while (elapsed < .7) {
          const dt = Math.min(frame, .7 - elapsed);
          const next = lib.stepLibraryMotion(motion, bounds, dt);
          assert(Math.hypot(next.vx, next.vy) <= Math.hypot(motion.vx, motion.vy));
          motion = next; elapsed += dt;
        }
        return motion;
      }
      const fast = advance(.016), slow = advance(.033);
      for (const property of ['x', 'y', 'vx', 'vy']) near(fast[property], slow[property], 1e-8);
      assert(fast.x > 0 && fast.y < 0);
      assert(fast.x < 80, 'drag friction must bound the remaining travel');
    """)


def test_elastic_edges_settle_inside_bounds_without_an_endless_animation():
    _run_motion("""
      const bounds = {minX: -100, maxX: 100, minY: -80, maxY: 80};
      for (const initial of [
        {x: 160, y: -130, vx: 0, vy: 0},
        {x: 90, y: -70, vx: 1200, vy: -800},
      ]) {
        let motion = initial, frames = 0;
        while (!motion.done && frames < 300) {
          motion = lib.stepLibraryMotion(motion, bounds, .016);
          for (const property of ['x', 'y', 'vx', 'vy']) assert(Number.isFinite(motion[property]));
          assert(Math.abs(motion.x) < 400 && Math.abs(motion.y) < 350, 'edge overshoot must remain bounded');
          frames++;
        }
        assert(motion.done, 'motion must stop within a few seconds');
        assert(motion.x >= bounds.minX && motion.x <= bounds.maxX);
        assert(motion.y >= bounds.minY && motion.y <= bounds.maxY);
        assert.equal(motion.vx, 0); assert.equal(motion.vy, 0);
        assert.deepEqual(lib.stepLibraryMotion(motion, bounds, .016), motion, 'a settled view must not restart itself');
      }
    """)


def test_long_frame_delays_do_not_jump_the_collection_on_resume():
    _run_motion("""
      const motion = {x: 20, y: -10, vx: 800, vy: -300};
      const bounds = {minX: -1000, maxX: 1000, minY: -1000, maxY: 1000};
      const resumed = lib.stepLibraryMotion(motion, bounds, 60);
      const bounded = lib.stepLibraryMotion(motion, bounds, .034);
      assert.deepEqual(resumed, bounded);
      const backwardsClock = lib.stepLibraryMotion(motion, bounds, -1);
      assert.equal(backwardsClock.x, motion.x);
      assert.equal(backwardsClock.y, motion.y);
    """)


def test_motion_uses_one_frame_and_stops_for_hidden_or_replaced_views():
    setup = """
      const {stepLibraryMotion} = lib;
      let mode = 'explore', selected = null, layout = 'mosaic', searchOpen = false;
      const searchKeyboard = {isOpen: () => searchOpen};
      const document = {hidden: false};
      const reducedMotion = {matches: false};
      const map = {classList: {remove() {}}};
      const mapCovers = [{el: {classList: {remove() {}}}}];
      let mapFrame = null, mapMotion = null, mapFrameTime = 0;
      let panX = 0, panY = 0, zoom = 1, movedUntil = 0;
      let clock = 1000, nextId = 0, draws = 0, clamps = 0;
      const performance = {now: () => clock};
      const frames = new Map();
      function requestAnimationFrame(callback) {const id = ++nextId; frames.set(id, callback); return id;}
      function cancelAnimationFrame(id) {frames.delete(id);}
      function mapBounds() {return {minX: -1000, maxX: 1000, minY: -1000, maxY: 1000};}
      function positionMap() {draws++;}
      function clampMap() {clamps++;}
      function tick() {
        assert.equal(frames.size, 1);
        const [id, callback] = frames.entries().next().value;
        frames.delete(id); clock += 16; callback(clock);
      }
    """
    functions = "\n".join(_motion_function(name) for name in (
        "stopMapMotion", "queueMapFrame", "animateMapFrame", "startMapMotion",
    ))
    _run_motion("\n".join((setup, functions, """
      startMapMotion(800, 0);
      queueMapFrame(); queueMapFrame();
      assert.equal(frames.size, 1, 'pointer updates must share one queued frame');
      tick();
      assert(panX > 0);
      assert.equal(frames.size, 1);
      stopMapMotion();
      assert.equal(frames.size, 0, 'interruption must cancel the pending frame immediately');
      assert.equal(mapMotion, null);
      assert.equal(mapFrame, null);

      for (const hide of [
        () => {mode = 'controls';},
        () => {selected = {uri: 'spotify:album:detail'};},
        () => {layout = 'grid';},
        () => {searchOpen = true;},
        () => {document.hidden = true;},
      ]) {
        mode = 'explore'; selected = null; layout = 'mosaic'; searchOpen = false; document.hidden = false;
        startMapMotion(400, -100);
        const previousDraws = draws;
        hide(); tick();
        assert.equal(frames.size, 0, 'a hidden collection must not keep scheduling frames');
        assert.equal(mapMotion, null);
        assert.equal(draws, previousDraws, 'an inactive view must not mutate cover transforms');
      }
      mode = 'explore'; selected = null; layout = 'mosaic'; searchOpen = false; document.hidden = false;
      reducedMotion.matches = true;
      startMapMotion(800, -200);
      assert.equal(frames.size, 0, 'reduced motion must not start an inertial animation');
      assert.equal(mapMotion, null);
      assert.equal(clamps, 1);
    """)))


def test_pointer_coordinates_and_lens_follow_the_map_at_half_display_scale():
    setup = """
      const reducedMotion = {matches: false};
      const rect = {left: 40, top: 120, width: 510, height: 370};
      const map = {clientWidth: 1020, clientHeight: 740, getBoundingClientRect: () => rect};
    """
    functions = "\n".join(_motion_function(name) for name in (
        "lensOptions", "mapPoint",
    ))
    _run_motion("\n".join((setup, functions, """
      assert.deepEqual(lensOptions(), {width: 988, height: 708, reduceMotion: false});
      const centre = mapPoint({clientX: 295, clientY: 305});
      near(centre.x, 0); near(centre.y, 0);
      const moved = mapPoint({clientX: 345, clientY: 280});
      near(moved.x - centre.x, 100); // A 50px screen drag must move 100 local pixels.
      near(moved.y - centre.y, -50);
      const corner = mapPoint({clientX: rect.left, clientY: rect.top});
      near(corner.x, -510); near(corner.y, -370);

      map.clientWidth = 760; map.clientHeight = 450;
      rect.width = 380; rect.height = 225;
      assert.deepEqual(lensOptions(), {width: 728, height: 418, reduceMotion: false});
      const resizedCentre = mapPoint({clientX: 230, clientY: 232.5});
      near(resizedCentre.x, 0); near(resizedCentre.y, 0);
      const resizedMoved = mapPoint({clientX: 280, clientY: 207.5});
      near(resizedMoved.x, 100); near(resizedMoved.y, -50);
    """)))


def test_pinch_keeps_its_content_anchor_and_rebases_the_remaining_finger():
    setup = """
      const {projectLibraryCover, unprojectLibraryPoint, zoomLibraryAt} = lib;
      const reducedMotion = {matches: false};
      let panX = 0, panY = 0, zoom = 1, drag = null, movedUntil = 0, mapMotion = null;
      const pointers = new Map(), mapCovers = [], items = [], coasts = [];
      const map = {clientWidth: 1020, clientHeight: 740, classList: {add() {}, remove() {}}, setPointerCapture() {}};
      let clock = 1000;
      const performance = {now: () => clock};
      function mapPoint(event) {return {x: event.clientX, y: event.clientY};}
      function mapBounds() {return {minX: -10000, maxX: 10000, minY: -10000, maxY: 10000};}
      function stopMapMotion() {mapMotion = null;}
      function startMapMotion(vx,vy) {coasts.push({vx,vy});}
      function queueMapFrame() {} function clampMap() {} function positionMap() {}
      function showDetail() {throw new Error('a pinch must not select a record');}
      function event(pointerId, clientX, clientY, type = 'pointerdown') {
        return {pointerId, clientX, clientY, type, target: {closest: () => null}, preventDefault() {}};
      }
    """
    functions = "\n".join(_motion_function(name) for name in (
        "lensOptions", "pointerGeometry", "rubberMap", "beginMapPointer", "moveMapPointer", "endMapPointer",
    ))
    _run_motion("\n".join((setup, functions, """
      beginMapPointer(event(1, 100, 10));
      beginMapPointer(event(2, 200, 10));
      const anchor = unprojectLibraryPoint({x: 150, y: 10}, lensOptions());
      clock += 16; moveMapPointer(event(1, 50, 10, 'pointermove'));
      let projected = projectLibraryCover(anchor, {panX, panY, zoom, ...lensOptions()});
      near(projected.x, 125); near(projected.y, 10);
      clock += 16; moveMapPointer(event(2, 250, 10, 'pointermove'));
      assert.equal(zoom, 1.5, 'the pinch must respect its upper zoom bound');
      projected = projectLibraryCover(anchor, {panX, panY, zoom, ...lensOptions()});
      near(projected.x, 150); near(projected.y, 10);

      endMapPointer(event(1, 50, 10, 'pointerup'));
      assert.equal(pointers.size, 1);
      assert(drag.pinched && drag.moved);
      const before = {panX, panY, zoom};
      clock += 16; moveMapPointer(event(2, 251, 10, 'pointermove'));
      near(panX - before.panX, 1);
      near(panY, before.panY); near(zoom, before.zoom);
      endMapPointer(event(2, 251, 10, 'pointerup'));
      assert.deepEqual(coasts, [{vx: 0, vy: 0}], 'the last pinch contact must not introduce a fling');
    """)))


def test_shared_artwork_starts_at_the_tapped_cover_at_full_and_half_scale():
    setup = """
      let viewportScale = 1, ghost = null, frames = null;
      const viewport = {left: 20, top: 40, width: 1080};
      const destination = {left: 140, top: 332, width: 360, height: 360};
      const target = {offsetWidth: 360, style: {visibility: ''}, animate() {}, getBoundingClientRect: () => destination};
      const explore = {clientWidth: 1080, getBoundingClientRect: () => viewport, appendChild(element) {ghost = element;}};
      function getComputedStyle() {return {borderTopLeftRadius: '15px'};}
      const document = {createElement() {return {style: {}, setAttribute() {}, appendChild() {},
        animate(keyframes) {frames = keyframes; return {finished: Promise.resolve(), cancel() {}};}};}};
    """
    _run_motion("\n".join((setup, _motion_function("flyCover"), """
      for (const scale of [1, .5]) for (const diameter of [96, 196]) {
        viewport.width = 1080 * scale;
        Object.assign(destination, {left: 20 + 120 * scale, top: 40 + 292 * scale, width: 360 * scale, height: 360 * scale});
        const source = {rect: {left: 20 + 640 * scale, top: 40 + 390 * scale, width: diameter * scale, height: diameter * scale},
          radius: diameter === 96 ? .5 : 8 / diameter, art: {}};
        const motion = {animations: []};
        flyCover(source, target, motion);
        assert.equal(motion.animations.length, 1);
        const numbers = Array.from(frames[0].transform.matchAll(/-?\\d+(?:\\.\\d+)?/g), match => Number(match[0]));
        const [dx, dy, sx, sy] = numbers;
        // Reconstruct rendered screen bounds: neither device scaling nor the
        // circular mosaic lens may introduce a jump at the first frame.
        near(viewport.left + (parseFloat(ghost.style.left) + dx) * scale, source.rect.left);
        near(viewport.top + (parseFloat(ghost.style.top) + dy) * scale, source.rect.top);
        near(parseFloat(ghost.style.width) * sx * scale, source.rect.width);
        near(parseFloat(ghost.style.height) * sy * scale, source.rect.height);
        near(parseFloat(frames[0].borderRadius) / 100, source.radius);
        near(parseFloat(frames[1].borderRadius) / 100 * target.offsetWidth, 15);
        assert.equal(target.style.visibility, 'hidden', 'the destination must not duplicate the flying sleeve');
        assert.equal(motion.hiddenArt, target);
      }
    """)))


def test_cancelled_album_flight_cannot_clear_a_newer_flight_or_leave_private_art():
    setup = """
      let detailMotion = null, removed = 0, cancelled = 0;
      function flight() {
        let finish;
        const finished = new Promise(resolve => {finish = resolve;});
        return {animations: [{finished, cancel() {cancelled++;}}],
          hiddenArt: {style: {visibility: 'hidden'}}, visibility: '',
          ghost: {remove() {removed++;}}, finish};
      }
    """
    functions = "\n".join(_motion_function(name) for name in ("cancelDetailMotion", "settleDetailMotion"))
    _run_motion("\n".join((setup, functions, """
      (async () => {
        const old = flight(); detailMotion = old; settleDetailMotion(old);
        cancelDetailMotion();
        assert.equal(detailMotion, null);
        assert.equal(removed, 1, 'closing or changing profile removes cloned account artwork immediately');
        assert.equal(old.hiddenArt.style.visibility, '');
        const fresh = flight(); detailMotion = fresh; settleDetailMotion(fresh);
        old.finish(); await new Promise(setImmediate);
        assert.equal(detailMotion, fresh, 'a late completion callback must not cancel the next selected record');
        assert.equal(fresh.hiddenArt.style.visibility, 'hidden');
        assert.equal(removed, 1);
        fresh.finish(); await new Promise(setImmediate);
        assert.equal(detailMotion, null);
        assert.equal(fresh.hiddenArt.style.visibility, '');
        assert.equal(removed, 2); assert.equal(cancelled, 2);
        cancelDetailMotion();
        assert.equal(removed, 2, 'cleanup is safe to repeat');
      })().catch(error => {console.error(error); process.exitCode = 1;});
    """)))


def test_reduced_motion_skips_artwork_travel_and_track_drawer_animation():
    setup = """
      const reducedMotion = {matches: true};
      let detailMotion = null, cancels = 0;
      function cancelDetailMotion() {cancels++; detailMotion = null;}
      function $() {throw new Error('reduced motion must not create or inspect animated layers');}
      function flyCover() {throw new Error('reduced motion must not fly artwork');}
      function settleDetailMotion() {throw new Error('reduced motion must not wait for animation');}
    """
    functions = "\n".join(_motion_function(name) for name in ("animateDetailIn", "animateDetailOut"))
    _run_motion("\n".join((setup, functions, """
      animateDetailIn({rect: {width: 100}, art: {}});
      animateDetailOut({rect: {width: 360}, art: {}}, {animate() {throw new Error('unexpected animation');}});
      assert.equal(detailMotion, null); assert.equal(cancels, 1);
    """)))


def test_playback_flight_keeps_sleeve_position_at_full_and_half_display_scale():
    _run_player_motion("""
      for(const scale of [1,.5]){
        rect.width=rect.height=1080*scale;
        const source=sleeve(scale),controller=new AbortController();
        const before=animations.length;
        const pending=transitionExplorerPlayback({source,item,signal:controller.signal});
        const ghost=ghosts[ghosts.length-1],arrive=animations[before];
        assert.equal(ghost.children[0],source.art,'the selected sleeve must be carried onto the player');
        const [dx,dy,sx,sy]=Array.from(arrive.keyframes[0].transform.matchAll(/-?\\d+(?:\\.\\d+)?/g),match=>Number(match[0]));
        near(rect.left+dx*scale,source.rect.left);
        near(rect.top+dy*scale,source.rect.top);
        near(viewport.clientWidth*sx*scale,source.rect.width);
        near(viewport.clientHeight*sy*scale,source.rect.height);
        near(parseFloat(arrive.keyframes[0].borderRadius)/100,source.radius);
        assert.equal(arrive.keyframes[1].transform,'translate(0,0) scale(1)');
        assert.equal(arrive.keyframes[1].borderRadius,'50%','the sleeve must arrive at the circular playback disc');
        assert(viewport.classList.contains('library-play-entering'));
        assert(!ghost.removed);
        arrive.finish();await flush();
        assert(viewport.classList.contains('library-play-revealing'));
        const reveal=animations[before+3];
        assert.deepEqual(reveal.keyframes,[{opacity:1},{opacity:0}]);
        reveal.finish();await pending;
        assert(ghost.removed);assert.equal(libraryPlaybackMotion,null);
        assert(!viewport.classList.contains('library-play-entering'));
        assert(!viewport.classList.contains('library-play-revealing'));
        assert(animations.slice(before).every(active=>active.cancelled),'no completed animation may retain a private artwork layer');
      }
      assert.equal(prepared,2);assert.deepEqual(polls,['library-play','library-play']);
    """)


def test_aborted_playback_flight_removes_private_art_before_late_completion():
    _run_player_motion("""
      const oldController=new AbortController();
      const old=transitionExplorerPlayback({source:sleeve(),item,signal:oldController.signal});
      const oldMotion=libraryPlaybackMotion,oldGhost=ghosts[0],oldArrival=animations[0];
      oldController.abort();
      assert(oldGhost.removed,'a profile change must remove cloned artwork synchronously');
      assert.equal(libraryPlaybackMotion,null);
      assert(!viewport.classList.contains('library-play-entering'));
      assert(oldMotion.animations.every(active=>active.cancelled));
      const freshController=new AbortController();
      const fresh=transitionExplorerPlayback({source:sleeve(),item,signal:freshController.signal});
      const freshMotion=libraryPlaybackMotion,freshGhost=ghosts[1];
      oldArrival.finish();await old;
      assert.equal(libraryPlaybackMotion,freshMotion,'a stale animation completion must not cancel a new flight');
      assert(!freshGhost.removed);assert(viewport.classList.contains('library-play-entering'));
      freshController.abort();await fresh;
      assert(freshGhost.removed);assert.equal(libraryPlaybackMotion,null);
      assert(!viewport.classList.contains('library-play-entering'));
    """)


def test_player_holds_selected_sleeve_until_art_is_ready_and_abort_ends_hold():
    _run_player_motion("""
      ready=false;
      const controller=new AbortController();
      const pending=transitionExplorerPlayback({source:sleeve(),item,signal:controller.signal});
      animations[0].finish();await flush();
      assert.equal(animations.length,3,'the old playback cover must remain hidden while the new art decodes');
      assert(!viewport.classList.contains('library-play-revealing'));
      assert(!ghosts[0].removed);
      tickTimer(80);await flush();
      assert.equal(animations.length,3);
      controller.abort();
      assert(ghosts[0].removed,'abort must clear artwork even while waiting for a receiver poll');
      tickTimer(80);await pending;
      assert.equal(animations.length,3,'a cancelled handoff must never begin a late reveal');
      assert.equal(libraryPlaybackMotion,null);
    """)


def test_reduced_motion_and_already_aborted_playback_never_create_artwork_clones():
    _run_player_motion("""
      const controller=new AbortController();controller.abort();
      await transitionExplorerPlayback({source:sleeve(),item,signal:controller.signal});
      assert.equal(prepared,0);assert.equal(polls.length,0);
      REDUCED_MOTION=true;
      await transitionExplorerPlayback({source:sleeve(),item,signal:new AbortController().signal});
      assert.equal(prepared,1);assert.equal(polls.length,1);
      assert.equal(ghosts.length,0);assert.equal(animations.length,0);
      assert.equal(libraryPlaybackMotion,null);
    """)


def test_playback_artwork_ready_requires_current_album_track_and_decoded_image():
    setup = """
      const NEUTRAL_ART_URL='/static/neutral-art.svg';
      let artworkLoadFailedAt=0;
      const state={trackId:'new',trackUri:'spotify:track:chosen',albumId:'chosen',stale:false,artUrl:'/new.jpg'};
      const artworkEl={complete:true,naturalWidth:640,currentSrc:'/new.jpg',src:'/new.jpg',getAttribute(){return this.src;}};
      const item={uri:'spotify:album:chosen'};
    """
    _run_motion("\n".join((setup, _motion_function("explorerPlaybackArtworkReady", PLAYER), """
      assert(explorerPlaybackArtworkReady(item,'spotify:track:chosen'));
      state.albumId='previous';assert(!explorerPlaybackArtworkReady(item));state.albumId='chosen';
      state.trackUri='spotify:track:previous';assert(!explorerPlaybackArtworkReady(item,'spotify:track:chosen'));state.trackUri='spotify:track:chosen';
      state.stale=true;assert(!explorerPlaybackArtworkReady(item));state.stale=false;
      artworkEl.complete=false;assert(!explorerPlaybackArtworkReady(item));artworkEl.complete=true;
      artworkEl.naturalWidth=0;assert(!explorerPlaybackArtworkReady(item));artworkEl.naturalWidth=640;
      artworkEl.src=artworkEl.currentSrc='/previous.jpg';assert(!explorerPlaybackArtworkReady(item));
      artworkEl.src=artworkEl.currentSrc=NEUTRAL_ART_URL;
      assert(!explorerPlaybackArtworkReady(item),'a pending old placeholder must not count as the selected cover');
      artworkLoadFailedAt=1;assert(explorerPlaybackArtworkReady(item),'a verified image failure may reveal the neutral fallback');
    """)))
