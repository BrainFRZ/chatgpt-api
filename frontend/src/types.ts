// Convert LaTeX-style math delimiters to dollar-sign style for remark-math
export const convertMathDelimiters = (text: string): string => {
  return text
    .replace(/\\\[/g, '$$')      // \[ to $$
    .replace(/\\\]/g, '$$')      // \] to $$
    .replace(/\\\(/g, '$')       // \( to $
    .replace(/\\\)/g, '$');      // \) to $
};

// Format timestamp to "Month Day, Year  HH:MM" in Eastern time
export const formatTimestamp = (timestamp: string | undefined): string => {
  if (!timestamp) return '';
  try {
    const date = new Date(timestamp);
    const options: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
    };
    const timeOptions: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    };
    const datePart = date.toLocaleDateString('en-US', options);
    const timePart = date.toLocaleTimeString('en-US', timeOptions);
    return `${datePart}  ${timePart}`;
  } catch {
    return '';
  }
};

export interface LoginResponse {
  username: string;
  has_api_key: boolean;
  is_new_user: boolean;
}

export interface ChatMessage {
  id?: string;  // Unique message ID (for branching)
  parent_id?: string | null;  // ID of parent message (for branching)
  role: string;
  content: string;
  timestamp?: string;
  tokens?: string;
  cost?: string;
  reasoning?: string;
  attached_files?: {filename: string, content: string}[];
  model?: string;  // Model used for this response (assistant messages only)
  service_tier?: 'flex' | 'standard' | null;  // GPT service tier (flex or standard)
  bookmark?: string;  // User-defined bookmark annotation
  hack_mode?: boolean;  // True for messages during a hack mode encounter
  sex_mode?: boolean;  // True for messages during an intimate scene
  ship_combat_mode?: boolean;  // True for messages during ship combat
  combat_mode?: boolean;  // True for messages during meatspace combat
  net_combat_mode?: boolean;  // True for messages during NET+meatspace combat
  chase_mode?: boolean;  // True for messages during a Hot Pursuit chase
  artifact_ops?: ArtifactOp[];  // Document operations for inline cards (Novels system)
  staged?: boolean;  // False = excluded from API context (Novels manual staging)
}

export interface Artifact {
  doc_id: string;
  title: string;
  content: string;
  type: 'prose' | 'outline' | 'notes' | 'character' | 'json' | 'pdf';
  format?: 'md' | 'txt' | 'json' | 'pdf';  // Explicit render format; inferred from type if absent
  version: number;
  pinned?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface ArtifactOp {
  action: 'created' | 'replaced' | 'edited' | 'read' | 'error';
  doc_id: string;
  title?: string;
  version?: number;
  edit_count?: number;
  error?: string;
  tool_use_id?: string;
}

export interface SystemMapNode {
  type: 'gateway' | 'data_node' | 'control_node' | 'password_gate' | 'target';
  ice: 'patrol' | 'tar' | 'black' | 'trace' | null;
  dv: number;
  connections: string[];
  contents: string;
}

export interface SystemMap {
  sr: number;
  nodes: Record<string, SystemMapNode>;
}

export interface HackState {
  active: boolean;
  tier: 'quick_hack' | 'full_sequence' | 'full_run';
  target_system: string;
  sr: number;
  alert_level: number;
  // dnd5e_cyber fields
  processes_remaining?: number;
  processes_max?: number;
  program_slots_used?: string[];
  hp_change?: number;
  // CPRED fields
  cycles_remaining?: number;
  cycles_max?: number;
  interface_rank?: number;
  net_actions_per_turn?: number;
  active_programs?: Array<{
    name: string;
    category: 'booster' | 'defender' | 'attacker' | 'black_ice';
    rez: number;
    status: 'active' | 'deactivated' | 'derezzed' | 'destroyed';
  }>;
  brain_damage?: number;
  // Shared fields
  current_node: string;
  nodes_visited: string[];
  revealed_nodes?: string[];
  ice_status: Record<string, any>;
  trace_progress: number | null;
  tar_stacks: number;
  narrative_summary: string | null;
  available_actions: string[];
  system_map: SystemMap | null;
  start_message_id: string | null;
}

export interface ChatStats {
  total_input_tokens: number;
  total_cached_tokens: number;
  total_output_tokens: number;
  total_cost: number;
  total_prompts: number;
  gpt_prompts?: number;
  sonnet_prompts?: number;
  avg_gpt_context_growth?: number;
  avg_sonnet_context_growth?: number;
  first_prompt_date?: string;
  last_accessed?: string;
}

export interface UserStats {
  lifetime_prompts: number;
  lifetime_gpt_prompts: number;
  lifetime_sonnet_prompts: number;
  lifetime_input_tokens: number;
  lifetime_cached_tokens: number;
  lifetime_output_tokens: number;
  lifetime_reasoning_tokens: number;
  lifetime_cost: number;
  lifetime_cache_miss_percent: number;
  monthly_active_days: number;
  monthly_prompts: number;
  monthly_gpt_prompts: number;
  monthly_sonnet_prompts: number;
  monthly_input_tokens: number;
  monthly_cached_tokens: number;
  monthly_output_tokens: number;
  monthly_reasoning_tokens: number;
  monthly_total_tokens: number;
  monthly_cost: number;
  today_prompts: number;
  today_gpt_prompts: number;
  today_sonnet_prompts: number;
  today_input_tokens: number;
  today_cached_tokens: number;
  today_output_tokens: number;
  today_reasoning_tokens: number;
  today_total_tokens: number;
  today_cost: number;
  avg_prompts_per_day: number;
  avg_gpt_prompts_per_day: number;
  avg_sonnet_prompts_per_day: number;
  avg_input_per_day: number;
  avg_cached_per_day: number;
  avg_output_per_day: number;
  avg_reasoning_per_day: number;
  avg_total_per_day: number;
  avg_cost_per_day: number;
  avg_gpt_context_growth: number;
  avg_sonnet_context_growth: number;
  days_since_first: number;
}

export interface ModelInfo {
  id: string;
  name: string;
  pricing: {
    input_new: number;
    input_cached: number;
    output: number;
    reasoning: number;
  };
  context_limits: {
    threshold: number;
    target: number;
  };
}

export interface ApiKeysStatus {
  has_openai: boolean;
  has_anthropic: boolean;
  has_perplexity?: boolean;
}

export interface FreeTokens {
  total_free: number;
  used: number;
  remaining: number;
  resets_at_eastern: string;
}

export interface ProjectFileInfo {
  filename: string;
  tokens: number;
  size_bytes: number;
  staged: boolean;
  agents: string[];
}

export interface ProjectFilesResponse {
  files: ProjectFileInfo[];
  total_tokens: number;
  staged_tokens: number;
}

export interface ProjectInstructions {
  instructions: string;
  tokens: number;
}

export interface ChatCardInfo {
  name: string;
  lastMessage: string;
  lastActive: string;
  messageCount: number;
  cost: number;
}
