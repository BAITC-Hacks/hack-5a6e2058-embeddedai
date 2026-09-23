import type { AssistantContext, AssistantResponse } from "./types";
import { $, ApiError, api, escapeHtml, labels, money } from "./ui";

interface AssistantStatus {
  enabled: boolean;
  model: string;
  reason?: string;
  access_required?: boolean;
}
const operations: Record<string, string> = {
  inspect_graph: "Обзор данных и методики",
  find_nodes: "Поиск и сравнение узлов",
  inspect_nodes: "Изучение выбранных узлов",
  trace_flows: "Проверка путей переводов",
  inspect_community: "Изучение сообщества",
  assess_removal: "Изменение связности при удалении узлов",
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
  if (Array.isArray(query.steps)) {
    return `<details class="assistant-query"><summary>Как проверен вопрос · ${query.steps.length} действий</summary><ol>${query.steps
      .map((rawStep) => {
        const step = object(rawStep);
        return `<li><strong>${escapeHtml(operations[String(step.tool)] ?? "Проверка графа")}</strong><p>${escapeHtml(step.summary ?? "")}</p></li>`;
      })
      .join(
        "",
      )}</ol><p class="fine-print">Вывод AI основан на этих проверках. Интерпретацию следует сверять с наблюдаемыми связями и ограничениями данных.</p></details>`;
  }
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

function factsHtml(facts: unknown[], refs: (gids: string[]) => string): string {
  const names: Record<string, string> = {
    gid: "Узел",
    role: "Роль",
    rank: "Место в рейтинге",
    priority_score: "Приоритет",
    role_score: "Поддержка роли",
    in_deg: "Плательщики",
    out_deg: "Получатели",
    in_kzt: "Вход от других клиентов",
    out_kzt: "Выход другим клиентам",
    volume: "Денежный объём",
    in_tx: "Входящих переводов",
    out_tx: "Исходящих переводов",
    depth: "Уровень",
    cluster_id: "Сообщество",
    matched_count: "Подходящих узлов",
    shown_count: "Показано узлов",
    n_nodes: "Узлы",
    n_active_nodes: "Активные узлы",
    n_edges: "Связи",
    n_transactions: "Переводы",
    n_seed: "Исходные seed",
    n_clusters: "Сообщества",
    n_isolates: "Без связей",
    n_boundary: "На границе наблюдения",
    n_components: "Компоненты связности",
    n_anomalous_profiles: "Необычные профили",
    turnover_kzt: "Наблюдаемый оборот",
    period_from: "Начало периода",
    period_to: "Конец периода",
    source_count: "Источников",
    direct_sum_kzt: "Сумма прямых переводов",
    matched_sources: "Связанных источников",
  };
  const groups = facts
    .slice(0, 40)
    .map((raw) => {
      const fact = object(raw);
      const rows = Object.entries(fact).filter(
        ([key, value]) =>
          names[key] &&
          (typeof value === "string" || typeof value === "number" || typeof value === "boolean"),
      );
      if (!rows.length) return "";
      return `<dl class="node-metrics assistant-fact">${rows
        .map(([key, value]) => {
          const content =
            key === "gid" && typeof value === "string"
              ? refs([value]) || escapeHtml(value)
              : key.endsWith("_kzt") || key === "volume"
                ? typeof value === "number"
                  ? money(value)
                  : escapeHtml(value)
                : key === "role"
                  ? escapeHtml(labels[String(value)] ?? value)
                  : escapeHtml(value);
          return `<div><dt>${names[key]}</dt><dd>${content}</dd></div>`;
        })
        .join("")}</dl>`;
    })
    .filter(Boolean);
  return groups.length
    ? `<details class="assistant-facts"><summary>Числа из расчёта · ${groups.length} записей</summary><p class="fine-print">Эти значения получены из графа сервером, до интерпретации AI.</p>${groups.join("")}</details>`
    : "";
}

interface ConversationTurn {
  question: string;
  result: AssistantResponse;
}
export class AnalystAssistant {
  private run = "";
  private sequence = 0;
  private enabled = false;
  private pending = false;
  private turns: ConversationTurn[] = [];
  private conversationId: string | null = null;
  private controller: AbortController | undefined;
  constructor(
    private context: () => AssistantContext,
    private select: (gid: string) => Promise<void>,
    private showView: (view: string) => void,
  ) {
    $("assistant-shell").innerHTML = `
      <div class="assistant-heading"><div><span class="assistant-spark" aria-hidden="true">✦</span><h2>Спросите о графе</h2><span class="tag small">AI-АНАЛИТИК</span></div><button id="assistant-toggle" class="text-button" type="button" aria-expanded="false" aria-controls="assistant-view">Открыть диалог</button></div>
      <form id="assistant-form"><label class="visually-hidden" for="assistant-question">Вопрос AI-аналитику</label><div class="assistant-composer"><textarea id="assistant-question" rows="1" maxlength="2000" required placeholder="Что здесь важно? Кто собирает деньги? Что проверить дальше?" disabled></textarea><button id="assistant-send" class="button primary" disabled>Спросить →</button><button id="assistant-cancel" class="text-button" type="button" hidden>Отменить</button></div></form>
      <div class="assistant-context-row"><p id="assistant-context" class="fine-print"></p><button id="assistant-new" class="text-button" type="button" hidden>Новый диалог</button></div>
      <div class="assistant-examples"><button type="button" class="text-button" data-question="С чего начать проверку этого графа?">С чего начать?</button><button type="button" class="text-button" data-question="Кто здесь собирает деньги и почему?">Кто собирает деньги?</button><button type="button" class="text-button" data-question="Какие ограничения данных важно учитывать?">Ограничения данных</button></div>
      <div class="assistant-settings-row"><p id="assistant-status" role="status">Проверяем доступность ассистента…</p><details id="assistant-access" hidden><summary>Код доступа</summary><label for="assistant-token">Код доступа к AI на публичном сервере</label><input id="assistant-token" type="password" autocomplete="off" maxlength="256"><p class="fine-print">Хранится только до перезагрузки страницы.</p></details><details class="assistant-privacy"><summary>Что получает AI</summary><p class="assistant-disclosure fine-print">Вопрос, контекст экрана, недавняя история диалога и краткие результаты проверок передаются OpenAI. Исходные файлы и локальные заметки не отправляются. Не включайте в вопрос личные сведения. На экране диалог доступен до обновления страницы или смены набора; продолжение на сервере истекает через 30 минут. AI помнит контекст в пределах текущего диалога.</p></details></div>
      <div id="assistant-view" tabindex="0" role="region" aria-label="Диалог с AI-аналитиком" hidden><div id="assistant-result" aria-live="polite"></div></div>`;
    $("assistant-form").onsubmit = (event) => {
      event.preventDefault();
      void this.ask();
    };
    $("assistant-question").onkeydown = (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        void this.ask();
      }
    };
    $("assistant-toggle").onclick = () => this.expand($("assistant-view").hidden);
    $("assistant-new").onclick = () => this.clearConversation();
    $("assistant-cancel").onclick = () => {
      this.cancel();
      this.render('<p role="status">Запрос отменён. Можно изменить вопрос и повторить.</p>');
    };
    for (const button of $("assistant-shell").querySelectorAll<HTMLElement>("[data-question]"))
      button.onclick = () => this.focus(button.dataset.question ?? "");
    this.updateContext();
    void this.loadStatus();
  }
  focus(question?: string) {
    if (question !== undefined) $<HTMLTextAreaElement>("assistant-question").value = question;
    this.expand(true);
    $("assistant-shell").scrollIntoView({ block: "start", behavior: "auto" });
    $("assistant-question").focus({ preventScroll: true });
    this.updateContext();
  }
  private expand(open: boolean) {
    $("assistant-view").hidden = !open;
    $("assistant-toggle").setAttribute("aria-expanded", String(open));
    $("assistant-toggle").textContent = open
      ? "Свернуть диалог"
      : this.turns.length
        ? `Диалог · ${this.turns.length}`
        : "Открыть диалог";
  }
  private clearConversation() {
    this.cancel();
    this.turns = [];
    this.conversationId = null;
    $("assistant-result").innerHTML = "";
    $("assistant-new").hidden = true;
    $<HTMLTextAreaElement>("assistant-question").value = "";
    $<HTMLTextAreaElement>("assistant-question").placeholder =
      "Что здесь важно? Кто собирает деньги? Что проверить дальше?";
    this.updateContext();
    this.expand(false);
  }
  reset() {
    this.clearConversation();
    this.run = "";
    this.updateControls();
  }
  setRun(run: string) {
    this.reset();
    this.run = run;
    this.updateControls();
  }
  updateContext() {
    const context = this.context();
    const parts: string[] = [];
    if (context.selected_gids.length) parts.push(`Выбрано ${context.selected_gids.length} узл.`);
    if (context.active_gid) parts.push(`Карточка ${context.active_gid}`);
    const filters = context.filters;
    if (filters.role) parts.push(labels[filters.role] ?? filters.role);
    if (filters.cluster !== null) parts.push(`Сообщество #${filters.cluster}`);
    if (filters.depth !== null) parts.push(`Уровень ${filters.depth}`);
    if (filters.seeds) parts.push("Только seed");
    if (context.queue) {
      const sorts: Record<string, string> = {
        rank: "место",
        volume: "объём",
        in_kzt: "вход",
        out_kzt: "выход",
        in_deg: "плательщики",
        out_deg: "получатели",
        role_score: "поддержка роли",
      };
      parts.push(
        `Очередь с позиции ${context.queue.offset + 1} · ${sorts[context.queue.sort] ?? context.queue.sort} ${context.queue.order === "desc" ? "↓" : "↑"}`,
      );
      if (context.queue.search) parts.push(`Поиск «${context.queue.search}»`);
    }
    $("assistant-context").textContent = parts.length
      ? `Вижу контекст: ${parts.join(" · ")}. Можно спросить и обо всём графе.`
      : "Весь граф · Задайте вопрос своими словами, выбирать узлы необязательно.";
  }
  private updateControls() {
    $<HTMLButtonElement>("assistant-send").disabled = !this.enabled || !this.run || this.pending;
    $<HTMLTextAreaElement>("assistant-question").disabled = !this.enabled || this.pending;
    $("assistant-cancel").hidden = !this.pending;
    $("assistant-send").textContent = this.pending ? "Проверяем…" : "Спросить →";
    $("assistant-shell").setAttribute("aria-busy", String(this.pending));
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
        ? "Понимает вопросы, учитывает контекст и проверяет связи по графу"
        : `AI недоступен. ${status.reason ?? "На сервере не настроен API-ключ. Основной анализ работает без AI."}`;
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
  private render(status = "") {
    $("assistant-result").innerHTML =
      this.turns
        .map(({ question, result }, index) => {
          const known = new Set(result.nodes.map((node) => node.gid));
          const linkedText = (text: string) =>
            text
              .split(/(\[gid:-?\d+\])/g)
              .map((part) => {
                const match = /^\[gid:(-?\d+)\]$/.exec(part);
                return match && known.has(match[1])
                  ? `<button class="text-button gid" data-assistant-gid="${escapeHtml(match[1])}">${escapeHtml(match[1])} ↗</button>`
                  : escapeHtml(part);
              })
              .join("");
          const refs = (gids: string[]) =>
            gids
              .filter((gid) => known.has(gid))
              .map(
                (gid) =>
                  `<button class="text-button gid" data-assistant-gid="${escapeHtml(gid)}">${escapeHtml(gid)} ↗</button>`,
              )
              .join(" · ");
          return `<article class="assistant-answer" data-assistant-turn="${index}"><p class="assistant-question-echo"><strong>Вы</strong> · ${escapeHtml(question)}</p><p class="assistant-answer-label">AI · Интерпретация по проверенным фактам</p><p class="assistant-answer-text">${linkedText(result.answer)}</p>${result.claims.length ? `<ul class="assistant-claims">${result.claims.map((claim) => `<li><p>${linkedText(claim.text)}</p><div>${refs(claim.gids)}</div></li>`).join("")}</ul>` : ""}${queryHtml(result.query, refs)}${factsHtml(result.facts, refs)}${pathsHtml(result.facts, known, refs)}${result.nodes.length ? `<details class="assistant-evidence"><summary>Узлы и основания · ${result.nodes.length}</summary>${result.nodes.map((node) => `<div class="assistant-node">${refs([node.gid])}<span>${escapeHtml(labels[node.role] ?? node.role)}</span><p>${escapeHtml(node.evidence)}</p></div>`).join("")}</details>` : ""}${result.limitations.length ? `<details class="assistant-limitations"><summary>Границы ответа</summary><ul>${result.limitations.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></details>` : ""}${result.actions?.length ? `<div class="assistant-navigation">${result.actions.map((action, actionIndex) => ((action.type === "focus_node" && action.gid && known.has(action.gid)) || (action.type === "show_view" && ["graph", "queue", "analysis", "clusters"].includes(action.view ?? "")) ? `<button class="button secondary" data-assistant-action="${index}:${actionIndex}">${escapeHtml(action.label)} ↗</button>` : "")).join("")}</div>` : ""}${
            result.followups?.length
              ? `<div class="assistant-followups"><span class="fine-print">Можно уточнить</span>${result.followups
                  .slice(0, 3)
                  .map(
                    (question) =>
                      `<button class="text-button" data-assistant-followup="${escapeHtml(question)}">${escapeHtml(question)}</button>`,
                  )
                  .join("")}</div>`
              : ""
          }<p class="fine-print">${escapeHtml(result.model)} · AI помогает с интерпретацией. Сверяйте выводы с фактами и карточками узлов.</p></article>`;
        })
        .join("") + status;
    for (const button of $("assistant-result").querySelectorAll<HTMLElement>(
      "[data-assistant-gid]",
    ))
      button.onclick = () => void this.select(button.dataset.assistantGid ?? "");
    for (const button of $("assistant-result").querySelectorAll<HTMLElement>(
      "[data-assistant-followup]",
    ))
      button.onclick = () => this.focus(button.dataset.assistantFollowup ?? "");
    for (const button of $("assistant-result").querySelectorAll<HTMLElement>(
      "[data-assistant-action]",
    ))
      button.onclick = () => {
        const [turn, index] = (button.dataset.assistantAction ?? "").split(":").map(Number);
        const action = this.turns[turn]?.result.actions?.[index];
        if (action?.type === "focus_node" && action.gid) void this.select(action.gid);
        else if (action?.type === "show_view" && action.view) this.showView(action.view);
      };
    $("assistant-new").hidden = !this.turns.length && !this.pending;
  }
  private showLatest() {
    const region = $("assistant-view");
    const last = $("assistant-result").lastElementChild;
    if (last instanceof HTMLElement) region.scrollTop = last.offsetTop - region.offsetTop;
  }
  private async ask() {
    const question = $<HTMLTextAreaElement>("assistant-question").value.trim();
    if (!question || !this.enabled || !this.run || this.pending) return;
    const context = this.context();
    const sequence = ++this.sequence;
    this.controller = new AbortController();
    this.pending = true;
    this.updateControls();
    this.expand(true);
    this.render(
      '<p class="assistant-progress" role="status">Понимаем вопрос, выбираем проверки и изучаем граф…</p>',
    );
    this.showLatest();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = $<HTMLInputElement>("assistant-token").value.trim();
    if (token) headers["X-Assistant-Token"] = token;
    try {
      const result = await api<AssistantResponse>(`/api/runs/${this.run}/assistant`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          question,
          selected_gids: context.selected_gids,
          context: {
            active_gid: context.active_gid,
            filters: context.filters,
            active_tab: context.active_tab,
            ...(context.queue ? { queue: context.queue } : {}),
          },
          conversation_id: this.conversationId,
        }),
        signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(90000)]),
      });
      if (sequence !== this.sequence) return;
      this.conversationId = result.conversation_id ?? null;
      this.turns.push({ question, result });
      this.turns = this.turns.slice(-8);
      $<HTMLTextAreaElement>("assistant-question").value = "";
      $<HTMLTextAreaElement>("assistant-question").placeholder =
        "Уточните ответ: почему? А что с первым узлом? Что проверить дальше?";
      this.render();
    } catch (error) {
      if (sequence !== this.sequence) return;
      if (error instanceof ApiError && error.status === 409) this.conversationId = null;
      this.render(
        `<div class="alert" role="alert">${escapeHtml(error instanceof Error ? error.message : "Ассистент не смог ответить")}</div><p class="fine-print">Ответ не получен. Проверьте соединение или код доступа и повторите запрос.</p>`,
      );
    } finally {
      if (sequence === this.sequence) {
        this.pending = false;
        this.updateControls();
        this.showLatest();
      }
    }
  }
}
