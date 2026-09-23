import type { NodeDetail, QueueResponse } from "./types";
import { $, api, download, escapeHtml, labels, money, percent } from "./ui";

const selectionLimit = 20;
const pageSize = 25;
/** Prefix spreadsheet formulas and preserve int64 identifiers as literal text. */
function csvCell(value: unknown, identifier = false): string {
  let text = String(value ?? "");
  if (identifier || /^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}
export class InvestigationQueue {
  private run = "";
  private offset = 0;
  private sequence = 0;
  private selected = new Set<string>();
  private notes: Record<string, string> = {};
  private matched = 0;
  private controller: AbortController | undefined;
  constructor(
    private filters: () => URLSearchParams,
    private select: (gid: string) => Promise<void>,
    private changed: () => void,
  ) {
    $("queue-view").innerHTML =
      `<div class="workspace-intro"><p class="eyebrow">РАБОЧИЙ СПИСОК АНАЛИТИКА</p><h2>Очередь проверки</h2><p>Все узлы по фильтрам слева, включая те, которых нет в TOP 50. Приоритет и место рассчитаны по всему набору.</p></div>
      <div class="queue-controls"><label>Поиск по части gid<input id="queue-search" maxlength="30" placeholder="Часть идентификатора" autocomplete="off"></label><label>Сортировка<select id="queue-sort"><option value="rank">Место в общем рейтинге</option><option value="volume">Денежный объём</option><option value="in_kzt">Входящая сумма</option><option value="out_kzt">Исходящая сумма</option><option value="in_deg">Число плательщиков</option><option value="out_deg">Число получателей</option><option value="role_score">Поддержка роли</option></select></label><label>Порядок<select id="queue-order"><option value="asc">По возрастанию</option><option value="desc">По убыванию</option></select></label></div>
      <div class="queue-selection"><span id="queue-selected-count"></span><button id="queue-export" class="text-button" disabled>CSV выбранных ↓</button><button id="queue-clear" class="text-button" disabled>Очистить выбор</button><button id="queue-ask" class="text-button" disabled>Спросить AI →</button></div><div id="queue-selected-list" class="queue-selected-list"></div><p class="fine-print">До 20 узлов в одном вопросе. Выбор и заметки сохраняются только в этом браузере для текущего расчёта; заметки не являются доказательствами и не отправляются AI. В CSV перед gid добавляется апостроф для сохранения точности в таблицах.</p><p id="queue-storage" class="fine-print" hidden>Хранилище браузера недоступно: выбор и заметки сохранятся только до перезагрузки.</p><div id="queue-error" class="alert" role="alert" hidden></div><div id="queue-table" class="table-scroll" aria-live="polite"></div><div class="queue-pagination"><button id="queue-prev" class="button secondary" disabled>← Назад</button><span id="queue-count" role="status"></span><button id="queue-next" class="button secondary" disabled>Далее →</button></div>`;
    let timer: ReturnType<typeof setTimeout>;
    $("queue-search").oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => this.refresh(), 180);
    };
    $("queue-sort").onchange = () => {
      $<HTMLSelectElement>("queue-order").value =
        $<HTMLSelectElement>("queue-sort").value === "rank" ? "asc" : "desc";
      this.refresh();
    };
    $("queue-order").onchange = () => this.refresh();
    $("queue-prev").onclick = () => {
      this.offset = Math.max(0, this.offset - pageSize);
      void this.load();
    };
    $("queue-next").onclick = () => {
      this.offset += pageSize;
      void this.load();
    };
    $("queue-clear").onclick = () => {
      this.selected.clear();
      this.persist();
      this.syncSelection();
    };
    $("queue-export").onclick = () => void this.export();
    this.syncSelection();
  }
  reset() {
    this.controller?.abort();
    ++this.sequence;
    this.run = "";
    this.selected.clear();
    this.notes = {};
    $("queue-table").innerHTML = "";
    this.syncSelection();
  }
  setRun(run: string) {
    this.reset();
    this.run = run;
    $("queue-storage").hidden = true;
    try {
      const state = JSON.parse(localStorage.getItem(`money-graph-queue-${run}`) ?? "null");
      if (state && typeof state === "object") {
        if (Array.isArray(state.selected))
          this.selected = new Set(
            state.selected
              .filter((gid: unknown) => typeof gid === "string" && /^-?\d+$/.test(gid))
              .slice(0, selectionLimit),
          );
        if (state.notes && typeof state.notes === "object") {
          for (const [gid, note] of Object.entries(state.notes))
            if (/^-?\d+$/.test(gid) && typeof note === "string")
              this.notes[gid] = note.slice(0, 1000);
        }
      }
    } catch {
      $("queue-storage").hidden = false;
    }
    $<HTMLInputElement>("queue-search").value = "";
    $<HTMLSelectElement>("queue-sort").value = "rank";
    $<HTMLSelectElement>("queue-order").value = "asc";
    this.syncSelection();
    this.refresh();
  }
  selectedGids(): string[] {
    return [...this.selected];
  }
  refresh() {
    this.offset = 0;
    void this.load();
  }
  private persist() {
    try {
      localStorage.setItem(
        `money-graph-queue-${this.run}`,
        JSON.stringify({ selected: [...this.selected], notes: this.notes }),
      );
    } catch {
      $("queue-storage").hidden = false;
    }
  }
  private syncSelection() {
    $("queue-selected-count").textContent = `Выбрано: ${this.selected.size} / ${selectionLimit}`;
    for (const id of ["queue-export", "queue-clear", "queue-ask"])
      $<HTMLButtonElement>(id).disabled = this.selected.size === 0;
    for (const checkbox of $("queue-table").querySelectorAll<HTMLInputElement>(
      "[data-queue-check]",
    )) {
      checkbox.checked = this.selected.has(checkbox.dataset.queueCheck ?? "");
      checkbox.disabled = this.selected.size >= selectionLimit && !checkbox.checked;
    }
    $("queue-selected-list").innerHTML = [...this.selected]
      .map(
        (gid) =>
          `<button class="selection-chip gid" data-remove-selection="${escapeHtml(gid)}" aria-label="Убрать из выбора ${escapeHtml(gid)}">${escapeHtml(gid)} ×</button>`,
      )
      .join("");
    for (const button of $("queue-selected-list").querySelectorAll<HTMLElement>(
      "[data-remove-selection]",
    ))
      button.onclick = () => {
        this.selected.delete(button.dataset.removeSelection ?? "");
        this.persist();
        this.syncSelection();
      };
    this.changed();
  }
  private async load() {
    if (!this.run) return;
    this.controller?.abort();
    this.controller = new AbortController();
    const sequence = ++this.sequence;
    const params = this.filters();
    params.set("sort", $<HTMLSelectElement>("queue-sort").value);
    params.set("order", $<HTMLSelectElement>("queue-order").value);
    params.set("offset", String(this.offset));
    params.set("limit", String(pageSize));
    const search = $<HTMLInputElement>("queue-search").value.trim();
    if (search) params.set("search", search);
    $("queue-table").setAttribute("aria-busy", "true");
    $("queue-count").textContent = "Загрузка списка…";
    $("queue-error").hidden = true;
    $<HTMLButtonElement>("queue-prev").disabled = true;
    $<HTMLButtonElement>("queue-next").disabled = true;
    try {
      const result = await api<QueueResponse>(`/api/runs/${this.run}/nodes?${params}`, {
        signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(45000)]),
      });
      if (sequence !== this.sequence) return;
      this.matched = result.matched;
      $("queue-table").innerHTML = result.nodes.length
        ? `<table class="queue-results"><thead><tr><th>Выбор</th><th>Место · узел · роль</th><th>Потоки / связи</th><th>Основание</th><th>Личная заметка</th></tr></thead><tbody>${result.nodes.map((node) => `<tr><td><input type="checkbox" data-queue-check="${escapeHtml(node.gid)}" aria-label="Выбрать узел ${escapeHtml(node.gid)}"></td><td><span class="tiny">#${node.rank} · ${percent(node.priority_score)}</span><button class="text-button gid" data-queue-gid="${escapeHtml(node.gid)}">${escapeHtml(node.gid)}</button><span>${escapeHtml(labels[node.role] ?? node.role)}</span><small>Сообщество #${node.cluster_id} · уровень ${node.depth}${node.is_seed ? " · seed" : ""}</small></td><td><span>Вход ${money(node.in_kzt)}</span><span>Выход ${money(node.out_kzt)}</span><small>${node.in_deg} плательщиков / ${node.out_deg} получателей</small></td><td>${escapeHtml(node.evidence)}${node.n_anomaly_signals ? `<small>Сигналов профиля: ${node.n_anomaly_signals}</small>` : ""}</td><td><textarea data-queue-note="${escapeHtml(node.gid)}" aria-label="Заметка по узлу ${escapeHtml(node.gid)}" maxlength="1000" rows="2" placeholder="Что проверить">${escapeHtml(this.notes[node.gid] ?? "")}</textarea></td></tr>`).join("")}</tbody></table>`
        : '<p class="queue-empty">По этим фильтрам узлов нет. Измените роль, сообщество, уровень или строку поиска.</p>';
      for (const checkbox of $("queue-table").querySelectorAll<HTMLInputElement>(
        "[data-queue-check]",
      ))
        checkbox.onchange = () => {
          const gid = checkbox.dataset.queueCheck ?? "";
          if (checkbox.checked && this.selected.size < selectionLimit) this.selected.add(gid);
          else this.selected.delete(gid);
          this.persist();
          this.syncSelection();
        };
      for (const button of $("queue-table").querySelectorAll<HTMLElement>("[data-queue-gid]"))
        button.onclick = () => void this.select(button.dataset.queueGid ?? "");
      for (const input of $("queue-table").querySelectorAll<HTMLTextAreaElement>(
        "[data-queue-note]",
      ))
        input.oninput = () => {
          const gid = input.dataset.queueNote ?? "";
          if (input.value) this.notes[gid] = input.value;
          else delete this.notes[gid];
          this.persist();
        };
      $("queue-count").textContent = result.matched
        ? `${this.offset + 1}–${this.offset + result.nodes.length} из ${result.matched} · всего ${result.total}`
        : `0 подходящих · всего ${result.total}`;
      $<HTMLButtonElement>("queue-prev").disabled = this.offset === 0;
      $<HTMLButtonElement>("queue-next").disabled = this.offset + pageSize >= this.matched;
      this.syncSelection();
    } catch (error) {
      if (sequence !== this.sequence) return;
      $("queue-error").textContent =
        error instanceof Error ? error.message : "Не удалось открыть очередь";
      $("queue-error").hidden = false;
      $("queue-table").innerHTML =
        '<button id="queue-retry" class="button secondary">Повторить загрузку</button>';
      $("queue-retry").onclick = () => void this.load();
      $("queue-count").textContent = "Список не загружен";
    } finally {
      if (sequence === this.sequence) $("queue-table").setAttribute("aria-busy", "false");
    }
  }
  private async export() {
    const run = this.run;
    const gids = this.selectedGids();
    const button = $<HTMLButtonElement>("queue-export");
    button.disabled = true;
    button.textContent = "Подготовка CSV…";
    try {
      const nodes = await Promise.all(
        gids.map((gid) => api<NodeDetail>(`/api/runs/${run}/nodes/${encodeURIComponent(gid)}`)),
      );
      if (run !== this.run) return;
      const header = [
        "gid_text",
        "role",
        "priority_score",
        "rank",
        "cluster_id",
        "in_kzt",
        "out_kzt",
        "evidence",
        "analyst_note",
      ];
      const lines = nodes.map((node) =>
        [
          csvCell(node.gid, true),
          ...[
            node.role,
            node.priority_score,
            node.rank,
            node.cluster_id,
            node.in_kzt,
            node.out_kzt,
            node.evidence,
            this.notes[node.gid] ?? "",
          ].map((cell) => csvCell(cell)),
        ].join(","),
      );
      download(
        `investigation-${run}.csv`,
        `\uFEFF${header.join(",")}\r\n${lines.join("\r\n")}\r\n`,
        "text/csv;charset=utf-8",
      );
    } catch (error) {
      if (run === this.run) {
        $("queue-error").textContent =
          error instanceof Error ? error.message : "Не удалось выгрузить выбор";
        $("queue-error").hidden = false;
      }
    } finally {
      button.textContent = "CSV выбранных ↓";
      button.disabled = this.selected.size === 0;
    }
  }
}
