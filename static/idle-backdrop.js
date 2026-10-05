/* One low-resolution shader, only while the idle shelf is actually visible. */
(function () {
  "use strict";
  window.createIdleBackdrop = function (element, options = {}) {
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    let scene = null;
    let requested = false;
    let failed = false;
    let lastFrameAt = 0;
    let frameAverage = 0;
    let frameCount = 0;
    let slowWindows = 0;
    let reduced = false;
    let monitorRaf = null;
    let failTimer = null;
    function onContextLost() {
      // Defer teardown until after the runtime's context-loss listener.
      failTimer = setTimeout(fallback, 0);
    }

    function stop() {
      if (monitorRaf !== null) cancelAnimationFrame(monitorRaf);
      monitorRaf = null;
      if (failTimer !== null) clearTimeout(failTimer);
      failTimer = null;
      if (scene) {
        scene.canvas.removeEventListener("webglcontextlost", onContextLost);
        scene.destroy();
      }
      scene = null;
      element.classList.remove("active");
      element.dataset.mode = failed ? "static fallback" : "stopped";
      lastFrameAt = frameAverage = frameCount = slowWindows = 0;
    }

    function fallback() {
      failed = true;
      stop();
      element.dataset.mode = "static fallback";
    }

    // Measure the real browser cadence, independently of the capped shader.
    // Sustained pressure halves its pixels and draw rate, then falls back.
    function monitor(now) {
      monitorRaf = null;
      if (!scene) return;
      if (lastFrameAt) {
        const dt = now - lastFrameAt;
        if (dt < 250) { frameAverage += dt; frameCount++; }
      }
      lastFrameAt = now;
      if (frameCount >= 120) {
        const average = frameAverage / frameCount;
        element.dataset.frameMs = average.toFixed(1);
        slowWindows = average > 28 ? slowWindows + 1 : 0;
        frameAverage = frameCount = 0;
        if (slowWindows >= 2) {
          if (reduced) { fallback(); return; }
          reduced = true;
          slowWindows = 0;
          scene.set({ quality: 0.25, maxPixels: 72900, maxFps: 15 });
          element.dataset.mode = "mesh 270px / 15 fps";
        }
      }
      monitorRaf = requestAnimationFrame(monitor);
    }

    function sync() {
      const active = requested && options.enabled !== false && !failed && !motion.matches && !document.hidden;
      if (!active) { stop(); return; }
      if (scene) return;
      if (!window.GradientBackdrop) { fallback(); return; }
      element.classList.add("active");
      try {
        scene = GradientBackdrop.createBackdrop(element, {
          style: "mesh", mode: "direct", quality: 0.35, maxPixels: 142884,
          maxFps: 30, speed: 0.75, grain: 0, seed: 4,
          colors: ["#e19270", "#1a5267", "#5e397b", "#48a79c", "#111527"],
          params: { blend: 2.4, drift: 1.7, warp: 0.7 },
        });
        if (!scene.renderer || scene.fallbackApplied) { fallback(); return; }
        scene.canvas.addEventListener("webglcontextlost", onContextLost, { once: true });
        reduced = false;
        element.dataset.mode = "mesh 378px / 30 fps";
        monitorRaf = requestAnimationFrame(monitor);
      } catch (error) { fallback(); }
    }

    motion.addEventListener("change", sync);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("pagehide", stop);
    return {
      setActive(active) { requested = !!active; sync(); },
      status() { return element.dataset.mode || "stopped"; },
    };
  };
})();
