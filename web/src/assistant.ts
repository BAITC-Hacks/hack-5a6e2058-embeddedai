import type { AssistantResponse } from "./types";
import { $, api, escapeHtml, labels, money } from "./ui";

interface AssistantStatus {
  enabled: boolean;
  model: string;
  reason?: string;
  access_required?: boolean;
}
const operations: Record<string, string> = {
  node_summary: "Профиль узлов",
  rank_nodes: "Ранжирование узлов",
  common_recipients: "Общие получатели",
  common_senders: "Общие плательщики",
  path: "Кратчайший направленный путь",
  cycles: "Примеры направленных циклов",
  community: "Сообщества выбранных узлов",
  unsupported: "Нужно уточнение вопроса",
};
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function queryHtml(raw: unknown, refs: (gids: string[]) => string): string {
  const query = object(raw);
  const gids = Array.isArray(query.gids)
    ? query.gids.filter((gid): gid is string => typeof gid === "string")
    : [];
  const sortNames: Record<string, string> = {
    priority_score: "Приоритет",
    volume: "Денежный объём",
    in_kzt: "Входящая сумма",
    out_kzt: "Исходящая сумма",
  };
  return `<details class="assistant-query"><summary>Как понят вопрос</summary><dl class="node-metrics"><div><dt>Действие</dt><dd>${escapeHtml(operations[String(query.operation)] ?? "Не указано")}</dd></div><div><dt>Область</dt><dd>${gids.length ? `${gids.length} узл. · ${gids.map((gid) => refs([gid]) || escapeHtml(gid)).join(", ")}` : "Весь набор"}</dd></div><div><dt>Роль</dt><dd>${escapeHtml(labels[String(query.role)] ?? "Все роли")}</dd></div>${typeof query.limit === "number" ? `<div><dt>Максимум результатов</dt><dd>${query.limit}</dd></div>` : ""}${["common_recipients", "common_senders", "path", "cycles"].includes(String(query.operation)) ? `<div><dt>Максимум рёбер в пути</dt><dd>${escapeHtml(query.max_hops)}</dd></div>` : ""}${["common_recipients", "common_senders"].includes(String(query.operation)) ? `<div><dt>Минимальный охват</dt><dd>${query.min_sources === 0 ? `Все ${gids.length} выбранных узлов` : escapeHtml(query.min_sources)}</dd></div>` : ""}${query.operation === "rank_nodes" ? `<div><dt>Сортировка по убыванию</dt><dd>${escapeHtml(sortNames[String(query.sort_by)] ?? query.sort_by)}</dd></div>` : ""}</dl>${typeof query.clarification === "string" && query.clarification ? `<p>${escapeHtml(query.clarification)}</p>` : ""}<p class="fine-print">Если вопрос понят неверно, уточните направление переводов, узлы и число переходов.</p></details>`;
}
function pathsHtml(facts: unknown[], known: Set<string>, refs: (gids: string[]) => string): string {
  const groups: string[] = [];
  for (const raw of facts.slice(0, 20)) {
    const fact = object(raw);
    if (!Array.isArray(fact.paths)) continue;
    const paths: string[] = [];
    for (const rawPath of fact.paths.slice(0, 20)) {
      const path = object(rawPath);
      if (
        !Array.isArray(path.gids) ||
        !Array.isArray(path.edges) ||
        path.gids.length > 6 ||
        path.gids.length !== path.edges.length + 1
      )
        continue;
      const gids = path.gids;
      const edges = path.edges.map(object);
      if (
        !gids.every((gid) => typeof gid === "string" && known.has(gid)) ||
        !edges.every(
          (edge, index) =>
            edge.src === gids[index] &&
            edge.dst === gids[index + 1] &&
            typeof edge.sum_kzt === "number" &&
            Number.isFinite(edge.sum_kzt) &&
            typeof edge.n_tx === "number",
        )
      )
        continue;
      paths.push(
        `<div class="path-chain">${gids.map((gid, index) => `${refs([gid])}${index < edges.length ? `<span class="path-arrow">→ <small>${money(edges[index].sum_kzt as number)} · ${edges[index].n_tx} пер.</small></span>` : ""}`).join("")}</div>`,
      );
    }
    if (paths.length)
      groups.push(
        `${typeof fact.gid === "string" ? `<h3>Совпадение: ${refs([fact.gid])}</h3>` : ""}${paths.join("")}`,
      );
  }
  return groups.length
    ? `<details class="assistant-paths" open><summary>Пути и переводы</summary>${groups.join("")}<p class="fine-print">Суммы относятся к отдельным рёбрам за весь период. Они не суммируются в сквозной поток и не доказывают хронологию движения тех же денег.</p></details>`
    : "";
}
export class AnalystAssistant {
  private run = "";
  private sequence = 0;
  private enabled = false;
  private pending = false;
  private controller: AbortController | undefined;
  constructor(
    private context: () => string[],
    private select: (gid: string) => Promise<void>,
  ) {
    $("assistant-view").innerHTML =
      `<div class="workspace-intro"><p class="eyebrow">ВОПРОС → ПРОВЕРЯЕМЫЕ ФАКТЫ</p><h2>AI-ассистент аналитика</h2><p>Задайте вопрос о выбранных узлах. AI помогает понять запрос; суммы, направления переводов и ссылки вычисляются по графу на сервере.</p></div><p id="assistant-status" role="status">Проверяем доступность ассистента…</p><div class="subtle-box assistant-disclosure">Вопрос и выбранные gid отправляются OpenAI. Таблицы графа остаются на сервере. Не включайте в вопрос имена, реквизиты или другие сведения, которыми не хотите делиться.</div><form id="assistant-form"><label for="assistant-question">Вопрос о графе</label><textarea id="assistant-question" rows="3" maxlength="2000" required placeholder="Кто собирает деньги с этих пятерых?" disabled></textarea><p id="assistant-context" class="fine-print"></p><div class="assistant-examples"><button type="button" class="text-button" data-question="Кто получает переводы от выбранных узлов?">Общие получатели</button><button type="button" class="text-button" data-question="Кто отправляет деньги выбранным узлам?">Общие плательщики</button><button type="button" class="text-button" data-question="Покажи путь между выбранными узлами">Найти путь</button></div><div id="assistant-access" hidden><label for="assistant-token">Код доступа к AI на публичном сервере</label><input id="assistant-token" type="password" autocomplete="off" maxlength="256"><p class="fine-print">Код предоставляется владельцем демо. Он не сохраняется в браузере.</p></div><div class="assistant-actions"><button id="assistant-send" class="button primary" disabled>Спросить по графу →</button><button id="assistant-cancel" class="button secondary" type="button" hidden>Отменить</button></div></form><div id="assistant-result" aria-live="polite"></div><p class="fine-print">Роль — гипотеза, а не вывод о виновности. При неполных данных ассистент должен обозначить ограничения. Сверяйте выводы с карточками узлов.</p>`;
    $("assistant-form").onsubmit = (event) => {
      event.preventDefault();
      void this.ask();
    };
    $("assistant-cancel").onclick = () => {
      this.cancel();
      $("assistant-result").innerHTML =
        '<p role="status">Запрос отменён. Можно изменить вопрос и повторить.</p>';
    };
    for (const button of $("assistant-view").querySelectorAll<HTMLElement>("[data-question]"))
      button.onclick = () => {
        $<HTMLTextAreaElement>("assistant-question").value = button.dataset.question ?? "";
        $("assistant-question").focus();
      };
    this.updateContext();
    void this.loadStatus();
  }
  reset() {
    this.cancel();
    this.run = "";
    $("assistant-result").innerHTML = "";
    $<HTMLTextAreaElement>("assistant-question").value = "";
    this.updateContext();
  }
  setRun(run: string) {
    this.reset();
    this.run = run;
    this.updateControls();
  }
  updateContext() {
    const gids = this.context();
    $("assistant-context").textContent = gids.length
      ? `Контекст вопроса: ${gids.length} узл. · ${gids.join(", ")}. Выберите до 20 узлов во вкладке «Очередь»; без выбора используется открытая карточка.`
      : "Узлы не выбраны. Можно спросить о всём наборе либо выбрать узлы во вкладке «Очередь».";
  }
  private updateControls() {
    $<HTMLButtonElement>("assistant-send").disabled = !this.enabled || !this.run || this.pending;
    $<HTMLTextAreaElement>("assistant-question").disabled = !this.enabled || this.pending;
    $("assistant-cancel").hidden = !this.pending;
    $("assistant-send").textContent = this.pending ? "Ищем факты…" : "Спросить по графу →";
  }
  private cancel() {
    ++this.sequence;
    this.controller?.abort();
    this.pending = false;
    this.updateControls();
  }
  private async loadStatus() {
    try {
      const status = await api<AssistantStatus>("/api/assistant/status");
      this.enabled = status.enabled;
      $("assistant-status").textContent = status.enabled
        ? `Ассистент доступен · ${status.model}`
        : `Ассистент недоступен. ${status.reason ?? "На сервере не настроен API-ключ. Основной анализ работает без AI."}`;
      $("assistant-access").hidden = !status.access_required;
    } catch (error) {
      $("assistant-status").textContent =
        error instanceof Error ? error.message : "Не удалось проверить AI";
      const retry = document.createElement("button");
      retry.className = "text-button";
      retry.textContent = "Повторить проверку";
      retry.onclick = () => void this.loadStatus();
      $("assistant-status").append(" ", retry);
    }
    this.updateControls();
  }
  private async ask() {
    const question = $<HTMLTextAreaElement>("assistant-question").value.trim();
    if (!question || !this.enabled || !this.run || this.pending) return;
    const selected = this.context();
    const sequence = ++this.sequence;
    this.controller = new AbortController();
    this.pending = true;
    this.updateControls();
    $("assistant-result").innerHTML =
      '<p role="status">Понимаем вопрос и проверяем связи в графе…</p>';
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = $<HTMLInputElement>("assistant-token").value.trim();
    if (token) headers["X-Assistant-Token"] = token;
    try {
      const result = await api<AssistantResponse>(`/api/runs/${this.run}/assistant`, {
        method: "POST",
        headers,
        body: JSON.stringify({ question, selected_gids: selected }),
        signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(45000)]),
      });
      if (sequence !== this.sequence) return;
      const known = new Set(result.nodes.map((node) => node.gid));
      const refs = (gids: string[]) =>
        gids
          .filter((gid) => known.has(gid))
          .map(
            (gid) =>
              `<button class="text-button gid" data-assistant-gid="${escapeHtml(gid)}">${escapeHtml(gid)} ↗</button>`,
          )
          .join(" · ");
      $("assistant-result").innerHTML =
        `<div class="assistant-answer"><h3>Ответ по наблюдаемому графу</h3><p class="assistant-question-echo">${escapeHtml(question)}</p><p class="assistant-answer-text">${escapeHtml(result.answer)}</p>${result.claims.length ? `<ul class="assistant-claims">${result.claims.map((claim) => `<li><p>${escapeHtml(claim.text)}</p><div>${refs(claim.gids)}</div></li>`).join("")}</ul>` : ""}${queryHtml(result.query, refs)}${pathsHtml(result.facts, known, refs)}${result.nodes.length ? `<details open><summary>Узлы и основания · ${result.nodes.length}</summary>${result.nodes.map((node) => `<div class="assistant-node">${refs([node.gid])}<span>${escapeHtml(labels[node.role] ?? node.role)}</span><p>${escapeHtml(node.evidence)}</p></div>`).join("")}</details>` : ""}${result.limitations.length ? `<div class="warning-box"><h3>Границы ответа</h3><ul>${result.limitations.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>` : ""}<p class="fine-print">${escapeHtml(result.model)} · цифры и ссылки проверены сервером по этому набору. Кнопки открывают исходные карточки узлов.</p></div>`;
      for (const button of $("assistant-result").querySelectorAll<HTMLElement>(
        "[data-assistant-gid]",
      ))
        button.onclick = () => void this.select(button.dataset.assistantGid ?? "");
    } catch (error) {
      if (sequence !== this.sequence) return;
      $("assistant-result").innerHTML =
        `<div class="alert" role="alert">${escapeHtml(error instanceof Error ? error.message : "Ассистент не смог ответить")}</div><p class="fine-print">Ответ не получен. Проверьте вопрос, соединение или код доступа и повторите запрос.</p>`;
    } finally {
      if (sequence === this.sequence) {
        this.pending = false;
        this.updateControls();
      }
    }
  }
}
