import type { ModelProvider } from './modelProviders';
import { getModelProvider } from './modelProviders';

type ModelProviderIconProps = {
  modelId?: string;
  provider?: ModelProvider;
  size?: number;
  className?: string;
};

type ProviderAsset = {
  src: string;
  width: number;
  height: number;
  cropX: number;
  cropY: number;
  cropSize: number;
};

const PROVIDER_ASSETS: Partial<Record<ModelProvider, ProviderAsset>> = {
  qwen: { src: '/model-providers/qwen.webp', width: 474, height: 474, cropX: 4, cropY: 4, cropSize: 466 },
  anthropic: { src: '/model-providers/claude.png', width: 520, height: 520, cropX: 30, cropY: 30, cropSize: 465 },
  deepseek: { src: '/model-providers/deepseek.png', width: 1024, height: 1024, cropX: 0, cropY: 0, cropSize: 1024 },
  doubao: { src: '/model-providers/doubao.png', width: 1667, height: 1250, cropX: 356, cropY: 520, cropSize: 238 },
  openai: { src: '/model-providers/openai.png', width: 213, height: 200, cropX: 0, cropY: 0, cropSize: 200 },
};

function ProviderGlyph({ provider }: { provider: ModelProvider }) {
  switch (provider) {
    case 'qwen':
      return <><path d="M12 3.3a4.4 4.4 0 0 1 4.13 2.88 4.4 4.4 0 0 1 2.36 7.17 4.4 4.4 0 0 1-6.1 5.05 4.4 4.4 0 0 1-6.52-4.58A4.4 4.4 0 0 1 12 3.3Z" /><path d="M8.1 8.2c1.35-1.46 3.85-1.5 5.3-.12 1.48 1.4 1.47 3.86.02 5.28-1.42 1.4-3.83 1.4-5.27-.02" /></>;
    case 'anthropic':
      return <><path d="m4.5 19 5.8-14h3.25L19.5 19h-3.25l-1.36-3.45H9.05L7.72 19H4.5Z" fill="currentColor" stroke="none" /><path d="m10.12 12.75 1.84-4.8 1.86 4.8h-3.7Z" fill="white" stroke="none" /></>;
    case 'deepseek':
      return <><path d="M3.2 13.25c2.15.25 3.62-.4 4.42-1.95 1.05 1.67 2.8 2.72 5.22 3.13 2.45.42 4.82-.15 7.12-1.7-.55 4.5-3.38 7.02-7.85 7.02-4.18 0-7.26-2.16-8.91-6.5Z" /><path d="M16.65 9.15c.78-1.76 2.14-2.7 4.1-2.82-.47 1.9-1.7 3.08-3.67 3.55M7.55 11.4c.08-3.35 2.1-5.45 5.62-5.76" /><circle cx="13.1" cy="10.3" r=".85" fill="currentColor" stroke="none" /></>;
    case 'doubao':
      return <><path d="M7.2 4.4c2.45-1.42 5.3-.43 6.2 1.65.75 1.72-.07 3.62-1.88 4.66-2.34 1.35-5.25.55-6.2-1.54-.78-1.72.07-3.72 1.88-4.77Z" /><path d="M12.35 13.17c2.2-1.72 5.18-1.26 6.38.72 1 1.65.43 3.72-1.2 4.98-2.15 1.68-5.16 1.32-6.37-.7-1.03-1.7-.46-3.71 1.19-5Z" /></>;
    case 'openai':
      return <><path d="M9.3 4.05A4.15 4.15 0 0 1 16 7.32a4.15 4.15 0 0 1 1.52 7.32 4.15 4.15 0 0 1-6.72 3.25 4.15 4.15 0 0 1-5.15-5.26A4.15 4.15 0 0 1 9.3 4.05Z" /><path d="m8.15 9.72 3.9-2.25 3.85 2.22v4.52l-3.9 2.25-3.85-2.22V9.72Zm3.9-2.25v4.47l3.85 2.27M8.15 9.72 12 11.94v4.52" /></>;
    case 'google':
      return <><path d="M12 2.7c.4 4.95 3.02 8.4 8.25 9.3-5.23.9-7.85 4.35-8.25 9.3-.4-4.95-3.02-8.4-8.25-9.3C8.98 11.1 11.6 7.65 12 2.7Z" /><path d="M19 3.9c.1 1.5.9 2.52 2.45 2.8-1.55.27-2.35 1.3-2.45 2.8-.1-1.5-.9-2.53-2.45-2.8C18.1 6.42 18.9 5.4 19 3.9Z" /></>;
    case 'meta':
      return <path d="M3 15.85C4.2 8.65 6.28 5.4 8.68 5.4c2.72 0 4.18 5.2 6.5 8.45 1.2 1.67 2.18 2.35 3.13 2.35 1.38 0 2.2-1.63 2.69-4.2-.78-4.52-2.25-6.6-4.37-6.6-2.7 0-4.47 4.57-6.67 7.95C8.52 15.57 7.45 16.2 6.4 16.2 5.05 16.2 3.92 14.75 3 12" />;
    case 'mistral':
      return <path d="M4 5h4v3h2V5h4v3h2V5h4v14h-4v-7h-2v4h-4v-4H8v7H4V5Z" fill="currentColor" stroke="none" />;
    case 'xai':
      return <><path d="M4 4.5 19.5 20M19.5 4 4 19.5" /><path d="M14.8 4H20v5.2" /></>;
    case 'moonshot':
      return <path d="M17.65 17.65A8 8 0 0 1 7 6.95a8.15 8.15 0 1 0 10.65 10.7Z" fill="currentColor" stroke="none" />;
    case 'zhipu':
      return <><path d="m12 3.2 7.6 4.4v8.8L12 20.8l-7.6-4.4V7.6L12 3.2Z" /><path d="M8.3 8.4h7.4l-7.4 7.2h7.4" /></>;
    default:
      return <><path d="m12 3-1.35 5.65L5 10l5.65 1.35L12 17l1.35-5.65L19 10l-5.65-1.35L12 3Z" /><path d="m19 16-.65 2.35L16 19l2.35.65L19 22l.65-2.35L22 19l-2.35-.65L19 16Z" /></>;
  }
}

export function ModelProviderIcon({ modelId = '', provider, size = 14, className }: ModelProviderIconProps) {
  const resolvedProvider = provider ?? getModelProvider(modelId);
  const asset = PROVIDER_ASSETS[resolvedProvider];
  if (asset) {
    const scale = size / asset.cropSize;
    return <span
      className={`${className || ''} model-provider-image`.trim()}
      style={{ width: size, height: size }}
      aria-hidden="true"
      data-model-provider={resolvedProvider}
      data-provider-asset="image"
    ><img
      src={asset.src}
      alt=""
      draggable={false}
      style={{
        width: asset.width * scale,
        height: asset.height * scale,
        left: -asset.cropX * scale,
        top: -asset.cropY * scale,
      }}
    /></span>;
  }
  return <svg
    className={className}
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    focusable="false"
    data-model-provider={resolvedProvider}
  ><ProviderGlyph provider={resolvedProvider} /></svg>;
}
