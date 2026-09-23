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
