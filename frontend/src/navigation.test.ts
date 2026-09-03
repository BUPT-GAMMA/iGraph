import { describe, expect, it, vi } from 'vitest';
import { parseWorkspaceRoute, workspaceRouteUrl, writeWorkspaceRoute } from './navigation';

describe('workspace navigation', () => {
  it('parses home, project, draft and session routes', () => {
    expect(parseWorkspaceRoute('')).toEqual({ kind: 'home' });
    expect(parseWorkspaceRoute('?project=project-1')).toEqual({ kind: 'project', id: 'project-1' });
    expect(parseWorkspaceRoute('?project=project-1&new=1')).toEqual({ kind: 'new-session', projectId: 'project-1' });
    expect(parseWorkspaceRoute('?session=session-1')).toEqual({ kind: 'session', id: 'session-1' });
  });

  it('builds a home URL without leaving a deleted resource id behind', () => {
    expect(workspaceRouteUrl('http://localhost:21108/?session=deleted#content', { kind: 'home' })).toBe('/#content');
    expect(workspaceRouteUrl('http://localhost:21108/?project=old&theme=dark', { kind: 'home' })).toBe('/?theme=dark');
  });

  it('keeps session and project parameters mutually exclusive', () => {
    expect(workspaceRouteUrl('http://localhost:21108/?project=old&session=old', { kind: 'session', id: 'next' })).toBe('/?session=next');
    expect(workspaceRouteUrl('http://localhost:21108/?session=old', { kind: 'project', id: 'project-2' })).toBe('/?project=project-2');
    expect(workspaceRouteUrl('http://localhost:21108/?session=old', { kind: 'new-session', projectId: 'project-2' })).toBe('/?project=project-2&new=1');
  });

  it('uses replaceState when removing an invalid or deleted route', () => {
    const replace = vi.spyOn(window.history, 'replaceState');
    writeWorkspaceRoute({ kind: 'home' }, true);
    expect(replace).toHaveBeenCalledWith({}, '', '/');
    replace.mockRestore();
  });
});
