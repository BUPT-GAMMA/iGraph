export type WorkspaceRoute =
  | { kind: 'home' }
  | { kind: 'project'; id: string }
  | { kind: 'session'; id: string }
  | { kind: 'new-session'; projectId: string | null };

export function parseWorkspaceRoute(search: string): WorkspaceRoute {
  const query = new URLSearchParams(search);
  const projectId = query.get('project')?.trim();
  const sessionId = query.get('session')?.trim();
  if (query.get('new') === '1') return { kind: 'new-session', projectId: projectId || null };
  if (projectId) return { kind: 'project', id: projectId };
  if (sessionId) return { kind: 'session', id: sessionId };
  return { kind: 'home' };
}

export function workspaceRouteUrl(href: string, route: WorkspaceRoute) {
  const url = new URL(href);
  url.searchParams.delete('project');
  url.searchParams.delete('session');
  url.searchParams.delete('new');
  if (route.kind === 'new-session') {
    if (route.projectId) url.searchParams.set('project', route.projectId);
    url.searchParams.set('new', '1');
  } else if (route.kind !== 'home') url.searchParams.set(route.kind, route.id);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function writeWorkspaceRoute(route: WorkspaceRoute, replace = false) {
  const nextUrl = workspaceRouteUrl(window.location.href, route);
  if (replace) window.history.replaceState({}, '', nextUrl);
  else window.history.pushState({}, '', nextUrl);
}
