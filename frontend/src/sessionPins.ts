export const pinKey = (userId: string) => 'igraph-pinned-sessions:v1:' + encodeURIComponent(userId);

export function readPins(userId: string): Set<string> {
  try {
    const value = JSON.parse(localStorage.getItem(pinKey(userId)) || '[]');
    return new Set(Array.isArray(value) ? value.filter(item => typeof item === 'string' && item.length > 0) : []);
  } catch { return new Set(); }
}
