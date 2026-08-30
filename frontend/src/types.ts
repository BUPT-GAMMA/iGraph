export type Session = {
  session_id: string;
  project_id?: string;
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
export type Plan = { steps?: Array<{ step_id?: number; goal?: string; status?: string; result_summary?: string }>; completed?: boolean };
export type GraphSnapshot = {
  nodes: Array<Record<string, unknown>>;
  edges?: Array<Record<string, unknown>>;
  links?: Array<Record<string, unknown>>;
  directed?: boolean;
  num_nodes?: number;
  num_edges?: number;
};

export type Message = {
  role: 'user' | 'assistant' | 'system';
  content: string;
  tools?: ToolCall[];
  plan?: Plan | null;
  graphs?: GraphSnapshot[];
  reports?: Array<Record<string, unknown>>;
  streaming?: boolean;
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
};

export type Dataset = {
  dataset_id: string;
  project_id: string;
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
export type FeedbackRequest = {
  request_id?: string;
  call_id?: string;
  tool_name?: string;
  proposed_args?: Record<string, unknown>;
  description?: string;
};
export type StreamEvent = { type?: string; [key: string]: any };
