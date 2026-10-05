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
const element = {dataset:{}, classList:{add() {}, remove() {}}};
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
control.setActive(true);
assert.equal(creates, 1, 'duplicate idle update allocated another scene');
control.setActive(false);
assert.equal(destroys, 1);
assert.equal(frames.size, 0);
assert.equal(timers.size, 0, 'normal context release incorrectly triggered fallback');
control.setActive(true);
assert.equal(creates, 2);
document.hidden = true; events.get('visibilitychange')();
assert.equal(destroys, 2);
assert.equal(frames.size, 0);
document.hidden = false; events.get('visibilitychange')();
assert.equal(creates, 3);
motion.matches = true; events.get('motion')();
assert.equal(destroys, 3);
motion.matches = false; events.get('motion')();
assert.equal(creates, 4);
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
assert.equal(creates, 4, 'failed scene recreated on the next player tick');
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
