import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ModelProviderIcon } from './ModelProviderIcon';
import { getModelProvider, getModelProviderMeta, selectRepresentativeModels } from './modelProviders';

afterEach(cleanup);

describe('modelProviders', () => {
  it('recognises supported companies from common model ID forms', () => {
    expect([
      'qwen3-max',
      'anthropic/claude-opus-4-1',
      'deepseek-chat',
      'doubao-seed-1-6-pro',
      'openai/gpt-5.2',
      'gemini-2.5-pro',
      'meta-llama-3.3-70b',
      'mistral-large-latest',
      'xai/grok-4',
      'moonshot/kimi-k2',
      'zhipu/glm-4.5',
    ].map(getModelProvider)).toEqual([
      'qwen', 'anthropic', 'deepseek', 'doubao', 'openai', 'google',
      'meta', 'mistral', 'xai', 'moonshot', 'zhipu',
    ]);
  });

  it('keeps the selected model for its company and picks stronger, newer representatives elsewhere', () => {
    expect(selectRepresentativeModels([
      'qwen3-plus',
      'claude-3-5-sonnet',
      'deepseek-v3',
      'qwen3-max',
      'claude-opus-4-1',
      'gpt-4o-mini',
      'gpt-5-pro',
      'doubao-lite-32k',
      'doubao-pro-32k',
      'deepseek-v3.2',
    ], 'qwen3-plus')).toEqual([
      'qwen3-plus',
      'claude-opus-4-1',
      'deepseek-v3.2',
      'gpt-5-pro',
      'doubao-pro-32k',
    ]);
  });

  it('does not fold unrelated unknown IDs together', () => {
    expect(getModelProviderMeta('model-a').key).not.toBe(getModelProviderMeta('model-b').key);
    expect(selectRepresentativeModels(['model-a', 'model-b', 'MODEL-A', ''])).toEqual(['model-a', 'model-b']);
  });

  it('renders a lightweight, hidden provider glyph with a testable provider marker', () => {
    const { container, rerender } = render(<ModelProviderIcon modelId="qwen3-max" size={16} />);
    let icon = container.querySelector('[data-model-provider]') as HTMLElement;
    expect(icon.getAttribute('data-model-provider')).toBe('qwen');
    expect(icon.getAttribute('data-provider-asset')).toBe('image');
    expect(icon.querySelector('img')?.getAttribute('src')).toBe('/model-providers/qwen.webp');
    expect(icon.getAttribute('aria-hidden')).toBe('true');
    expect(icon.style.width).toBe('16px');

    rerender(<ModelProviderIcon provider="anthropic" />);
    icon = container.querySelector('[data-model-provider]') as HTMLElement;
    expect(icon.getAttribute('data-model-provider')).toBe('anthropic');
    expect(icon.querySelector('img')?.getAttribute('src')).toBe('/model-providers/claude.png');
  });

  it('uses the supplied image asset for every configured live provider', () => {
    const expectedAssets = [
      ['qwen3.6-plus', 'qwen.webp'],
      ['claude-opus-4-6', 'claude.png'],
      ['deepseek-v4-pro', 'deepseek.png'],
      ['doubao-seed-2-0-pro-260215', 'doubao.png'],
      ['gpt-5-4', 'openai.png'],
    ];

    expectedAssets.forEach(([modelId, filename]) => {
      const { container, unmount } = render(<ModelProviderIcon modelId={modelId} />);
      expect(container.querySelector('[data-model-provider]')?.getAttribute('data-provider-asset')).toBe('image');
      expect(container.querySelector('img')?.getAttribute('src')).toBe(`/model-providers/${filename}`);
      unmount();
    });
  });

  it('keeps inline glyph fallbacks for providers without supplied artwork', () => {
    const { container, rerender } = render(<ModelProviderIcon modelId="gemini-2.5-pro" />);
    expect(container.querySelector('svg')?.getAttribute('data-provider-asset')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('path')).toBeTruthy();

    rerender(<ModelProviderIcon modelId="private-model" />);
    expect(container.querySelector('svg')?.getAttribute('data-model-provider')).toBe('unknown');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('path')).toBeTruthy();
  });
});
