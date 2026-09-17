/** A dense, gently floating point cloud. Canvas 2D only; no external assets. */
import { sampleCloud } from './cloud-geometry.js';

const TAU = Math.PI * 2;
const TOTAL_POINTS = 6000;
const DEPTH_LEVELS = 18;
const COLOR_LEVELS = 3;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

/**
 * @param {HTMLCanvasElement} canvas A full-viewport canvas sized with CSS.
 * @param {{onReady?: () => void}} options Called after the first complete draw.
 * @returns {{setPaused: (paused: boolean) => void, destroy: () => void} | null}
 */
export function createPointCloud(canvas, { onReady } = {}) {
  let cleanup = () => {};
  try {
    if (!canvas || typeof canvas.getContext !== 'function') return null;
    const document = canvas.ownerDocument;
    const window = document?.defaultView;
    if (!window || typeof window.requestAnimationFrame !== 'function') return null;
    const context = canvas.getContext('2d', { alpha: true });
    if (!context) return null;

    const originalSize = [canvas.width, canvas.height];
    const media = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const cloud = sampleCloud(TOTAL_POINTS);
    const waves = Float32Array.from(cloud, point => Math.sin(point.x * 2.8 + point.y * 3.2 + point.z));
    const screenX = new Float32Array(TOTAL_POINTS);
    const screenY = new Float32Array(TOTAL_POINTS);
    const radius = new Float32Array(TOTAL_POINTS);
    const offsetX = new Float32Array(TOTAL_POINTS);
    const offsetY = new Float32Array(TOTAL_POINTS);
    const velocityX = new Float32Array(TOTAL_POINTS);
    const velocityY = new Float32Array(TOTAL_POINTS);
    const buckets = Array.from({ length: DEPTH_LEVELS * COLOR_LEVELS }, () => []);
    const disposers = [];
    const hardware = Number(window.navigator?.hardwareConcurrency) || 8;
    const memory = Number(window.navigator?.deviceMemory) || 8;
    const constrained = hardware <= 4 || memory <= 4;

    let destroyed = false;
    let ready = false;
    let paused = false;
    let reduced = Boolean(media?.matches);
    let intersecting = true;
    let frame = 0;
    let width = 0;
    let height = 0;
    let dpr = 1;
    let count = TOTAL_POINTS;
    let quality = 1;
    let elapsed = 0;
    let previous = 0;
    let renderAverage = 0;
    let measuredFrames = 0;
    let slowFrames = 0;
    let palette;
    const pointer = { x: 0, y: 0, targetX: 0, targetY: 0, tiltX: 0, tiltY: 0, inside: false };

    const listen = (target, name, callback, options) => {
      target.addEventListener(name, callback, options);
      disposers.push(() => target.removeEventListener(name, callback, options));
    };
    const stop = () => {
      if (frame) window.cancelAnimationFrame(frame);
      frame = 0;
      previous = 0;
    };
    const resetPointer = () => {
      pointer.inside = false;
      pointer.targetX = 0;
      pointer.targetY = 0;
    };
    const resetDisplacement = () => {
      pointer.tiltX = 0;
      pointer.tiltY = 0;
      offsetX.fill(0);
      offsetY.fill(0);
      velocityX.fill(0);
      velocityY.fill(0);
    };
    const canDraw = () => !destroyed && width > 0 && height > 0 && !document.hidden && intersecting;
    const canAnimate = () => canDraw() && !paused && !reduced;
    const schedule = () => {
      if (!frame && canDraw()) frame = window.requestAnimationFrame(tick);
    };
    const updateCount = () => {
      const mobile = width < 700 || window.innerWidth < 700;
      const base = mobile ? (constrained ? 1900 : 2200) : (constrained ? 4800 : TOTAL_POINTS);
      count = Math.max(mobile ? 1200 : 2800, Math.round(base * quality));
    };
    const updateTheme = () => {
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      palette = dark
        ? { colors: ['#76a8ff', '#83e3ed', '#d0e2ff'], opacity: 1, glow: '66,126,242' }
        : { colors: ['#3166ba', '#188b9f', '#254f94'], opacity: 0.94, glow: '99,157,220' };
      schedule();
    };
    const checkViewport = () => {
      const rect = canvas.getBoundingClientRect();
      intersecting = rect.bottom > 0 && rect.right > 0
        && rect.top < window.innerHeight && rect.left < window.innerWidth;
      if (!intersecting) stop();
      else schedule();
    };
    const resize = () => {
      if (destroyed) return;
      const rect = canvas.getBoundingClientRect();
      width = Math.max(0, rect.width);
      height = Math.max(0, rect.height);
      dpr = clamp(Number(window.devicePixelRatio) || 1, 1, 2);
      const pixelWidth = Math.round(width * dpr);
      const pixelHeight = Math.round(height * dpr);
      if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
      if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
      updateCount();
      checkViewport();
      if (!width || !height) stop();
      else schedule();
    };

    function render(dt, animate) {
      const follow = animate ? 1 - Math.exp(-dt * 9) : 0;
      pointer.tiltX += (pointer.targetX - pointer.tiltX) * follow;
      pointer.tiltY += (pointer.targetY - pointer.tiltY) * follow;
      // Bounded angles retain a broad silhouette while the volume slowly breathes.
      const pitch = 0.2 + Math.sin(elapsed * 0.12) * 0.07 - pointer.tiltY * 0.3;
      const yaw = 0.08 + Math.sin(elapsed * 0.095) * 0.12 + pointer.tiltX * 0.42;
      const roll = -0.08 + Math.sin(elapsed * 0.07) * 0.055;
      const cp = Math.cos(pitch), sp = Math.sin(pitch);
      const cy = Math.cos(yaw), sy = Math.sin(yaw);
      const cr = Math.cos(roll), sr = Math.sin(roll);
      const scaleX = width * 0.385;
      const scaleY = height * 0.415;
      const centerX = width * 0.5 + pointer.tiltX * 9;
      const centerY = height * 0.52 + Math.sin(elapsed * 0.33) * 6 + pointer.tiltY * 7;
      const breathing = Math.sin(elapsed * 0.24) * 0.018;
      const influence = clamp(Math.min(width, height) * 0.24, 88, 155);
      const step = dt * 60;
      const damping = Math.exp(-dt * 7.5);
      const dotScale = clamp(Math.min(width, height) / 650, 0.83, 1.25);
      for (const bucket of buckets) bucket.length = 0;

      for (let index = 0; index < count; index += 1) {
        const i = Math.floor(index * TOTAL_POINTS / count);
        const point = cloud[i];
        const x = point.x + waves[i] * breathing;
        const y = point.y * (1 + breathing * 0.6);
        const z = point.z + waves[i] * breathing * 0.5;
        const y1 = y * cp - z * sp;
        const z1 = y * sp + z * cp;
        const x2 = x * cy + z1 * sy;
        const z2 = -x * sy + z1 * cy;
        const x3 = x2 * cr - y1 * sr;
        const y3 = x2 * sr + y1 * cr;
        const perspective = 4.8 / (4.8 - z2);
        const px = centerX + x3 * scaleX * perspective;
        const py = centerY + y3 * scaleY * perspective;
        if (animate) {
          let forceX = 0;
          let forceY = 0;
          if (pointer.inside) {
            const dx = px + offsetX[i] - pointer.x;
            const dy = py + offsetY[i] - pointer.y;
            const distance = Math.sqrt(dx * dx + dy * dy);
            if (distance > 0.01 && distance < influence) {
              const force = (1 - distance / influence) ** 2 * 1.3;
              forceX = dx / distance * force;
              forceY = dy / distance * force;
            }
          }
          velocityX[i] = (velocityX[i] + (forceX - offsetX[i] * 0.033) * step) * damping;
          velocityY[i] = (velocityY[i] + (forceY - offsetY[i] * 0.033) * step) * damping;
          offsetX[i] = clamp(offsetX[i] + velocityX[i] * step, -32, 32);
          offsetY[i] = clamp(offsetY[i] + velocityY[i] * step, -32, 32);
        }
        const near = clamp((z2 + 1.25) / 2.5, 0, 1);
        screenX[i] = px + offsetX[i];
        screenY[i] = py + offsetY[i];
        radius[i] = Math.min(2.6, (0.42 + point.weight * 0.34 + near * 0.72 + (i % 83 === 0 ? 0.38 : 0)) * dotScale * perspective * 1.35);
        const layer = Math.min(DEPTH_LEVELS - 1, Math.floor(near * DEPTH_LEVELS));
        const tone = i % 13 === 0 ? 2 : (point.x + point.z * 0.7 > 0.25 || i % 5 === 0 ? 1 : 0);
        buckets[layer * COLOR_LEVELS + tone].push(i);
      }

      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.clearRect(0, 0, width, height);
      const glow = context.createRadialGradient(centerX, centerY, 0, centerX, centerY, Math.max(width, height) * 0.5);
      glow.addColorStop(0, `rgba(${palette.glow},.035)`);
      glow.addColorStop(1, `rgba(${palette.glow},0)`);
      context.globalAlpha = 1;
      context.fillStyle = glow;
      context.fillRect(0, 0, width, height);
      // Linear depth buckets avoid a connection graph or a per-frame sort.
      for (let layer = 0; layer < DEPTH_LEVELS; layer += 1) {
        const near = (layer + 0.5) / DEPTH_LEVELS;
        context.globalAlpha = (0.22 + near * near * 0.72) * palette.opacity;
        for (let tone = 0; tone < COLOR_LEVELS; tone += 1) {
          const bucket = buckets[layer * COLOR_LEVELS + tone];
          if (!bucket.length) continue;
          context.fillStyle = palette.colors[tone];
          context.beginPath();
          for (const i of bucket) {
            context.moveTo(screenX[i] + radius[i], screenY[i]);
            context.arc(screenX[i], screenY[i], radius[i], 0, TAU);
          }
          context.fill();
        }
      }
      context.globalAlpha = 1;
      if (!ready) {
        ready = true;
        if (typeof onReady === 'function') onReady();
      }
    }

    function tick(time) {
      frame = 0;
      if (!canDraw()) return;
      const animate = canAnimate();
      const dt = previous ? clamp((time - previous) / 1000, 0.001, 0.04) : 1 / 60;
      previous = animate ? time : 0;
      if (animate) elapsed += dt;
      const start = window.performance?.now?.() ?? 0;
      try {
        render(dt, animate);
      } catch {
        cleanup();
        return;
      }
      if (animate) {
        const cost = (window.performance?.now?.() ?? start) - start;
        renderAverage = renderAverage ? renderAverage * 0.94 + cost * 0.06 : cost;
        measuredFrames += 1;
        if (dt > 0.027) slowFrames += 1;
        if (measuredFrames >= 100) {
          if ((renderAverage > 11 || slowFrames > 60) && quality > 0.6) {
            quality = Math.max(0.6, quality - 0.2);
            updateCount();
          }
          measuredFrames = 0;
          slowFrames = 0;
        }
        schedule();
      }
    }

    cleanup = () => {
      if (destroyed) return;
      destroyed = true;
      stop();
      for (const dispose of disposers.splice(0)) dispose();
      canvas.width = originalSize[0];
      canvas.height = originalSize[1];
    };

    const movePointer = event => {
      if (event.pointerType === 'touch' || reduced || paused) return;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      pointer.x = event.clientX - rect.left;
      pointer.y = event.clientY - rect.top;
      pointer.inside = pointer.x >= 0 && pointer.x <= rect.width && pointer.y >= 0 && pointer.y <= rect.height;
      pointer.targetX = clamp(pointer.x / rect.width * 2 - 1, -1, 1);
      pointer.targetY = clamp(pointer.y / rect.height * 2 - 1, -1, 1);
    };
    // The background has pointer-events:none, so input belongs to the viewport.
    listen(window, 'pointermove', movePointer, { passive: true });
    listen(window, 'pointerout', event => { if (!event.relatedTarget) resetPointer(); }, { passive: true });
    listen(window, 'pointercancel', resetPointer, { passive: true });
    listen(window, 'blur', resetPointer);
    listen(window, 'resize', resize, { passive: true });
    if (window.visualViewport) listen(window.visualViewport, 'resize', resize, { passive: true });
    listen(document, 'visibilitychange', () => {
      resetPointer();
      if (document.hidden) stop();
      else schedule();
    });
    const motionChanged = event => {
      reduced = event.matches;
      if (reduced) {
        elapsed = 0;
        resetPointer();
        resetDisplacement();
      }
      stop();
      schedule();
    };
    if (media?.addEventListener) listen(media, 'change', motionChanged);
    else if (media?.addListener) {
      media.addListener(motionChanged);
      disposers.push(() => media.removeListener(motionChanged));
    }
    if (typeof window.ResizeObserver === 'function') {
      const observer = new window.ResizeObserver(resize);
      observer.observe(canvas);
      disposers.push(() => observer.disconnect());
    }
    if (typeof window.IntersectionObserver === 'function') {
      const observer = new window.IntersectionObserver(entries => {
        intersecting = entries.some(entry => entry.isIntersecting);
        if (!intersecting) stop();
        else schedule();
      });
      observer.observe(canvas);
      disposers.push(() => observer.disconnect());
    } else {
      listen(window, 'scroll', checkViewport, { passive: true });
    }
    if (typeof window.MutationObserver === 'function') {
      const observer = new window.MutationObserver(updateTheme);
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      disposers.push(() => observer.disconnect());
    }

    updateTheme();
    resize();
    if (canDraw()) render(0, false);
    if (reduced) stop();
    else schedule();
    return {
      setPaused(value) {
        if (destroyed) return;
        paused = Boolean(value);
        resetPointer();
        if (paused) stop();
        else schedule();
      },
      destroy: cleanup,
    };
  } catch {
    cleanup();
    return null;
  }
}
