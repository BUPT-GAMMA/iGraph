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
  job_id?: string;
  call_id?: string;
  args?: unknown;
  done?: boolean;
  progress?: Record<string, unknown>;
  result?: unknown;
  history?: Array<Record<string, unknown>>;
  status?: 'running' | 'completed' | 'failed' | 'cancelled';
};
export type ProcessItem = { id: string; kind: 'reasoning' | 'text' | 'tool'; sequence: number; content?: string; tool?: ToolCall };
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
  degree_scope?: 'full_graph' | 'subgraph';
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
  run_status?: string;
  regenerate_of?: string | null;
  run_id?: string;
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  tools?: ToolCall[];
  plan?: Plan | null;
  graphs?: GraphSnapshot[];
  reports?: Array<Record<string, unknown>>;
  attachments?: AttachmentRef[];
  artifacts?: ArtifactRef[];
  streaming?: boolean;
  thinking?: Array<{ phase?: string; text?: string; streaming?: boolean }>;
  processItems?: ProcessItem[];
  variants?: Array<{ content: string; tools?: ToolCall[]; graphs?: GraphSnapshot[]; reports?: Array<Record<string, unknown>> }>;
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
  schema_kind?: 'tables';
  degree_scope?: 'full_graph' | 'subgraph';
  success?: boolean;
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  visualization_kind?: 'instances' | 'schema' | string;
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

export type PartitionedGraphSummary = {
  num_nodes?: number;
  num_edges?: number;
  directed?: boolean;
  graph_version?: number;
  partitions?: number;
  representation?: 'partitioned_snapshot' | string;
  has_weights?: boolean;
  storage_bytes?: number;
  preparation_status?: 'queued' | 'running' | string;
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
    }
  | {
      kind: 'partitioned_graph';
      visualization: { supported: false; code: 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE' };
      summary: PartitionedGraphSummary;
    };

export type GraphViewState =
  | { status: 'empty'; data: null }
  | { status: 'loading'; data: null }
  | { status: 'ready'; data: GraphData }
  | { status: 'unsupported'; data: null; code: 'NON_SINGLE_GRAPH'; summary: GraphCollectionSummary }
  | { status: 'unsupported'; data: null; code: 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE'; summary: PartitionedGraphSummary }
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
  description?: string;
  original_filename: string;
  file_type: string;
  metadata?: Record<string, unknown>;
  status: string;
  source_kind?: 'upload' | 'server' | string;
  source_id?: string | null;
  is_default?: boolean;
  created_at?: string;
  updated_at?: string;
};
export type CsvForeignKey = {
  columns: string[];
  ref_table: string;
  ref_columns: string[];
};
export type CsvBundleTableSchema = {
  file_name: string;
  name: string;
  columns: string[];
  primary_key: string[];
  foreign_keys: CsvForeignKey[];
};
export type CsvBundleSchema = {
  version: 'csv_bundle.v1';
  description?: string;
  tables: CsvBundleTableSchema[];
};

export type ConstructionState = {
  dataset_id: string;
  build_id?: string | null;
  failure_code?: string | null;
  status: 'not_built' | 'algorithm_unavailable' | 'building' | 'ready' | 'failed';
  revision: number;
  node_count?: number;
  edge_count?: number;
  directed?: boolean;
  source_filename?: string;
  source_type?: string;
  algorithm_available?: boolean;
  message?: string;
  updated_at?: string;
  last_build_status?: string;
  progress?: ConstructionProgress;
};
export type ConstructionProgressEvent = {
  id: string;
  kind: 'stage' | 'candidate' | 'terminal';
  stage: string;
  message: string;
  outcome?: 'accepted' | 'rejected' | 'ready' | 'failed';
  timestamp?: string;
};
export type ConstructionProgress = {
  version: 'construction_progress.v1';
  status?: string;
  stage: string;
  current_message?: string;
  elapsed_seconds?: number;
  updated_at?: string;
  metrics?: {
    query_count?: number;
    max_iterations?: number;
    iterations?: number;
    tool_calls?: number;
    evaluated_candidates?: number;
    accepted_candidates?: number;
    initial_nodes?: number;
    initial_edges?: number;
    chunks_completed?: number;
    chunks_total?: number;
    extracted_nodes?: number;
    extracted_edges?: number;
  };
  events: ConstructionProgressEvent[];
};
export type ConstructionDomainCategory = 'auto' | 'finance' | 'academic' | 'commerce' | 'healthcare' | 'social' | 'general';
export type ConstructionBuildOptions = {
  mode?: 'llm' | 'rules';
  query_count?: number;
  domain?: string;
  domain_category?: ConstructionDomainCategory;
  experience_enabled?: boolean;
  delimiter?: string;
  txt_mode?: 'auto' | 'lines' | 'entities';
};
export type ConstructionOperation =
  | { op: 'collapse_node'; node_id: string; edge_type: string }
  | { op: 'node_property_to_edge'; node_id: string; property: string; edge_ids: string[]; target_property?: string; remove_from_node: boolean }
  | { op: 'extract_property_node'; node_ids: string[]; property: string; node_type: string; edge_type: string; remove_from_node: boolean }
  | { op: 'delete'; target: 'node' | 'edge'; ids: string[]; property?: string; cascade?: boolean }
  | { op: 'reverse_edges'; edge_ids: string[] }
  | { op: 'rename_type'; target: 'node' | 'edge'; old_type: string; new_type: string };
export type ConstructionGraph = { dataset_id: string; revision: number; graph: GraphData };
export type ConstructionSchemaPropertyPath = {
  path: string;
  types?: string[];
  type_counts?: Record<string, number>;
  present_count?: number;
  value_count?: number;
};
export type ConstructionSchemaNodeType = {
  id: string;
  type: string;
  type_name?: string;
  instance_count: number;
  property_paths?: ConstructionSchemaPropertyPath[];
};
export type ConstructionSchemaEdgeType = {
  id: string;
  type: string;
  type_name?: string;
  source_type: string;
  target_type: string;
  relationship_count: number;
  property_paths?: ConstructionSchemaPropertyPath[];
};
export type ConstructionSourceTable = {
  name: string;
  columns: string[];
  column_types?: Record<string, string>;
  primary_key: string[];
  foreign_keys: CsvForeignKey[];
};
export type ConstructionSchema = {
  source_tables?: ConstructionSourceTable[];
  directed?: boolean;
  node_count?: number;
  edge_count?: number;
  node_type_count: number;
  edge_type_count: number;
  node_types: ConstructionSchemaNodeType[];
  edge_types: ConstructionSchemaEdgeType[];
  graph: GraphData;
};
export type ConstructionSchemaResponse = { dataset_id: string; revision: number; schema: ConstructionSchema };
export type ConstructionResult = ConstructionGraph & {
  success: boolean;
  status: 'preview' | 'ready';
  base_revision: number;
  summary: {
    before: { node_count: number; edge_count: number };
    after: { node_count: number; edge_count: number };
    changed_nodes: number;
    changed_edges: number;
  };
};

export type ServerDataset = {
  server_dataset_id: string;
  revision?: string;
  name: string;
  original_filename: string;
  relative_path: string;
  file_type: string;
  size_bytes: number;
  modified_at?: number;
  directed?: boolean;
  metadata_error?: string;
  preparation?: {
    status: 'not_required' | 'not_prepared' | 'queued' | 'running' | 'ready' | 'failed' | 'cancelled' | 'unavailable' | string;
    progress?: number;
    message?: string;
    error?: string;
    job_id?: string;
    graph_version?: number;
    num_nodes?: number;
    num_edges?: number;
    partitions?: number;
    directed?: boolean;
    has_weights?: boolean;
    storage_bytes?: number | Record<string, number>;
  };
};
export type AttachmentRef = { file_token: string; filename: string };
/** A Derived workspace file the agent published as a deliverable for one turn. */
export type ArtifactRef = { path: string; filename: string; size?: number; mime_type?: string; artifact_id?: string };
export type DraftPhase = 'editing' | 'creating_session' | 'uploading' | 'starting_run' | 'failed';
export type DraftAttachment = {
  localId: string;
  filename: string;
  file?: File;
  fileToken?: string;
  status: 'staged' | 'uploading' | 'uploaded' | 'failed' | 'needs_reselect';
  progress?: number;
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
