/** Missing or malformed measurements are not zero. Numeric API strings are supported. */
export function metricNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && (!value.trim() || !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export type MetricPoint = { epoch: number; value: number };

/** Keep gaps explicit so unmeasured epochs are never interpolated by the chart. */
export function metricSegments(rows: Array<Record<string, unknown>>, key: string): MetricPoint[][] {
  const segments: MetricPoint[][] = [];
  let segment: MetricPoint[] = [];
  for (const row of rows) {
    const epoch = metricNumber(row.epoch);
    const value = metricNumber(row[key]);
    if (epoch === null || value === null) {
      segment = [];
      continue;
    }
    if (!segment.length) segments.push(segment);
    segment.push({ epoch, value });
  }
  return segments;
}
