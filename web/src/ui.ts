export const labels: Record<string, string> = {
  coordinator: "Связующий узел",
  consolidator: "Консолидатор",
  transit: "Транзит",
  distributor: "Распределитель",
  terminal: "Конечный в выборке",
  peripheral: "Мало данных",
};
export const colors: Record<string, string> = {
  coordinator: "#8057d4",
  consolidator: "#e59432",
  transit: "#3888c7",
  distributor: "#159b8c",
  terminal: "#d7748e",
  peripheral: "#a2b0bc",
};
export const metricLabels: Record<string, string> = {
  pagerank: "Входящая значимость",
  betweenness: "Посредничество",
  seed_reach: "Охват seed",
  in_deg: "Плательщики",
  out_deg: "Получатели",
  volume: "Денежный объём",
};
export const number = (n: number) => n.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
export const money = (n: number) => `${number(n)} ₸`;
export const percent = (n: number) => `${Math.round(n * 100)}%`;
export const escapeHtml = (value: unknown) =>
  String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
  );
export const $ = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal: AbortSignal.timeout(45000), ...options });
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError")
      throw new Error("Сервер не ответил вовремя. Повторите действие или проверьте соединение.");
    throw new Error("Нет связи с сервером. Проверьте соединение и повторите действие.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      typeof body.detail === "string" ? body.detail : `Ошибка сервера (${response.status})`,
      response.status,
    );
  }
  return response.json();
}
export function download(name: string, text: string, type = "text/markdown;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const communityColor = (id: number) => `hsl(${Math.round((id * 137.508) % 360)}, 48%, 48%)`;
