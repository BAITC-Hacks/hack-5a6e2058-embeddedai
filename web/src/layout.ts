import type { LayoutRequest, LayoutResult } from "./layout.worker";

let stop: (() => void) | undefined;
export function cancelLayout() {
  stop?.();
  stop = undefined;
}
/** Each graph owns its worker: superseded layouts cannot consume CPU or overwrite newer maps. */
export function graphPositions(data: LayoutRequest): Promise<LayoutResult> {
  cancelLayout();
  if (data.nodes.length > 500 || data.nodes.length < 2) return Promise.resolve({});
  return new Promise((resolve) => {
    let worker: Worker;
    try {
      worker = new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" });
    } catch {
      resolve({ error: "Фоновая раскладка недоступна; использована сетка." });
      return;
    }
    const finish = (result: LayoutResult) => {
      clearTimeout(timeout);
      worker.terminate();
      if (stop === cancel) stop = undefined;
      resolve(result);
    };
    const cancel = () => finish({});
    const timeout = setTimeout(
      () => finish({ error: "Раскладка заняла слишком долго; использована сетка." }),
      20000,
    );
    stop = cancel;
    worker.onmessage = ({ data: result }: MessageEvent<LayoutResult>) => finish(result);
    worker.onerror = () => finish({ error: "Фоновая раскладка недоступна; использована сетка." });
    worker.postMessage(data);
  });
}
