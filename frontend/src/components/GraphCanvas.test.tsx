import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { zoomIdentity } from 'd3';
import { GraphCanvas, createGraphCanvasState } from './GraphCanvas';
import { I18nProvider } from '../i18n';
import { graphIndex, previewGraph } from '../graphPreview';

afterEach(cleanup);

const preview = previewGraph(graphIndex({ nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ source: 'a', target: 'b' }] }), [], '', false, false, '', 0);
function canvasProps(layout: 'circle' | 'force' | 'radial' = 'circle') {
  return { active: true, preview, selected: null, onSelect: vi.fn(), layout, sizeMode: 'degree' as const, labels: 'auto' as const,
    edgeWidth: 1, fitVersion: 0, focusId: '', highlights: [], communities: new Map<string, string>(), state: createGraphCanvasState() };
}

describe('GraphCanvas state restoration', () => {
  it.each(['circle', 'force', 'radial'] as const)('restores exact positions and camera in %s layout', async layout => {
    const props = canvasProps(layout);
    const first = render(<GraphCanvas {...props} />);
    first.unmount();
    // A saved, manually positioned graph must not be laid out again on remount.
    props.state.positions = new Map([['a', { x: 15, y: 25 }], ['b', { x: 250, y: 180 }]]);
    props.state.camera = zoomIdentity.translate(-140, -220).scale(2.5);
    const { container, rerender } = render(<GraphCanvas {...props} />);
    const assertSaved = () => {
      expect(container.querySelector('[data-node-id="a"]')?.getAttribute('transform')).toBe('translate(15,25)');
      expect(container.querySelector('[data-node-id="b"]')?.getAttribute('transform')).toBe('translate(250,180)');
      expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBe(props.state.camera.toString());
    };
    assertSaved();
    rerender(<GraphCanvas {...props} highlights={[{ type: 'node', nodes: ['a'] }]} />);
    assertSaved();
    rerender(<GraphCanvas {...props} active={false} />);
    rerender(<GraphCanvas {...props} active />);
    await waitFor(assertSaved);
    expect(props.state.camera.k).toBe(2.5);
  });

  it('does not replay old focus after zoom, language changes, appearance changes or hiding', async () => {
    const props = { ...canvasProps(), focusId: 'a' };
    const view = (language: 'en' | 'zh', active = true, color = '0') => <I18nProvider key={language} initialLanguage={language}><GraphCanvas {...props} active={active} communities={new Map([['a', color]])} /></I18nProvider>;
    const { container, rerender } = render(view('zh'));
    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBe(props.state.camera.toString()));
    fireEvent.wheel(container.querySelector('svg')!, { deltaY: -240, clientX: 40, clientY: 80 });
    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBe(props.state.camera.toString()));
    const camera = props.state.camera.toString();
    rerender(view('en'));
    expect(props.state.camera.toString()).toBe(camera);
    rerender(view('en', true, '1'));
    expect(props.state.camera.toString()).toBe(camera);
    rerender(view('en', false));
    rerender(view('en'));
    expect(props.state.camera.toString()).toBe(camera);
    rerender(<GraphCanvas {...props} focusId="b" />);
    expect(props.state.camera.toString()).not.toBe(camera);
    expect(props.state.appliedFocusId).toBe('b');
  });

  it('defers fit until the canvas is active and applies it only once', () => {
    const props = canvasProps();
    const { rerender } = render(<GraphCanvas {...props} />);
    rerender(<GraphCanvas {...props} active={false} fitVersion={1} />);
    expect(props.state.appliedFitVersion).toBe(0);
    props.state.camera = zoomIdentity.translate(-300, -200).scale(4);
    rerender(<GraphCanvas {...props} fitVersion={1} />);
    expect(props.state.appliedFitVersion).toBe(1);
    expect(props.state.camera.k).not.toBe(4);
    const camera = props.state.camera;
    rerender(<GraphCanvas {...props} fitVersion={1} communities={new Map([['a', '1']])} />);
    expect(props.state.camera).toEqual(camera);
  });

  it('recomputes circular positions only on an explicit layout change', () => {
    const props = canvasProps('force');
    const { container, rerender } = render(<GraphCanvas {...props} />);
    rerender(<GraphCanvas {...props} layout="circle" />);
    expect(container.querySelector('[data-node-id="a"]')?.getAttribute('transform')).toBe('translate(302.4,210)');
    expect(container.querySelector('[data-node-id="b"]')?.getAttribute('transform')).toBe('translate(57.599999999999994,210.00000000000003)');
  });
});
