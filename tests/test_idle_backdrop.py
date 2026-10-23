"""Idle shader lifecycle: no GPU scene survives playback or hidden screens."""

from pathlib import Path
import subprocess


def test_idle_backdrop_releases_resources_and_handles_pressure():
    source = Path(__file__).resolve().parents[1] / "static" / "idle-backdrop.js"
    subprocess.run(["node", "-e", r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const events = new Map(), frames = new Map(), timers = new Map();
let id = 0, creates = 0, destroys = 0, latest, unavailable = false;
const motion = {matches:false, addEventListener(name, fn) {events.set('motion', fn);}};
const root = {appendChild(el) {el.parentElement = this;}};
const shelfParent = {closest() {return root;}, appendChild(el) {el.parentElement = this;}};
const element = {parentElement:shelfParent, dataset:{}, classList:{add() {}, remove() {}, toggle() {}}};
const document = {hidden:false, addEventListener(name, fn) {events.set(name, fn);}};
const window = {addEventListener(name, fn) {events.set(name, fn);}};
const GradientBackdrop = {createBackdrop(el, opts) {
  creates++;
  const listeners = new Map();
  latest = {renderer:unavailable ? null : {}, fallbackApplied:unavailable,
    opts, canvas:{addEventListener(name, fn) {listeners.set(name, fn);},
    removeEventListener(name, fn) {if (listeners.get(name) === fn) listeners.delete(name);}},
    set(next) {Object.assign(this.opts, next);},
    destroy() {destroys++; listeners.get('webglcontextlost')?.();},
    lose() {listeners.get('webglcontextlost')?.();}};
  return latest;
}};
window.GradientBackdrop = GradientBackdrop;
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window, document, GradientBackdrop, matchMedia:() => motion,
  requestAnimationFrame(fn) {frames.set(++id, fn); return id;},
  cancelAnimationFrame(id) {frames.delete(id);},
  setTimeout(fn) {timers.set(++id, fn); return id;},
  clearTimeout(id) {timers.delete(id);},
});
const control = window.createIdleBackdrop(element);
control.setActive(true);
assert.equal(creates, 1);
assert.equal(latest.opts.maxPixels, 142884);
assert.equal(latest.opts.style, 'sky', 'no forecast should keep the blue-sky clouds');
control.setActive(true);
assert.equal(creates, 1, 'duplicate idle update allocated another scene');
control.setActive(false);
assert.equal(destroys, 1);
assert.equal(frames.size, 0);
assert.equal(timers.size, 0, 'normal context release incorrectly triggered fallback');
control.setActive(true, true);
assert.equal(latest.opts.maxPixels, 46656);
assert.equal(latest.opts.maxFps, 12);
assert.equal(control.status(), 'standby sky 216px / 12 fps');
assert.equal(element.parentElement, root, 'standby backdrop stayed behind the dimmer');
control.setActive(false, false);
const standbyCreates = creates;
const standbyDestroys = destroys;
control.setActive(true);
assert.equal(element.parentElement, shelfParent, 'awake backdrop did not return to the shelf');
assert.equal(creates, standbyCreates + 1);
document.hidden = true; events.get('visibilitychange')();
assert.equal(destroys, standbyDestroys + 1);
assert.equal(frames.size, 0);
document.hidden = false; events.get('visibilitychange')();
assert.equal(creates, standbyCreates + 2);
motion.matches = true; events.get('motion')();
assert.equal(destroys, standbyDestroys + 2);
motion.matches = false; events.get('motion')();
assert.equal(creates, standbyCreates + 3);
function frame(now) {
  const jobs = [...frames.values()]; frames.clear(); jobs.forEach(fn => fn(now));
}
for (let n=1; n<=241; n++) frame(n * 35);
assert.equal(latest.opts.maxPixels, 72900);
assert.equal(latest.opts.maxFps, 15);
for (let n=242; n<=500; n++) frame(n * 35);
assert.equal(control.status(), 'static fallback');
assert.equal(frames.size, 0);
control.setActive(true);
assert.equal(creates, standbyCreates + 3, 'failed scene recreated on the next player tick');
const second = window.createIdleBackdrop(element);
second.setActive(true);
latest.lose();
for (const fn of [...timers.values()]) fn();
assert.equal(second.status(), 'static fallback');
assert.equal(frames.size, 0);
unavailable = true;
const third = window.createIdleBackdrop(element);
third.setActive(true);
assert.equal(third.status(), 'static fallback');
assert.equal(frames.size, 0);
const disabled = window.createIdleBackdrop(element, {enabled:false});
const before = creates;
disabled.setActive(true);
assert.equal(creates, before);
""", str(source)], check=True, capture_output=True, text=True)


def test_idle_backdrop_plays_through_the_forecast():
    source = Path(__file__).resolve().parents[1] / "static" / "idle-backdrop.js"
    subprocess.run(["node", "-e", r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const timers = new Map(), classes = new Set();
let id = 0, creates = 0, latest;
const motion = {matches:false, addEventListener() {}};
const root = {appendChild(el) {el.parentElement = this;}};
const shelfParent = {closest() {return root;}, appendChild(el) {el.parentElement = this;}};
const element = {parentElement:shelfParent, dataset:{},
  classList:{add(c) {classes.add(c);}, remove(c) {classes.delete(c);}, toggle(c, on) {on ? classes.add(c) : classes.delete(c);}}};
const window = {addEventListener() {}};
window.GradientBackdrop = {createBackdrop(el, opts) {
  creates++;
  latest = {renderer:{}, fallbackApplied:false, opts:{...opts}, sets:[],
    canvas:{addEventListener() {}, removeEventListener() {}},
    set(next) {this.sets.push(next); Object.assign(this.opts, next);}, destroy() {}};
  return latest;
}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window, document:{hidden:false, addEventListener() {}}, GradientBackdrop:window.GradientBackdrop,
  matchMedia:() => motion,
  requestAnimationFrame() {return ++id;}, cancelAnimationFrame() {},
  setTimeout(fn, ms) {timers.set(++id, {fn, ms}); return id;},
  clearTimeout(id) {timers.delete(id);},
});
function fire() {
  const jobs = [...timers.values()]; timers.clear(); jobs.forEach(job => job.fn());
}
function runUntil(done, limit = 40) {
  for (let n = 0; n < limit && !done(); n++) fire();
  assert.ok(done(), 'timers never reached the expected state');
}
const control = window.createIdleBackdrop(element);
const {design} = control;
const base = {code:2, day:true, temp:12, wind:5, gust:10, rain:0, chance:0, cloud:30, visibility:20000, golden:null};
const w = (o) => design({...base, ...o});
const lum = (hex) => [1, 3, 5].reduce((sum, i) => sum + parseInt(hex.slice(i, i + 2), 16), 0);

// The weather type picks a shader that can depict it.
const names = {storm:{code:95}, snow:{code:73}, 'snow-night':{code:73, day:false}, downpour:{code:65, rain:5},
  rain:{code:61, rain:1}, drizzle:{code:51, rain:0.2}, 'rain-night':{code:61, rain:1, day:false},
  fog:{code:45}, gale:{gust:60}, dusk:{golden:'dusk'}, 'clear-night':{code:0, cloud:5, day:false},
  'cloudy-night':{cloud:80, day:false}, sun:{code:0, cloud:5, temp:26}, sky:{cloud:60}};
for (const [name, o] of Object.entries(names)) assert.equal(w(o).name, name, JSON.stringify(o));
assert.equal(w({visibility:300}).name, 'fog', 'low visibility is fog even without a fog code');

// The measured values drive the shader, continuously.
assert.ok(w({wind:30, gust:50}).speed > w({wind:5, gust:10}).speed, 'wind should speed the sky up');
assert.ok(w({wind:30, gust:45}).params.wind > w({}).params.wind, 'wind should drive cloud drift');
assert.ok(w({cloud:95}).params.cover > w({cloud:10}).params.cover, 'cloud cover should cover the sky');
const blueness = (hex) => parseInt(hex.slice(5, 7), 16) - parseInt(hex.slice(1, 3), 16);
assert.ok(blueness(w({cloud:100}).colors[2]) < blueness(w({cloud:0}).colors[2]) - 40, 'cloud should grey the sky');
const drizzly = w({code:61, rain:0.3}), soaking = w({code:63, rain:3.5});
assert.ok(soaking.params.contrast > drizzly.params.contrast, 'heavier rain should raise contrast');
assert.ok(lum(soaking.colors[3]) < lum(drizzly.colors[3]), 'heavier rain should darken');
assert.ok(w({code:63, rain:1, gust:70}).params.warp > w({code:63, rain:1}).params.warp, 'wind should churn rain');
const pour = w({code:65, rain:5}), torrent = w({code:82, rain:15, wind:40, gust:80});
assert.ok(torrent.params.bars > pour.params.bars, 'more rain should add columns');
assert.ok(torrent.params.angle < pour.params.angle, 'wind should slant the rain');
assert.ok(w({code:65, rain:8, day:false}).params.rings > w({code:61, rain:0.3, day:false}).params.rings);
assert.ok(w({gust:95, wind:60}).params.twist > w({gust:55}).params.twist, 'stronger gales should twist harder');
assert.ok(w({code:0, cloud:5, temp:31}).params.size > w({code:0, cloud:5, temp:20}).params.size);
assert.ok(Number.isInteger(torrent.params.bars));

// Forecast: the hour now, then three-hour windows of the worst weather in each.
const hour = (o) => ({...base, from:0, ...o});
control.setForecast({hours:[
  hour({code:63, rain:3}),
  hour({code:61, rain:0.5}), hour({code:61, rain:0.4}), {broken:true}, hour({code:61, rain:0.3}),
  hour({code:0, cloud:5, day:false}), hour({code:0, cloud:5, day:false}), hour({code:0, cloud:5, day:false}),
]});
control.setActive(true);
assert.equal(creates, 1);
assert.equal(latest.opts.style, 'flow', 'idle should open on the weather right now');
assert.equal(control.status(), 'rain 378px / 30 fps');
assert.match(control.weather(), /rain 3\.0 mm\/h/);
const heavyContrast = latest.opts.params.contrast;
assert.equal(timers.size, 1);
assert.equal([...timers.values()][0].ms, 60000);

fire();  // rotation: same shader, so it glides instead of fading
assert.ok(!classes.has('switching'));
runUntil(() => [...timers.values()].some(job => job.ms === 60000));
assert.equal(creates, 1, 'a weather change must not allocate another WebGL scene');
assert.ok(latest.opts.params.contrast < heavyContrast, 'lighter rain should calm the contrast');
assert.match(control.weather(), /rain 0\.5 mm\/h/, 'a window shows its heaviest rain');

fire();  // rotation to a different shader: fade, swap, fade in
assert.ok(classes.has('switching'));
fire();
assert.ok(!classes.has('switching'));
assert.equal(creates, 1);
assert.equal(latest.opts.style, 'glow');
assert.equal(control.status(), 'clear-night 378px / 30 fps');
fire(); fire();
assert.equal(control.status(), 'rain 378px / 30 fps', 'rotation should loop back to now');

// Standby shows only the hour now and never rotates.
control.setActive(true, true);
assert.equal(control.status(), 'standby rain 216px / 12 fps');
assert.equal(timers.size, 0);

// A changed forecast moves the live scene to the new hour now.
control.setActive(true, false);
control.setForecast({hours:[hour({code:73, rain:1, temp:-1})]});
fire();
assert.equal(latest.opts.style, 'prism');
assert.equal(control.status(), 'snow 378px / 30 fps');
assert.equal(timers.size, 0, 'a single-hour forecast should not rotate');
control.setActive(false);
assert.equal(timers.size, 0);
assert.ok(!classes.has('switching'));
""", str(source)], check=True, capture_output=True, text=True)
