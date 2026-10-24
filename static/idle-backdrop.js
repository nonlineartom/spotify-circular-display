/* One low-resolution shader, only while the idle shelf is actually visible. */
(function () {
  "use strict";
  // The idle backdrop is drawn from the forecast's measured values, not from
  // fixed presets. The weather type chooses which shader can depict it (sky,
  // falling rain, ripples, mist, a moon...); the numbers then set that
  // shader's motion, turbulence, density, contrast and palette:
  //   wind/gusts  -> animation speed, cloud drift, warp, swirl, rain slant
  //   rain rate   -> darkness, contrast, number of rain columns or ripples
  //   cloud cover -> how much sky is covered and how grey it is
  //   temperature -> sun warmth, frost on clear nights
  //   visibility  -> fog density
  // Colours follow each style's colour order from the gradient-backdrops skill.
  const STORM = new Set([95, 96, 99]);
  const SNOW = new Set([66, 67, 71, 73, 75, 77, 85, 86]);
  const DOWNPOUR = new Set([65, 82]);
  const RAIN = new Set([61, 63, 80, 81]);
  const DRIZZLE = new Set([51, 53, 55, 56, 57]);
  const FOG = new Set([45, 48]);
  const INTEGER_PARAMS = new Set(["bars", "rings", "blobs", "shape", "crescent", "layers"]);

  // Kept deliberately narrow: neighbouring stops sit close in lightness so
  // every look stays a quiet backdrop behind the record shelf and clock.
  const PALETTES = {
    clear: ["#f2f7fc", "#c4dcf3", "#86b3e6", "#4a82cc"],
    grey: ["#dfe4e9", "#bcc5ce", "#95a1ad", "#6c7886"],
    dawn: ["#fff2e4", "#f8d6bd", "#e8b3a8", "#8e86aa"],
    dusk: ["#fde3c4", "#f0b28a", "#cf8077", "#5c4a74"],
    nightCloud: ["#7d89a0", "#55627e", "#33405e", "#1a2440"],
    heatMild: ["#3a72c6", "#78a8de", "#b9cfe4", "#f2d9b4", "#fff5e4"],
    heatHot: ["#4176c2", "#8eb0d8", "#d3d1c8", "#f5c68a", "#ffefd2"],
    moon: ["#0b1530", "#111c3e", "#22386f", "#7f9dd2", "#cfdcf5"],
    frost: ["#08131f", "#0d2234", "#1f4f68", "#8cc6dc", "#dff4fb"],
    lightRain: ["#b3c1ce", "#98a9b9", "#7e91a4", "#66798e"],
    heavyRain: ["#7a8a9a", "#62748a", "#4c5e73", "#394a5e"],
    columnsLight: ["#2a3b50", "#364d66", "#46607c", "#5d7894", "#7d97b1"],
    columnsHeavy: ["#192637", "#22344a", "#2f455e", "#415b77", "#5a7591"],
    drizzle: ["#dfe5eb", "#c3ccd5", "#9eabb8", "#7b8a99"],
    ripples: ["#0a1424", "#10203a", "#183050", "#244466", "#36587c"],
    snow: ["#768da4", "#92a7bb", "#afc1d1", "#cbd8e4", "#e6edf3"],
    snowNight: ["#aab6c6", "#7b8ba2", "#4b5b76", "#27344d"],
    fog: ["#e2e6e9", "#d3d9de", "#c3cad1", "#b1b9c1", "#9ea8b2", "#8b96a1"],
    fogNight: ["#3a4250", "#323947", "#2a303c", "#222833", "#1b2029", "#14181f"],
    gale: ["#d6dfde", "#bccac9", "#9fb1b3", "#84999e", "#6b8189", "#566b75"],
    galeDark: ["#9aa7ae", "#82929b", "#6a7c87", "#556773", "#42545f", "#33434e"],
    galeNight: ["#5c6f86", "#3e5068", "#28374d", "#1a2638", "#101827", "#080d16"],
    storm: ["#5a5474", "#47425f", "#36324c", "#26233a"],
  };
  // Shown until the first forecast arrives: the original blue-sky clouds.
  const CALM = { code: 2, day: true, temp: 12, wind: 40, gust: 45, rain: 0, chance: 0, cloud: 55, visibility: null, golden: null };

  const clamp01 = (value) => Math.min(1, Math.max(0, value));
  const lerp = (a, b, t) => a + (b - a) * t;
  function mixHex(a, b, t) {
    const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
    const channel = (shift) => Math.round(lerp((pa >> shift) & 255, (pb >> shift) & 255, t));
    return "#" + ((1 << 24) | (channel(16) << 16) | (channel(8) << 8) | channel(0)).toString(16).slice(1);
  }
  const mixPalette = (a, b, t) => a.map((colour, i) => mixHex(colour, b[i], clamp01(t)));

  function severity(code) {
    if (STORM.has(code)) return 6;
    if (SNOW.has(code)) return 5;
    if (DOWNPOUR.has(code)) return 4;
    if (RAIN.has(code)) return 3;
    if (DRIZZLE.has(code)) return 2;
    if (FOG.has(code)) return 1;
    return 0;
  }

  // Merge forecast hours into one sample: the worst weather and strongest
  // wind in the window, its mean temperature and cloud.
  function aggregate(hours) {
    const mean = (key) => hours.reduce((sum, hour) => sum + hour[key], 0) / hours.length;
    const max = (key) => Math.max(...hours.map((hour) => hour[key]));
    const worst = hours.reduce((a, b) => (severity(b.code) > severity(a.code) ||
      (severity(b.code) === severity(a.code) && b.code > a.code) ? b : a));
    const seen = hours.map((hour) => hour.visibility).filter((value) => typeof value === "number");
    return {
      code: worst.code, day: hours[Math.floor(hours.length / 2)].day,
      temp: mean("temp"), wind: max("wind"), gust: max("gust"), rain: max("rain"),
      chance: max("chance"), cloud: mean("cloud"),
      visibility: seen.length ? Math.min(...seen) : null,
      golden: (hours.find((hour) => hour.golden) || {}).golden || null,
    };
  }

  function valid(hour) {
    return hour && typeof hour === "object" && Number.isFinite(hour.code) && Number.isFinite(hour.temp) &&
      ["wind", "gust", "rain", "chance", "cloud"].every((key) => Number.isFinite(hour[key]));
  }

  // What the weather is, as a shader can show it.
  function condition(w) {
    if (STORM.has(w.code)) return "storm";
    if (SNOW.has(w.code)) return w.day ? "snow" : "snow-night";
    if (w.rain >= 0.1 || RAIN.has(w.code) || DOWNPOUR.has(w.code) || DRIZZLE.has(w.code)) {
      if (!w.day) return "rain-night";
      if (DOWNPOUR.has(w.code) || w.rain >= 4) return "downpour";
      return DRIZZLE.has(w.code) && w.rain < 0.5 ? "drizzle" : "rain";
    }
    if (FOG.has(w.code) || (w.visibility !== null && w.visibility < 1000)) return "fog";
    if (w.gust >= 50) return "gale";
    if (w.golden) return w.golden;
    if (!w.day) return w.cloud < 30 ? "clear-night" : "cloudy-night";
    if (w.cloud < 25 && w.temp >= 20) return "sun";
    return "sky";
  }

  // Turn one weather sample into shader options.
  function design(w) {
    const name = condition(w);
    const wind = clamp01(Math.max(w.wind / 50, w.gust / 80));
    const rain = clamp01(Math.sqrt(w.rain / 8));
    const cloud = clamp01(w.cloud / 100);
    const chance = clamp01(w.chance / 100);
    const heat = clamp01((w.temp - 18) / 14);
    const cold = clamp01((4 - w.temp) / 8);
    const skyParams = { scale: 1 + 0.5 * cloud, cover: 0.08 + 0.9 * cloud, wind: 0.3 + 2.7 * wind, streak: 0.4 + 1.4 * wind };
    const looks = {
      sky: () => ({ style: "sky", speed: 0.6 + 2.2 * wind, params: skyParams,
        colors: mixPalette(PALETTES.clear, PALETTES.grey, 0.85 * cloud ** 1.6 + 0.15 * chance) }),
      dawn: () => ({ style: "sky", speed: 0.6 + 2 * wind, params: skyParams,
        colors: mixPalette(PALETTES.dawn, PALETTES.grey, 0.6 * cloud ** 1.4) }),
      dusk: () => ({ style: "sky", speed: 0.6 + 2 * wind, params: skyParams,
        colors: mixPalette(PALETTES.dusk, PALETTES.grey, 0.6 * cloud ** 1.4) }),
      "cloudy-night": () => ({ style: "sky", speed: 0.5 + 2 * wind, params: skyParams,
        colors: mixPalette(PALETTES.nightCloud, PALETTES.moon.slice(1), 0.5 * (1 - cloud)) }),
      // The appliance's own "heat" shader: light from a sun just beyond the frame
      // and shimmer off a golden horizon. Heat strengthens all of it.
      sun: () => ({ style: "heat", speed: 0.5 + 0.8 * wind, colors: mixPalette(PALETTES.heatMild, PALETTES.heatHot, heat),
        params: { sunx: 0.72, suny: 0.92, glow: 0.9 + 0.45 * heat, rays: 0.45 + 0.3 * heat,
          shimmer: 0.4 + 0.7 * heat, wisps: Math.min(0.6, 1.8 * cloud) } }),
      "clear-night": () => ({ style: "glow", speed: 0.4 + 0.6 * wind, colors: mixPalette(PALETTES.moon, PALETTES.frost, cold),
        params: { blobs: 1, crescent: 1, size: 0.45, rim: 0.7 + 0.4 * cold, light: 30, x: -0.4, y: -0.3 } }),
      rain: () => ({ style: "flow", speed: 0.5 + 0.6 * rain + 1.2 * wind,
        colors: mixPalette(PALETTES.lightRain, PALETTES.heavyRain, rain),
        params: { scale: 0.75 + 0.2 * rain, warp: 0.5 + 0.8 * wind, contrast: 0.7 + 0.3 * rain } }),
      downpour: () => ({ style: "prism", speed: 0.8 + 1.2 * rain + 0.6 * wind,
        colors: mixPalette(PALETTES.columnsLight, PALETTES.columnsHeavy, rain),
        // Soft streaks multiply with the rain rate and lean with the wind.
        params: { bars: Math.round(18 + 16 * rain), angle: -28 * wind, softness: 1.35 - 0.35 * rain, spread: 2.5, valley: 1 } }),
      drizzle: () => ({ style: "sky", speed: 0.5 + 1.5 * wind, colors: PALETTES.drizzle,
        params: { scale: 1.6, cover: 0.9, wind: 0.4 + 2 * wind, streak: 0.4 + wind } }),
      "rain-night": () => ({ style: "rings", speed: 0.4 + rain + 0.5 * wind, colors: PALETTES.ripples,
        params: { rings: Math.round(8 + 14 * rain), cx: 0, cy: 0, glow: 0.3 + 0.3 * rain, depth: 0.5 + 0.8 * rain } }),
      snow: () => ({ style: "prism", speed: 0.4 + 0.8 * rain + 0.5 * wind, colors: PALETTES.snow,
        params: { bars: Math.round(16 + 12 * rain), angle: -25 * wind, softness: 1.3, spread: 2.5, valley: 1 } }),
      "snow-night": () => ({ style: "sky", speed: 0.5 + 1.5 * wind, colors: PALETTES.snowNight,
        params: { scale: 1.6, cover: 0.85 + 0.15 * cloud, wind: 0.4 + 2 * wind, streak: 0.4 + 1.4 * wind } }),
      fog: () => ({ style: "mist", speed: 0.6 + 0.6 * wind, colors: w.day ? PALETTES.fog : PALETTES.fogNight,
        params: { haze: w.visibility === null ? 1.5 : 1 + clamp01((1000 - w.visibility) / 1000),
          sun: w.day ? 0.4 * (1 - cloud) : 0, drift: 0.3 + 2 * wind } }),
      gale: () => ({ style: "swirl", speed: 0.6 + 1.6 * wind,
        colors: w.day ? mixPalette(PALETTES.gale, PALETTES.galeDark, 0.7 * cloud + 0.3 * chance) : PALETTES.galeNight,
        params: { twist: 0.4 + 1.4 * wind, scale: 1, cx: 0, cy: 0 } }),
      storm: () => ({ style: "flow", speed: 0.8 + wind, colors: PALETTES.storm,
        params: { scale: 0.8, warp: 0.6 + 0.6 * wind, contrast: 0.75 + 0.25 * rain } }),
    };
    return { name, ...looks[name]() };
  }

  function summary(w) {
    return `${Math.round(w.temp)}°C, wind ${Math.round(w.wind)} gust ${Math.round(w.gust)} km/h, ` +
      `rain ${w.rain.toFixed(1)} mm/h (${Math.round(w.chance)}%), cloud ${Math.round(w.cloud)}%`;
  }

  // Awake, the idle screen plays through the coming day: the hour now, then
  // three-hour windows. Standby only ever shows the hour now.
  const ROTATE_MS = 60 * 1000;
  const FADE_MS = 700;
  const TWEEN_STEPS = 12;
  const TWEEN_STEP_MS = 400;

  window.createIdleBackdrop = function (element, options = {}) {
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    let scene = null;
    let requested = false;
    let standby = false;
    const shelfParent = element.parentElement;
    let failed = false;
    let lastFrameAt = 0;
    let frameAverage = 0;
    let frameCount = 0;
    let slowWindows = 0;
    let reduced = false;
    let monitorRaf = null;
    let failTimer = null;
    let frames = [];
    let frameIndex = 0;
    let shown = null;
    let rotateTimer = null;
    let changeTimer = null;
    function onContextLost() {
      // Defer teardown until after the runtime's context-loss listener.
      failTimer = setTimeout(fallback, 0);
    }

    function wantedWeather() {
      return frames[standby ? 0 : frameIndex] || CALM;
    }

    function label() {
      const [px, fps] = standby ? (reduced ? [180, 8] : [216, 12]) : (reduced ? [270, 15] : [378, 30]);
      return `${standby ? "standby " : ""}${shown.name} ${px}px / ${fps} fps`;
    }

    function clearRotation() {
      if (rotateTimer !== null) clearTimeout(rotateTimer);
      if (changeTimer !== null) clearTimeout(changeTimer);
      rotateTimer = changeTimer = null;
      element.classList.remove("switching");
    }

    function scheduleRotation() {
      if (rotateTimer !== null || !scene || standby || frames.length < 2) return;
      rotateTimer = setTimeout(() => {
        rotateTimer = null;
        frameIndex = (frameIndex + 1) % frames.length;
        changeWeather();
      }, ROTATE_MS);
    }

    function apply(target) {
      // A style change without params resets them; set them second.
      scene.set({ style: target.style, colors: target.colors, speed: target.speed });
      scene.set({ params: target.params || {} });
      shown = target;
      element.dataset.mode = label();
    }

    // Same shader: glide speed, palette and params so the weather evolves.
    // New shader: fade the canvas out, swap it on the same context, fade in.
    function changeWeather() {
      if (!scene) return;
      const weather = wantedWeather();
      const target = design(weather);
      element.dataset.weather = summary(weather);
      if (target.style !== shown.style) {
        element.classList.add("switching");
        changeTimer = setTimeout(() => {
          changeTimer = null;
          if (!scene) return;
          apply(target);
          element.classList.remove("switching");
          scheduleRotation();
        }, FADE_MS);
        return;
      }
      const from = shown;
      let step = 0;
      const glide = () => {
        changeTimer = null;
        if (!scene) return;
        step++;
        const t = step / TWEEN_STEPS;
        const params = {};
        for (const [key, value] of Object.entries(target.params || {})) {
          const start = (from.params || {})[key];
          const mixed = typeof start === "number" ? lerp(start, value, t) : value;
          params[key] = INTEGER_PARAMS.has(key) ? Math.round(mixed) : mixed;
        }
        apply({ ...target, speed: lerp(from.speed, target.speed, t), params,
          colors: target.colors.length === from.colors.length ? mixPalette(from.colors, target.colors, t) : target.colors });
        if (step < TWEEN_STEPS) changeTimer = setTimeout(glide, TWEEN_STEP_MS);
        else { shown = target; scheduleRotation(); }
      };
      changeTimer = setTimeout(glide, TWEEN_STEP_MS);
    }

    function stop() {
      if (monitorRaf !== null) cancelAnimationFrame(monitorRaf);
      monitorRaf = null;
      if (failTimer !== null) clearTimeout(failTimer);
      failTimer = null;
      clearRotation();
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
          scene.set(standby ? { quality: 1 / 6, maxPixels: 32400, maxFps: 8 } :
            { quality: 0.25, maxPixels: 72900, maxFps: 15 });
          element.dataset.mode = label();
        }
      }
      monitorRaf = requestAnimationFrame(monitor);
    }

    function sync() {
      const active = requested && options.enabled !== false && !failed && !motion.matches && !document.hidden;
      if (!active) { stop(); return; }
      if (scene) return;
      if (!window.GradientBackdrop) { fallback(); return; }
      // Reuse the same surface above the dimmer and below the clock in
      // standby. Pointer events still pass through to the wake shield.
      if (shelfParent) (standby ? shelfParent.closest("#viewport") : shelfParent).appendChild(element);
      element.classList.toggle("standby", standby);
      element.classList.add("active");
      // Each return to idle starts from the weather right now.
      frameIndex = 0;
      const weather = wantedWeather();
      const target = design(weather);
      try {
        scene = GradientBackdrop.createBackdrop(element, {
          style: target.style, colors: target.colors, params: target.params || {}, speed: target.speed,
          mode: "direct", quality: 0.35, maxPixels: 142884, maxFps: 30, grain: 0, seed: 4,
          ...(standby ? { quality: 0.2, maxPixels: 46656, maxFps: 12 } : {}),
        });
        if (!scene.renderer || scene.fallbackApplied) { fallback(); return; }
        scene.canvas.addEventListener("webglcontextlost", onContextLost, { once: true });
        reduced = false;
        shown = target;
        element.dataset.mode = label();
        element.dataset.weather = frames.length ? summary(weather) : "no forecast";
        monitorRaf = requestAnimationFrame(monitor);
        scheduleRotation();
      } catch (error) { fallback(); }
    }

    motion.addEventListener("change", sync);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("pagehide", stop);
    return {
      setActive(active, dimmed = false) {
        requested = !!active;
        if (standby !== !!dimmed) { stop(); standby = !!dimmed; }
        sync();
      },
      // Accepts /api/weather/backdrop: the hour now, then three-hour windows.
      setForecast(forecast) {
        const hours = (forecast && Array.isArray(forecast.hours) ? forecast.hours : []).filter(valid);
        const next = hours.length ? [hours[0]] : [];
        for (let i = 1; i < hours.length; i += 3) next.push(aggregate(hours.slice(i, i + 3)));
        if (JSON.stringify(next) === JSON.stringify(frames)) return;
        frames = next;
        frameIndex = 0;
        clearRotation();
        changeWeather();
      },
      status() { return element.dataset.mode || "stopped"; },
      weather() { return element.dataset.weather || "no forecast"; },
      design,
    };
  };
})();
