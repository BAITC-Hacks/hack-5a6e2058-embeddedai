import type { TemporalPatterns } from "./types";
import { escapeHtml, money, number } from "./ui";

interface PatternSection {
  title: string;
  observations: string[];
  columns: string[];
  rows: string[][];
  empty: string;
}
const directions = { in: "Вход", out: "Выход" };
const unavailable =
  "В этом сохранённом расчёте временные паттерны отсутствуют. Пересчитайте исходные данные, чтобы увидеть формальные проверки.";

function sections(patterns: TemporalPatterns): PatternSection[] {
  const { activity, synchronous, repeated_amounts: repeated } = patterns;
  const activityStatus =
    activity.status === "assessed"
      ? `Активных дней: ${activity.active_days}; минимум: ${activity.minimum_days}. Медиана: ${number(activity.baseline_median_tx ?? 0)} операций; строгий порог: больше ${number(activity.threshold_tx ?? 0)}. Дней выше порога: ${activity.spike_day_count}; показано: ${Math.min(activity.spike_days.length, 5)}.`
      : `Оценка не выполнена: активных дней ${activity.active_days}, требуется не менее ${activity.minimum_days}. Недостаточно истории для сравнения активности.`;
  return [
    {
      title: "Всплески активности",
      observations: [activity.rule, activityStatus],
      columns: ["Дата", "Операции · вход / выход", "Вход / выход"],
      rows:
        activity.status === "assessed"
          ? activity.spike_days
              .slice(0, 5)
              .map((day) => [
                day.date,
                `${day.n_tx} · ${day.in_tx} / ${day.out_tx}`,
                `${money(day.in_kzt)} / ${money(day.out_kzt)}`,
              ])
          : [],
      empty: activity.status === "assessed" ? "Дней выше формального порога не найдено." : "",
    },
    {
      title: "Поступления от нескольких плательщиков за день",
      observations: [
        `Не менее ${synchronous.minimum_senders} различных плательщиков за одну дату. Подходящих дней: ${synchronous.day_count}; показано: ${Math.min(synchronous.days.length, 5)}. Общая дата не устанавливает точное время переводов.`,
      ],
      columns: ["Дата", "Плательщики / переводы", "Вход"],
      rows: synchronous.days
        .slice(0, 5)
        .map((day) => [day.date, `${day.senders} / ${day.in_tx}`, money(day.in_kzt)]),
      empty: `Дней с ${synchronous.minimum_senders} и более плательщиками не найдено.`,
    },
    {
      title: "Повторные одинаковые малые суммы",
      observations: [
        repeated.rule,
        ...(["in", "out"] as const).map((direction) => {
          const cohort = repeated.directions[direction];
          return cohort.status === "assessed"
            ? `${directions[direction]}: переводов ${cohort.n_transactions}, минимум ${repeated.minimum_transactions}; Q1 = ${money(cohort.q1_kzt ?? 0)}.`
            : `${directions[direction]}: оценка не выполнена — переводов ${cohort.n_transactions}, требуется не менее ${repeated.minimum_transactions}.`;
        }),
        `Групп по правилу: ${repeated.group_count}; показано: ${Math.min(repeated.groups.length, 5)}.`,
      ],
      columns: ["Дата · направление", "Сумма × повторы", "Всего / контрагенты"],
      rows: repeated.groups
        .slice(0, 5)
        .map((group) => [
          `${group.date} · ${directions[group.direction]}`,
          `${money(group.amount_kzt)} × ${group.n_tx}`,
          `${money(group.total_kzt)} / ${group.counterparties}`,
        ]),
      empty:
        repeated.directions.in.status === "assessed" ||
        repeated.directions.out.status === "assessed"
          ? "В направлениях с достаточной историей групп по этому правилу не найдено."
          : "Недостаточно переводов в обоих направлениях; отсутствие оценки не означает отсутствие повторов.",
    },
  ];
}

export function temporalPatternsHtml(patterns?: TemporalPatterns): string {
  if (patterns?.version !== "1")
    return `<p class="fine-print temporal-patterns-unavailable">${unavailable}</p>`;
  return `<details class="temporal-patterns"><summary>Временные паттерны · проверяемые признаки</summary><p class="fine-print">Отдельные наблюдения; роль и приоритет остаются прежними.</p>${sections(
    patterns,
  )
    .map(
      (section) =>
        `<section><h3>${section.title}</h3>${section.observations.map((observation) => `<p class="fine-print">${escapeHtml(observation)}</p>`).join("")}${section.rows.length ? `<div class="table-scroll"><table><thead><tr>${section.columns.map((column) => `<th>${column}</th>`).join("")}</tr></thead><tbody>${section.rows.map((row) => `<tr>${row.map((value) => `<td>${escapeHtml(value)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : section.empty ? `<p class="muted">${escapeHtml(section.empty)}</p>` : ""}</section>`,
    )
    .join(
      "",
    )}<p class="warning-box temporal-patterns-caveat">${escapeHtml(patterns.caveat)}</p></details>`;
}

export function temporalPatternsMarkdown(patterns?: TemporalPatterns): string {
  if (patterns?.version !== "1") return `## Временные паттерны\n\n${unavailable}\n`;
  const safe = (value: string) => escapeHtml(value).replaceAll("|", "\\|");
  return `## Временные паттерны\n\n${sections(patterns)
    .map(
      (section) =>
        `### ${section.title}\n\n${section.observations.map(safe).join("\n\n")}\n\n${section.rows.length ? `| ${section.columns.join(" | ")} |\n| ${section.columns.map(() => "---").join(" | ")} |\n${section.rows.map((row) => `| ${row.map(safe).join(" | ")} |`).join("\n")}` : safe(section.empty)}`,
    )
    .join("\n\n")}\n\n${safe(patterns.caveat)}\n`;
}
