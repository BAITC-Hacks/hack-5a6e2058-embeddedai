import type { Robustness } from "./types";
import { $, api, escapeHtml, labels, number, percent } from "./ui";

let sequence = 0;
let controller: AbortController | undefined;
const fraction = (value: number | null) =>
  value === null ? "Недостаточно данных" : percent(value);
export function appendRobustness(run: string, select: (gid: string) => Promise<void>) {
  controller?.abort();
  ++sequence;
  $("analysis-view").insertAdjacentHTML(
    "beforeend",
    `<div class="analysis-block"><p class="eyebrow">ПАРАМЕТРЫ И ГРАНИЦЫ ВЫВОДОВ</p><h2>Устойчивость ролей и сообществ</h2><p>Сравните выводы при изменении порогов ролей, разрешения Louvain и случайного seed. Это чувствительность к настройкам, не accuracy и не вероятность виновности.</p><button id="robustness-load" class="button primary">Проверить устойчивость</button><div id="robustness-result" aria-live="polite"></div></div>`,
  );
  $("robustness-load").onclick = async () => {
    const current = ++sequence;
    controller?.abort();
    controller = new AbortController();
    const button = $<HTMLButtonElement>("robustness-load");
    button.disabled = true;
    button.textContent = "Пересчитываем сценарии…";
    $("robustness-result").innerHTML =
      '<p role="status">Сравниваем роли и состав сообществ. Основные CSV остаются без изменений.</p>';
    try {
      const result = await api<Robustness>(`/api/runs/${run}/robustness`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(90000)]),
      });
      if (current !== sequence || !button.isConnected) return;
      const roles = result.roles;
      const communities = result.communities;
      const changed = roles.nodes.filter(
        (node) => node.assessed && node.unchanged_fraction !== null && node.unchanged_fraction < 1,
      );
      const nodeLink = (gid: string) =>
        `<button class="text-button gid" data-robustness-gid="${escapeHtml(gid)}">${escapeHtml(gid)}</button>`;
      $("robustness-result").innerHTML =
        `<h3>Роли · ${roles.scenario_count} сценариев</h3><p>Минимальная доля неизменных ролей: <strong>${fraction(roles.minimum_unchanged_fraction)}</strong> среди ${roles.n_active_nodes} активных узлов. Без внешних контрагентов: ${roles.n_nodes - roles.n_active_nodes}; они не подтверждают устойчивость.</p><div class="table-scroll"><table><thead><tr><th>Сценарий</th><th>Роль сохранена</th><th>Изменено узлов</th><th>Переходы</th></tr></thead><tbody>${roles.scenarios.map((scenario) => `<tr><td>${escapeHtml(scenario.label)}</td><td>${fraction(scenario.unchanged_fraction)}</td><td>${scenario.changed_nodes}</td><td>${scenario.transitions.map((transition) => `${escapeHtml(labels[transition.from_role] ?? transition.from_role)} → ${escapeHtml(labels[transition.to_role] ?? transition.to_role)}: ${transition.count}`).join("<br>") || "Нет изменений"}</td></tr>`).join("")}</tbody></table></div><details><summary>Пограничные роли · ${changed.length} узлов</summary><p class="fine-print">Показано до 100 пограничных узлов. Доля сценариев не является доверительным интервалом.</p>${
          changed
            .slice(0, 100)
            .map(
              (node) =>
                `<div class="robustness-node">${nodeLink(node.gid)} · ${escapeHtml(labels[node.baseline_role] ?? node.baseline_role)} сохранена в ${fraction(node.unchanged_fraction)} сценариев.<small>Альтернативы: ${node.alternatives
                  .filter((alternative) => alternative.role !== node.baseline_role)
                  .map(
                    (alternative) =>
                      `${escapeHtml(labels[alternative.role] ?? alternative.role)} (${alternative.scenario_count})`,
                  )
                  .join(", ")}</small></div>`,
            )
            .join("") || '<p class="muted">В проверенных сценариях пограничных ролей нет.</p>'
        }</details><h3>Сообщества · ${communities.scenario_count} сценариев</h3><p>Сравнение составов, без зависимости от номера сообщества. Исключено изолятов: ${communities.excluded_isolates}. Минимальный скорректированный индекс Рэнда: <strong>${communities.minimum_adjusted_rand === null ? "Недостаточно данных" : number(communities.minimum_adjusted_rand)}</strong>.</p><p class="fine-print">Индекс Рэнда с поправкой на случайное совпадение: 1 — одинаковое разбиение, около 0 — сходство на уровне случайного; возможны отрицательные значения.</p>${communities.scenarios.length ? `<div class="table-scroll"><table><thead><tr><th>Разрешение / seed</th><th>Сообществ</th><th>Индекс Рэнда</th></tr></thead><tbody>${communities.scenarios.map((scenario) => `<tr><td>${number(scenario.resolution)} / ${scenario.seed}</td><td>${scenario.n_communities}</td><td>${number(scenario.adjusted_rand)}</td></tr>`).join("")}</tbody></table></div>` : ""}<details><summary>Состав окружения TOP 20 при разных настройках</summary><p class="fine-print">Минимальное сходство Жаккара сравнивает остальных участников сообщества; сам узел исключён.</p><div class="table-scroll"><table><thead><tr><th>Узел</th><th>Исходное сообщество</th><th>Минимальное сходство</th></tr></thead><tbody>${communities.top20.map((node) => `<tr><td>${nodeLink(node.gid)}</td><td>#${node.baseline_cluster_id}</td><td>${fraction(node.minimum_membership_jaccard)}</td></tr>`).join("")}</tbody></table></div></details><div class="warning-box">${escapeHtml(result.caveat)}${communities.warnings.length ? `<ul>${communities.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : ""}</div>`;
      for (const link of $("robustness-result").querySelectorAll<HTMLElement>(
        "[data-robustness-gid]",
      ))
        link.onclick = () => void select(link.dataset.robustnessGid ?? "");
    } catch (error) {
      if (current === sequence && button.isConnected)
        $("robustness-result").innerHTML =
          `<div class="alert" role="alert">${escapeHtml(error instanceof Error ? error.message : "Не удалось сравнить сценарии")}</div>`;
    } finally {
      if (current === sequence && button.isConnected) {
        button.disabled = false;
        button.textContent = "Проверить устойчивость";
      }
    }
  };
}
