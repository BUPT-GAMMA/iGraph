export type KnownModelProvider =
  | 'qwen'
  | 'anthropic'
  | 'deepseek'
  | 'doubao'
  | 'openai'
  | 'google'
  | 'meta'
  | 'mistral'
  | 'xai'
  | 'moonshot'
  | 'zhipu';

export type ModelProvider = KnownModelProvider | 'unknown';

export type ModelProviderMeta = {
  provider: ModelProvider;
  /** Stable grouping key. Unknown model IDs deliberately receive distinct keys. */
  key: string;
  label: string;
};

type ProviderDefinition = {
  provider: KnownModelProvider;
  label: string;
  patterns: RegExp[];
};

const PROVIDERS: ProviderDefinition[] = [
  { provider: 'qwen', label: 'Qwen', patterns: [/(?:^|[\s/_.:-])qwen(?:$|[\s/_.:-]|\d)/, /(?:^|[\s/_.:-])q(?:wq|vq)(?:$|[\s/_.:-]|\d)/, /tongyi/] },
  { provider: 'anthropic', label: 'Anthropic', patterns: [/claude/, /anthropic/] },
  { provider: 'deepseek', label: 'DeepSeek', patterns: [/deep[\s/_.:-]?seek/] },
  { provider: 'doubao', label: 'Doubao', patterns: [/doubao/, /豆包/, /volcengine/] },
  { provider: 'openai', label: 'OpenAI', patterns: [/(?:^|[\s/_.:-])gpt(?:$|[\s/_.:-]|\d)/, /(?:^|[\s/_.:-])o[134](?:$|[\s/_.:-]|\d)/, /openai/, /chatgpt/, /(?:^|[\s/_.:-])codex(?:$|[\s/_.:-]|\d)/] },
  { provider: 'google', label: 'Google', patterns: [/gemini/, /google/] },
  { provider: 'meta', label: 'Meta', patterns: [/llama/, /(?:^|[\s/_.:-])meta(?:$|[\s/_.:-]|\d)/] },
  { provider: 'mistral', label: 'Mistral AI', patterns: [/mistral/, /mixtral/, /codestral/] },
  { provider: 'xai', label: 'xAI', patterns: [/(?:^|[\s/_.:-])grok(?:$|[\s/_.:-]|\d)/, /(?:^|[\s/_.:-])xai(?:$|[\s/_.:-]|\d)/] },
  { provider: 'moonshot', label: 'Moonshot AI', patterns: [/kimi/, /moonshot/] },
  { provider: 'zhipu', label: 'Zhipu AI', patterns: [/(?:^|[\s/_.:-])glm(?:$|[\s/_.:-]|\d)/, /chatglm/, /zhipu/] },
];

const LABELS: Record<ModelProvider, string> = {
  qwen: 'Qwen',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  doubao: 'Doubao',
  openai: 'OpenAI',
  google: 'Google',
  meta: 'Meta',
  mistral: 'Mistral AI',
  xai: 'xAI',
  moonshot: 'Moonshot AI',
  zhipu: 'Zhipu AI',
  unknown: 'Model',
};

function normalizeModelId(modelId: string) {
  return modelId.trim().toLocaleLowerCase('en-US');
}

export function getModelProvider(modelId: string): ModelProvider {
  const normalized = normalizeModelId(modelId);
  return PROVIDERS.find(candidate => candidate.patterns.some(pattern => pattern.test(normalized)))?.provider ?? 'unknown';
}

export function getModelProviderMeta(modelId: string): ModelProviderMeta {
  const normalized = normalizeModelId(modelId);
  const definition = PROVIDERS.find(candidate => candidate.patterns.some(pattern => pattern.test(normalized)));
  if (definition) return { provider: definition.provider, key: definition.provider, label: definition.label };
  return { provider: 'unknown', key: `unknown:${normalized}`, label: LABELS.unknown };
}

export function getModelProviderLabel(provider: ModelProvider) {
  return LABELS[provider];
}

const QUALITY_TIERS: Array<[RegExp, number]> = [
  [/(?:^|[\s/_.:-])max(?:$|[\s/_.:-]|\d)/, 1_000],
  [/(?:^|[\s/_.:-])ultra(?:$|[\s/_.:-]|\d)/, 950],
  [/(?:^|[\s/_.:-])opus(?:$|[\s/_.:-]|\d)/, 900],
  [/(?:^|[\s/_.:-])pro(?:$|[\s/_.:-]|\d)/, 800],
  [/(?:^|[\s/_.:-])sonnet(?:$|[\s/_.:-]|\d)/, 700],
  [/(?:^|[\s/_.:-])plus(?:$|[\s/_.:-]|\d)/, 600],
  [/(?:^|[\s/_.:-])large(?:$|[\s/_.:-]|\d)/, 550],
  [/(?:^|[\s/_.:-])turbo(?:$|[\s/_.:-]|\d)/, 500],
  [/(?:^|[\s/_.:-])flash(?:$|[\s/_.:-]|\d)/, 350],
  [/(?:^|[\s/_.:-])small(?:$|[\s/_.:-]|\d)/, 250],
  [/(?:^|[\s/_.:-])mini(?:$|[\s/_.:-]|\d)/, 200],
  [/(?:^|[\s/_.:-])lite(?:$|[\s/_.:-]|\d)/, 150],
  [/(?:^|[\s/_.:-])nano(?:$|[\s/_.:-]|\d)/, 100],
];

function qualityTier(modelId: string) {
  const normalized = normalizeModelId(modelId);
  return QUALITY_TIERS.find(([pattern]) => pattern.test(normalized))?.[1] ?? 0;
}

function versionParts(modelId: string) {
  return Array.from(normalizeModelId(modelId).matchAll(/\d+/g), match => Number(match[0]));
}

function compareVersions(left: string, right: string) {
  const leftParts = versionParts(left);
  const rightParts = versionParts(right);
  const length = Math.max(leftParts.length, rightParts.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference) return difference;
  }
  const leftLatest = /(?:^|[\s/_.:-])latest(?:$|[\s/_.:-])/.test(normalizeModelId(left));
  const rightLatest = /(?:^|[\s/_.:-])latest(?:$|[\s/_.:-])/.test(normalizeModelId(right));
  return Number(leftLatest) - Number(rightLatest);
}

function isPreferredModel(candidate: string, current: string) {
  const tierDifference = qualityTier(candidate) - qualityTier(current);
  if (tierDifference) return tierDifference > 0;
  return compareVersions(candidate, current) > 0;
}

/**
 * Keeps one built-in model per recognised company without changing company order.
 * The currently selected model wins its company; otherwise the strongest tier and
 * newest version win. Unknown IDs only collapse exact (case-insensitive) repeats.
 */
export function selectRepresentativeModels(models: readonly string[], selectedModel?: string) {
  const selected = selectedModel?.trim();
  const groups = new Map<string, { modelId: string; hasSelected: boolean }>();

  for (const rawModelId of models) {
    const modelId = rawModelId.trim();
    if (!modelId) continue;
    const key = getModelProviderMeta(modelId).key;
    const isSelected = Boolean(selected && modelId === selected);
    const current = groups.get(key);
    if (!current) {
      groups.set(key, { modelId, hasSelected: isSelected });
      continue;
    }
    if (isSelected || (!current.hasSelected && isPreferredModel(modelId, current.modelId))) {
      groups.set(key, { modelId, hasSelected: isSelected });
    }
  }

  return Array.from(groups.values(), group => group.modelId);
}
