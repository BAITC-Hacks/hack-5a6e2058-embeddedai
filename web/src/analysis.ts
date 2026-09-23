import type { Investigation, NodeDetail, PathEvidence, Report, Resilience } from "./types";
import { $, api, download, escapeHtml, labels, metricLabels, money, number, percent } from "./ui";

export function timeline(node: NodeDetail): string {
  const t = node.temporal;
  if (!t.daily.length) return '<p class="muted">Операций в выборке нет.</p>';
  const first = Date.parse(t.daily[0].date),
    last = Date.parse(t.daily.at(-1)?.date ?? "");
  const span = Math.max(1, (last - first) / 86400000 + 1);
  const max = Math.max(...t.daily.flatMap((d) => [d.in_kzt, d.out_kzt]), 1);
  const width = 620,
    step = width / span,
    bar = Math.max(1, Math.min(9, step / 3));
  const bars = t.daily
    .map((day) => {
      const x = 30 + ((Date.parse(day.date) - first) / 86400000 + 0.5) * step;
      const a = (day.in_kzt / max) * 85,
        b = (day.out_kzt / max) * 85;
      return `<g><title>${day.date}: вход ${money(day.in_kzt)}, выход ${money(day.out_kzt)}, плательщиков ${day.senders}</title><rect x="${x - bar}" y="${105 - a}" width="${bar}" height="${a}" fill="#159b8c"/><rect x="${x}" y="${105 - b}" width="${bar}" height="${b}" fill="#8057d4"/></g>`;
    })
    .join("");
  return `<div class="timeline"><svg viewBox="0 0 680 133" role="img" aria-label="Входящие и исходящие суммы по дням"><title>Дневные потоки: зелёный — вход, фиолетовый — выход</title><path d="M25 105 H660" stroke="#d2dfe5"/>${bars}<text x="25" y="124">${t.daily[0].date}</text><text x="660" y="124" text-anchor="end">${t.daily.at(-1)?.date}</text></svg><div class="timeline-key"><span>● Вход</span><span>● Выход</span></div></div><dl class="node-metrics"><div><dt>Дней с операциями</dt><dd>${t.active_days}</dd></div><div><dt>Плательщиков за один день, максимум</dt><dd>${t.max_same_day_senders}</dd></div><div><dt>Доля входа в пиковый день</dt><dd>${percent(t.peak_in_share)}</dd></div><div><dt>Сопоставлено с выходом через 1–2 дня</dt><dd>${money(t.matched_1_2d_kzt)} (${percent(t.matched_1_2d_share)} входа)</dd></div><div><dt>Совпадение входа/выхода в один день</dt><dd>${money(t.same_day_overlap_kzt)}</dd></div></dl><p class="fine-print">Сопоставление по FIFO без повторного использования суммы. Внутридневной порядок неизвестен; это совместимость потоков, а не доказательство транзита тех же денег.</p>`;
}
function pathHtml(path: PathEvidence): string {
  return `<div class="path-chain">${path.gids.map((gid, i) => `<button class="text-button gid" data-evidence-gid="${gid}">${gid}</button>${i < path.edges.length ? `<span class="path-arrow">→ <small>${money(path.edges[i].sum_kzt)} · ${path.edges[i].n_tx} пер.</small></span>` : ""}`).join("")}</div>`;
}
function evidenceSection(title: string, paths: PathEvidence[], empty: string): string {
  return `<h3>${title}</h3>${paths.length ? paths.map(pathHtml).join("") : `<p class="muted">${empty}</p>`}`;
}
let investigationSequence = 0;
export async function openInvestigation(
  run: string,
  node: NodeDetail,
  select: (gid: string) => Promise<void>,
) {
  const sequence = ++investigationSequence;
  const dialog = $<HTMLDialogElement>("investigation-dialog");
  $("investigation-title").textContent = `Доказательная карточка · ${node.gid}`;
  $("investigation-content").innerHTML = '<p role="status">Восстанавливаем направленные пути…</p>';
  dialog.showModal();
  try {
    const e = await api<Investigation>(`/api/runs/${run}/nodes/${node.gid}/investigation`);
    if (sequence !== investigationSequence) return;
    $("investigation-content").innerHTML =
      `<p class="evidence">${escapeHtml(node.evidence)}</p><h3>Хронология наблюдаемых операций</h3>${timeline(node)}<details><summary>Точные суммы по дням</summary><div class="table-scroll"><table><thead><tr><th>Дата</th><th>Вход / плательщики</th><th>Выход / получатели</th></tr></thead><tbody>${node.temporal.daily.map((d) => `<tr><td>${d.date}</td><td>${money(d.in_kzt)} / ${d.senders}</td><td>${money(d.out_kzt)} / ${d.receivers}</td></tr>`).join("")}</tbody></table></div></details>${evidenceSection(`Пути от seed · показано ${e.seed_paths.length} из ${e.seed_path_count}`, e.seed_paths, node.is_seed ? "Узел сам является seed. Других seed-предков в пределах 4 переходов нет." : "Seed-предков в пределах 4 направленных переходов не найдено.")}${evidenceSection("Возвратные контуры · до 5 примеров, до 5 рёбер", e.cycles, "Коротких направленных циклов не найдено. Это не исключает более длинные циклы.")}${evidenceSection(`Повторяемые звенья A → B → C · ${e.repeated_route_count} сочетаний`, e.repeated_routes, "Нет двух последовательных звеньев с ≥2 операциями на каждом.")}<p class="fine-print">Показано до 10 сочетаний. Повторяемость каждого звена не доказывает повторение маршрута целиком. Двусторонних контрагентов: ${e.reciprocal_count}.</p><div class="warning-box">${escapeHtml(e.caveat)}</div>`;
    for (const button of $("investigation-content").querySelectorAll<HTMLElement>(
      "[data-evidence-gid]",
    ))
      button.onclick = () => {
        dialog.close();
        void select(button.dataset.evidenceGid ?? "");
      };
  } catch (error) {
    $("investigation-content").textContent =
      error instanceof Error ? error.message : "Не удалось загрузить факты";
  }
}
export function downloadDossier(node: NodeDetail, report: Report) {
  const text = `# Справка по узлу ${node.gid}\n\nПравила v${report.rules_version}; период ${report.period_from} — ${report.period_to}.\n\nРоль-гипотеза: ${labels[node.role]}.\n${node.evidence}\n\nПриоритет: ${percent(node.priority_score)}, место ${node.rank}. Диапазон места при изменении весов ±20%: ${node.rank_range.join("–")}; это не доверительный интервал.\n\nВход: ${money(node.in_kzt)}, выход: ${money(node.out_kzt)}. Плательщиков ${node.in_deg}, получателей ${node.out_deg}, seed-предков ${node.seed_reach}.\n\n## Вклад в приоритет\n\n${Object.entries(
    node.priority_parts,
  )
    .map(([key, v]) => `- ${metricLabels[key]}: ${(v * 100).toFixed(2)} п.п.`)
    .join(
      "\n",
    )}\n\n## Временные наблюдения\n\nСовместимо с выходом через 1–2 дня: ${money(node.temporal.matched_1_2d_kzt)} (${percent(node.temporal.matched_1_2d_share)} входа). Не доказательство транзита тех же денег.\n\n| Дата | Вход KZT | Выход KZT | Плательщики | Получатели |\n|---|---:|---:|---:|---:|\n${node.temporal.daily.map((d) => `| ${d.date} | ${d.in_kzt} | ${d.out_kzt} | ${d.senders} | ${d.receivers} |`).join("\n")}\n\n## Ограничения\n\n${[...report.warnings, ...node.warnings].map((x) => `- ${x}`).join("\n")}\n\n## Следующие проверки\n\n${node.next_checks.map((x) => `- ${x}`).join("\n")}\n\n## Связи\n\n${[...node.incoming, ...node.outgoing].map((e) => `- ${e.src} → ${e.dst}: ${money(e.sum_kzt)}, ${e.n_tx} переводов`).join("\n")}\n\n## Происхождение данных (SHA-256)\n\n${Object.entries(
    report.input_sha256,
  )
    .map(([k, v]) => `- ${k}: ${v}`)
    .join("\n")}\n`;
  download(`node-${node.gid}.md`, text);
}
export function renderAnalysis(
  report: Report,
  run: string,
  select: (gid: string) => Promise<void>,
) {
  const s = report.sensitivity;
  $("analysis-view").innerHTML =
    `<div class="analysis-block"><p class="eyebrow">ПРОВЕРКА ГИПОТЕЗ</p><h2>Насколько устойчив наш приоритет?</h2><p>Каждый вес по очереди изменяется на ±20%, затем веса нормируются. Совпадение TOP ${s.top_n} с исходным списком — от <strong>${percent(s.minimum_top_overlap)}</strong>.</p><div class="sensitivity-grid">${s.scenarios.map((x) => `<div><span>${metricLabels[x.metric]} ${x.multiplier < 1 ? "−" : "+"}20%</span><strong>${percent(x.top_overlap)}</strong></div>`).join("")}</div><p class="fine-print">${escapeHtml(s.caveat)} Диапазон позиции конкретного узла показан в его карточке.</p></div><div class="analysis-block"><h2>Что изменится без ключевых узлов?</h2><p>Сравните приоритет с числом связей и случайным выбором. Анализируются оставшиеся вершины, без искусственного эффекта от уменьшения их количества.</p><form id="resilience-form" class="simulation-form"><label for="remove-count">Число узлов</label><select id="remove-count">${[1, 3, 5, 10, 20].map((n) => `<option value="${n}" ${n === 5 ? "selected" : ""}>${n}</option>`).join("")}</select><button id="simulate" class="button primary">Смоделировать</button></form><div id="resilience-result" aria-live="polite"></div></div><div class="analysis-block"><h2>Границы исходных данных</h2><p>${report.n_connected_components} компонент с ≥2 узлами и ${report.n_isolates} изолятов. ${report.n_boundary} узлов обрезаны глубиной обхода.</p><ul>${report.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul><p class="fine-print">Финансовый объём — max(наблюдаемый вход, наблюдаемый выход). Он входит в приоритет как отдельный процентиль, но не является остатком на счёте.</p></div>`;
  $("resilience-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = $<HTMLButtonElement>("simulate");
    button.disabled = true;
    button.textContent = "Расчёт…";
    try {
      const r = await api<Resilience>(
        `/api/runs/${run}/resilience?count=${$<HTMLSelectElement>("remove-count").value}`,
      );
      if (!button.isConnected) return;
      const random = r.random;
      $("resilience-result").innerHTML =
        `<p>Удалено ${r.count} активных узлов. Крупнейшая компонента до удаления: ${r.before.largest_component}.</p><div class="table-scroll"><table><thead><tr><th>Стратегия</th><th>Разрыв связности*</th><th>Оборот затронутых рёбер</th><th>Крупнейшая компонента</th></tr></thead><tbody>${[
          ["Наш приоритет", r.priority],
          ["Число связей", r.degree],
        ]
          .map(([name, impact]) => {
            const m = impact as Resilience["priority"];
            return `<tr><td>${name}</td><td>${percent(m.fragmented_pairs_share)}</td><td>${percent(m.removed_turnover_share)}</td><td>${m.largest_component}</td></tr>`;
          })
          .join(
            "",
          )}<tr><td>Случайный, ${r.random_trials} прогонов</td><td>${percent(random.fragmented_pairs_share.mean)} (${percent(random.fragmented_pairs_share.min)}–${percent(random.fragmented_pairs_share.max)})</td><td>${percent(random.removed_turnover_share.mean)}</td><td>${number(random.largest_component.mean)}</td></tr></tbody></table></div><p class="fine-print">* Доля пар оставшихся вершин, которые были связаны до удаления и стали несвязными. Диапазон случайного выбора — min–max, не доверительный интервал.</p><details><summary>Удалённые узлы нашего приоритета</summary>${r.priority.removed_gids.map((gid) => `<button class="text-button gid" data-simulation-gid="${gid}">${gid}</button>`).join(" · ")}</details><div class="warning-box">${escapeHtml(r.caveat)}</div>`;
      for (const b of $("resilience-result").querySelectorAll<HTMLElement>("[data-simulation-gid]"))
        b.onclick = () => {
          void select(b.dataset.simulationGid ?? "");
        };
    } catch (error) {
      if (button.isConnected)
        $("resilience-result").textContent =
          error instanceof Error ? error.message : "Ошибка расчёта";
    } finally {
      button.disabled = false;
      button.textContent = "Смоделировать";
    }
  };
}
