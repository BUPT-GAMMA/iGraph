/** Deterministic, filled geometry shared by the canvas and static SVG fallback. */
const TAU = Math.PI * 2;
const fraction = value => value - Math.floor(value);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

/** @returns {{x: number, y: number, z: number, weight: number}[]} */
export function sampleCloud(count = 6000) {
  const size = Number.isFinite(count) ? clamp(Math.floor(count), 1, 12000) : 6000;
  return Array.from({ length: size }, (_, index) => {
    const i = index + 0.5;
    const azimuth = fraction(i * 0.7548776662466927) * TAU;
    const vertical = fraction(i * 0.5698402909980532) * 2 - 1;
    const radius = Math.pow(fraction(i * 0.4384471871911697), 0.47);
    const latitude = Math.sqrt(Math.max(0, 1 - vertical * vertical));
    const x = radius * latitude * Math.cos(azimuth);
    const y = radius * latitude * Math.sin(azimuth);
    const z = radius * vertical;
    const fold = 1 + 0.1 * Math.sin(azimuth * 3 + z * 2) + 0.055 * Math.cos(y * 5 - x * 2);
    return {
      x: x * 1.34 * fold + 0.065 * Math.sin(y * 4.5),
      y: y * 0.79 * fold + 0.105 * Math.sin(x * 2.7) + z * 0.05,
      z: z * 0.77 + 0.13 * Math.sin(x * 3.1 + y * 2),
      weight: 0.35 + 0.65 * fraction(i * 0.6180339887498949),
    };
  });
}

/** Initial canvas view. Depth is normalized: 0 is far, 1 is near. */
export function projectCloudPoint(point, width, height) {
  const pitch = 0.2;
  const yaw = 0.08;
  const roll = -0.08;
  const y1 = point.y * Math.cos(pitch) - point.z * Math.sin(pitch);
  const z1 = point.y * Math.sin(pitch) + point.z * Math.cos(pitch);
  const x2 = point.x * Math.cos(yaw) + z1 * Math.sin(yaw);
  const z2 = -point.x * Math.sin(yaw) + z1 * Math.cos(yaw);
  const x3 = x2 * Math.cos(roll) - y1 * Math.sin(roll);
  const y3 = x2 * Math.sin(roll) + y1 * Math.cos(roll);
  const perspective = 4.8 / (4.8 - z2);
  return {
    x: width * 0.5 + x3 * width * 0.385 * perspective,
    y: height * 0.52 + y3 * height * 0.415 * perspective,
    depth: clamp((z2 + 1.25) / 2.5, 0, 1),
    perspective,
  };
}
