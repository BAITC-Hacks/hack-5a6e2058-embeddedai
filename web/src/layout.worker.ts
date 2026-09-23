import cytoscape from "cytoscape";

export interface LayoutRequest {
  nodes: { id: string; size: number }[];
  edges: { source: string; target: string }[];
}
export interface LayoutResult {
  positions?: Record<string, { x: number; y: number }>;
  error?: string;
}
const scope = self as unknown as {
  onmessage: (event: MessageEvent<LayoutRequest>) => void;
  postMessage: (data: LayoutResult) => void;
};
scope.onmessage = ({ data }) => {
  const graph = cytoscape({
    headless: true,
    styleEnabled: true,
    style: [{ selector: "node", style: { width: "data(size)", height: "data(size)" } }],
    elements: [
      ...data.nodes.map((node) => ({ data: node })),
      ...data.edges.map((edge, i) => ({ data: { id: `edge-${i}`, ...edge } })),
    ],
  });
  try {
    graph
      .layout({
        name: "cose",
        animate: false,
        randomize: false,
        boundingBox: { x1: 0, y1: 0, w: 1000, h: 700 },
        nodeRepulsion: () => 16000,
        idealEdgeLength: () => 75,
      } as cytoscape.LayoutOptions)
      .run();
    const positions: Record<string, { x: number; y: number }> = {};
    graph.nodes().forEach((node) => {
      const { x, y } = node.position();
      if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error("Invalid position");
      positions[node.id()] = { x, y };
    });
    scope.postMessage({ positions });
  } catch {
    scope.postMessage({ error: "Layout unavailable" });
  } finally {
    graph.destroy();
  }
};
