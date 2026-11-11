// Pure helpers (no DOM access) so they can be unit-tested with `node --test`.

/** Linear scale of `value` from [min, max] to [outMin, outMax]; a flat range maps to the middle. */
export function scale(value, min, max, outMin, outMax) {
  if (max === min) return (outMin + outMax) / 2;
  return outMin + ((value - min) / (max - min)) * (outMax - outMin);
}

/**
 * SVG polyline points for a time series.
 * `values` may contain null/NaN (skipped). Returns {points, min, max}.
 */
export function linePoints(values, width, height, pad = 4) {
  const valid = values.filter((v) => Number.isFinite(v));
  if (valid.length === 0) return { points: "", min: 0, max: 0 };
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const step = values.length > 1 ? (width - 2 * pad) / (values.length - 1) : 0;
  const points = values
    .map((v, i) =>
      Number.isFinite(v)
        ? `${(pad + i * step).toFixed(1)},${scale(v, min, max, height - pad, pad).toFixed(1)}`
        : null,
    )
    .filter((p) => p !== null)
    .join(" ");
  return { points, min, max };
}

/**
 * Project a GPS track to SVG coordinates (equirectangular, longitude corrected by
 * cos(latitude), north up). Keeps the aspect ratio so the track is not distorted.
 */
export function projectTrack(track, width, height, pad = 10) {
  const valid = track.filter((p) => Number.isFinite(p.latitude) && Number.isFinite(p.longitude));
  if (valid.length === 0) return [];
  const midLat = valid.reduce((sum, p) => sum + p.latitude, 0) / valid.length;
  const kx = Math.cos((midLat * Math.PI) / 180);
  const xs = valid.map((p) => p.longitude * kx);
  const ys = valid.map((p) => p.latitude);
  const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
  const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];
  const span = Math.max(maxX - minX, maxY - minY) || 1e-9;
  const inner = Math.min(width, height) - 2 * pad;
  const offX = (width - ((maxX - minX) / span) * inner) / 2;
  const offY = (height - ((maxY - minY) / span) * inner) / 2;
  return valid.map((_, i) => ({
    x: offX + ((xs[i] - minX) / span) * inner,
    y: height - (offY + ((ys[i] - minY) / span) * inner), // SVG y grows downwards
  }));
}

/** Human readable age, e.g. "12 s ago", "3 min ago". */
export function formatAge(isoTimestamp, nowMs = Date.now()) {
  const ageS = Math.max(0, Math.round((nowMs - Date.parse(isoTimestamp)) / 1000));
  if (!Number.isFinite(ageS)) return "unknown";
  if (ageS < 60) return `${ageS} s ago`;
  if (ageS < 3600) return `${Math.floor(ageS / 60)} min ago`;
  if (ageS < 86400) return `${Math.floor(ageS / 3600)} h ago`;
  return `${Math.floor(ageS / 86400)} d ago`;
}
