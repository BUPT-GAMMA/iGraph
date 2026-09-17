// Keep the current label budget near a threshold so tiny zoom reversals do not
// continually swap labels. A deliberate zoom still reveals/hides more detail.
export function labelBudget(scale: number, previous: number, nodeCount: number) {
  const target = Math.min(nodeCount, Math.max(8, Math.floor(20 * scale)));
  const margin = Math.max(2, Math.ceil(previous * .15));
  return previous > 0 && previous <= nodeCount && Math.abs(target - previous) < margin ? previous : target;
}
