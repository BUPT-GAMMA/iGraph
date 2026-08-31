export type WorkspaceRoute =
  | { kind: 'home' }
  | { kind: 'project'; id: string }
  | { kind: 'session'; id: string };

export function parseWorkspaceRoute(search: string): WorkspaceRoute {
  const query = new URLSearchParams(search);
  const projectId = query.get('project')?.trim();
  const sessionId = query.get('session')?.trim();
  if (projectId) return { kind: 'project', id: projectId };
  if (sessionId) return { kind: 'session', id: sessionId };
  return { kind: 'home' };
}

export function workspaceRouteUrl(href: string, route: WorkspaceRoute) {
  const url = new URL(href);
  url.searchParams.delete('project');
  url.searchParams.delete('session');
  if (route.kind !== 'home') url.searchParams.set(route.kind, route.id);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function writeWorkspaceRoute(route: WorkspaceRoute, replace = false) {
  const nextUrl = workspaceRouteUrl(window.location.href, route);
  if (replace) window.history.replaceState({}, '', nextUrl);
  else window.history.pushState({}, '', nextUrl);
}
