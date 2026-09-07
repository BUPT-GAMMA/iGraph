export type Session = {
  session_id: string;
  project_id?: string | null;
  title?: string;
  preview?: string;
  last_message?: string;
  updated_at?: string;
  created_at?: string;
  has_graph?: boolean;
  model?: string;
  active_dataset_id?: string | null;
};

export type Project = {
  project_id: string;
  user_id: string;
  name: string;
  description?: string;
  is_default?: boolean;
  default_dataset_id?: string | null;
  conversation_count?: number;
  dataset_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type ToolCall = {
  tool?: string;
  call_id?: string;
  args?: unknown;
  done?: boolean;
  progress?: Record<string, unknown>;
  result?: unknown;
  history?: Array<Record<string, unknown>>;
};
export type ProjectTrainingRun = ToolCall & {
  training_run_id: string;
  session_id: string;
  session_title?: string;
  dataset_id?: string | null;
  dataset_name?: string | null;
  session_updated_at?: string | null;
  sync_error?: string;
  status_unavailable?: boolean;
};
export type Plan = { steps?: Array<{ step_id?: number; goal?: string; status?: string; result_summary?: string }>; completed?: boolean };
export type GraphSnapshot = {
  id?: string;
  nodes: Array<Record<string, unknown>>;
  edges?: Array<Record<string, unknown>>;
  links?: Array<Record<string, unknown>>;
  directed?: boolean;
  num_nodes?: number;
  num_edges?: number;
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
  truncated_to?: number;
  community?: GraphCommunity | null;
  communities?: GraphCommunity | null;
  highlights?: GraphHighlights | null;
};

export type Message = {
  run_id?: string;
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  tools?: ToolCall[];
  plan?: Plan | null;
  graphs?: GraphSnapshot[];
  reports?: Array<Record<string, unknown>>;
  streaming?: boolean;
};

export type GraphCommunity = {
  num_communities?: number;
  node_community?: Record<string, number | string>;
};

export type Highlight = {
  id?: string;
  type?: 'node' | 'path' | 'subgraph' | string;
  nodes?: unknown[];
  edges?: Array<Record<string, unknown> | unknown[]>;
  color?: string;
  label?: string;
};

export type GraphHighlights = Highlight[] | {
  nodes?: Array<{ ids?: unknown[]; color?: string; label?: string }>;
  paths?: Array<{ node_sequence?: unknown[]; edges?: unknown[]; color?: string; label?: string }>;
  subgraphs?: Array<{ nodes?: unknown[]; edges?: unknown[]; color?: string; label?: string }>;
};

export type GraphData = {
  success?: boolean;
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  directed?: boolean;
  num_nodes?: number;
  num_edges?: number;
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
  truncated_to?: number;
  community?: GraphCommunity | null;
  communities?: GraphCommunity | null;
  highlights?: GraphHighlights | null;
};

export type GraphCollectionSummary = {
  num_graphs?: number;
  num_tasks?: number;
  metric?: string;
  split_sizes?: Record<string, number>;
  node_vocab_size?: number;
  edge_vocab_size?: number;
  has_missing_labels?: boolean;
  missing_label_rate?: number;
  num_edgeless_graphs?: number;
  uses_edge_features?: boolean;
  semantic_type?: string;
  storage_type?: string;
};

export type DatasetView =
  | {
      kind: 'single_graph';
      visualization: { supported: true; code?: null };
      summary?: Record<string, unknown>;
    }
  | {
      kind: 'graph_collection';
      visualization: { supported: false; code: 'NON_SINGLE_GRAPH' };
      summary: GraphCollectionSummary;
    };

export type GraphViewState =
  | { status: 'empty'; data: null }
  | { status: 'loading'; data: null }
  | { status: 'ready'; data: GraphData }
  | { status: 'unsupported'; data: null; code: 'NON_SINGLE_GRAPH'; summary: GraphCollectionSummary }
  | { status: 'failed'; data: null; error: string };

export function isGraphData(value: unknown): value is GraphData {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const graph = value as Partial<GraphData>;
  return graph.success !== false && Array.isArray(graph.nodes) && Array.isArray(graph.edges);
}

export type Dataset = {
  dataset_id: string;
  project_id: string | null;
  name: string;
  original_filename: string;
  file_type: string;
  metadata?: Record<string, unknown>;
  status: string;
  is_default?: boolean;
  created_at?: string;
  updated_at?: string;
};
export type AttachmentRef = { file_token: string; filename: string };
export type DraftPhase = 'editing' | 'creating_session' | 'uploading' | 'starting_run' | 'failed';
export type DraftAttachment = {
  localId: string;
  filename: string;
  file?: File;
  fileToken?: string;
  status: 'staged' | 'uploading' | 'uploaded' | 'failed' | 'needs_reselect';
  error?: string;
};
export type NewConversationDraft = {
  draftId: string;
  projectId: string | null;
  phase: DraftPhase;
  text: string;
  model: string;
  activeDatasetId: string | null;
  attachments: DraftAttachment[];
  error?: string;
};
export type FeedbackRequest = {
  run_id?: string;
  request_id?: string;
  call_id?: string;
  tool_name?: string;
  proposed_args?: Record<string, unknown>;
  description?: string;
};
export type StreamEvent = { type?: string; [key: string]: any };
export type ActiveRun = {
  run_id: string;
  status: string;
  last_event_index: number;
};
export type StreamBatch = {
  protocol_version?: 1 | 2;
  session_id: string;
  run_id: string;
  event_index?: number | null;
  events: StreamEvent[];
};
