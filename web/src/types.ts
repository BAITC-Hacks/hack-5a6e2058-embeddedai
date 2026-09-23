export interface GraphNode {
  gid: string;
  role: string;
  role_score: number;
  priority_score: number;
  cluster_id: number;
  depth: number;
  is_seed: boolean;
  boundary_censored: boolean;
  isolated: boolean;
}
export interface Edge {
  src: string;
  dst: string;
  sum_kzt: number;
  n_tx: number;
}
export interface NodeDetail extends GraphNode {
  anomaly_profile: {
    cohort_depth: number;
    cohort_size: number;
    minimum_cohort_size: number;
    signals: {
      metric: string;
      value: number;
      threshold: number;
      q1: number;
      q3: number;
      text: string;
    }[];
    caveat: string;
  };
  self_transfer_kzt: number;
  self_transfer_tx: number;
  self_only: boolean;
  self_transfers: Edge[];
  rule_trace: {
    role: string;
    matched: boolean;
    support: number;
    raw_support: number;
    observation_multiplier: number;
  }[];
  temporal: Temporal;
  rank_range: [number, number];
  volume: number;
  in_deg: number;
  out_deg: number;
  in_kzt: number;
  out_kzt: number;
  in_tx: number;
  out_tx: number;
  seed_reach: number;
  pagerank: number;
  betweenness: number;
  pass_through: number | null;
  rank: number;
  evidence: string;
  warnings: string[];
  matched_rules: string[];
  priority_parts: Record<string, number>;
  next_checks: string[];
  incoming: Edge[];
  outgoing: Edge[];
}
export interface Report {
  betweenness_method: "exact" | "sampled";
  betweenness_pivots: number;
  sensitivity: Sensitivity;
  rules: { priority_weights: Record<string, number>; [key: string]: unknown };
  input_sha256: Record<string, string>;
  n_connected_components: number;
  n_nodes: number;
  n_edges: number;
  n_transactions: number;
  n_seed: number;
  n_clusters: number;
  n_isolates: number;
  n_boundary: number;
  n_components: number;
  turnover_kzt: number;
  period_from: string | null;
  period_to: string | null;
  runtime_seconds: number;
  rules_version: string;
  role_counts: Record<string, number>;
  warnings: string[];
  synthetic: boolean;
}
export interface Cluster {
  incoming_kzt: number;
  outgoing_kzt: number;
  boundary_nodes: number;
  role_counts: Record<string, number>;
  cluster_id: number;
  n_nodes: number;
  n_seed: number;
  sum_kzt_internal: number;
  top_gids: string[];
  hypothesis: string;
}
export interface TopNode {
  rank: number;
  gid: string;
  role: string;
  priority_score: number;
  why: string;
}
export interface GraphResponse {
  nodes: GraphNode[];
  edges: Edge[];
  total: number;
  matched: number;
  shown: number;
}

export interface Day {
  date: string;
  in_kzt: number;
  out_kzt: number;
  in_tx: number;
  out_tx: number;
  senders: number;
  receivers: number;
}
export interface Temporal {
  daily: Day[];
  active_days: number;
  max_same_day_senders: number;
  peak_in_share: number;
  matched_1_2d_kzt: number;
  matched_1_2d_share: number;
  same_day_overlap_kzt: number;
}
export interface PathEvidence {
  gids: string[];
  edges: Edge[];
}
export interface Investigation {
  seed_paths: PathEvidence[];
  seed_path_count: number;
  reciprocal: Edge[];
  reciprocal_count: number;
  cycles: PathEvidence[];
  repeated_routes: PathEvidence[];
  repeated_route_count: number;
  caveat: string;
}
export interface Sensitivity {
  top_n: number;
  minimum_top_overlap: number;
  caveat: string;
  scenarios: { metric: string; multiplier: number; top_overlap: number }[];
}
export interface Impact {
  removed_gids: string[];
  n_nodes: number;
  n_edges: number;
  components: number;
  largest_component: number;
  fragmented_pairs_share: number;
  removed_turnover_kzt: number;
  removed_turnover_share: number;
}
export interface Resilience {
  count: number;
  before: Impact;
  priority: Impact;
  degree: Impact;
  random: Record<string, { mean: number; min: number; max: number }>;
  random_trials: number;
  caveat: string;
}
export interface CommunityGraph {
  nodes: Cluster[];
  edges: { src: number; dst: number; sum_kzt: number; n_edges: number }[];
}

export interface NodeSummary {
  gid: string;
  role: string;
  role_score: number;
  priority_score: number;
  rank: number;
  cluster_id: number;
  depth: number;
  is_seed: boolean;
  in_deg: number;
  out_deg: number;
  in_kzt: number;
  out_kzt: number;
  volume: number;
  evidence: string;
  n_anomaly_signals: number;
}
export interface QueueResponse {
  nodes: NodeSummary[];
  total: number;
  matched: number;
  offset: number;
  limit: number;
}
export interface AssistantContext {
  selected_gids: string[];
  active_gid: string | null;
  filters: { role: string | null; cluster: number | null; depth: number | null; seeds: boolean };
  active_tab: string;
}
export interface AssistantAction {
  type: "focus_node" | "show_view";
  label: string;
  gid: string | null;
  view: "graph" | "queue" | "analysis" | "clusters" | null;
}
export interface AssistantResponse {
  answer: string;
  claims: { text: string; gids: string[] }[];
  nodes: { gid: string; role: string; evidence: string }[];
  facts: unknown[];
  limitations: string[];
  model: string;
  usage: unknown;
  query: unknown;
  conversation_id?: string;
  followups?: string[];
  actions?: AssistantAction[];
}
export interface RobustnessNode {
  gid: string;
  baseline_role: string;
  assessed: boolean;
  unchanged_fraction: number | null;
  alternatives: { role: string; scenario_count: number }[];
  changed_scenarios: string[];
}
export interface Robustness {
  version: string;
  caveat: string;
  roles: {
    n_nodes: number;
    n_active_nodes: number;
    scenario_count: number;
    minimum_unchanged_fraction: number | null;
    scenarios: {
      id: string;
      label: string;
      unchanged_fraction: number | null;
      changed_nodes: number;
      transitions: { from_role: string; to_role: string; count: number }[];
    }[];
    nodes: RobustnessNode[];
    top20: RobustnessNode[];
  };
  communities: {
    status: "complete" | "limited" | "not_applicable";
    n_active_nodes: number;
    excluded_isolates: number;
    scenario_count: number;
    minimum_adjusted_rand: number | null;
    scenarios: {
      id: string;
      seed: number;
      resolution: number;
      n_communities: number;
      adjusted_rand: number;
    }[];
    top20: {
      gid: string;
      assessed: boolean;
      baseline_cluster_id: number;
      mean_membership_jaccard: number | null;
      minimum_membership_jaccard: number | null;
    }[];
    warnings: string[];
  };
}
