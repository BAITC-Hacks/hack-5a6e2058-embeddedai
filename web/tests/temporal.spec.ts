import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import type { TemporalPatterns } from "../src/types";

function patterns(): TemporalPatterns {
  return {
    version: "1",
    activity: {
      status: "assessed", active_days: 8, minimum_days: 7, baseline_median_tx: 2,
      threshold_tx: 7, spike_day_count: 6,
      spike_days: Array.from({length: 5}, (_, index) => ({
        date: `2026-07-${10 + index}`, in_tx: 15, out_tx: 5, in_kzt: 75000, out_kzt: 25000, n_tx: 20,
      })),
      rule: "Больше max(3 × медиана, медиана + 5) операций за активный день; минимум 7 активных дней. Медиана включает проверяемый день; дни без операций не входят в базу.",
    },
    synchronous: {minimum_senders: 3, day_count: 2, days: [
      {date: "2026-07-10", senders: 5, in_tx: 15, in_kzt: 75000},
      {date: "2026-07-11", senders: 3, in_tx: 8, in_kzt: 40000},
    ]},
    repeated_amounts: {
      minimum_repeats: 3, minimum_transactions: 8,
      directions: {
        in: {status: "assessed", n_transactions: 20, q1_kzt: 5000},
        out: {status: "insufficient_transactions", n_transactions: 3, q1_kzt: null},
      },
      group_count: 1,
      groups: [{date: "2026-07-10", direction: "in", amount_kzt: 5000, n_tx: 3, total_kzt: 15000, counterparties: 3}],
      rule: "Не менее 3 переводов с точно одинаковой суммой за день и направление; сумма ≤ Q1 переводов узла в этом направлении, минимум 8 переводов за период. Q1 — линейная интерполяция.",
    },
    caveat: "Это описательные поводы для проверки, без влияния на роль или приоритет. Повторные малые суммы могут быть обычными платежами; намерение не установлено. Переводы ниже порога исходной выгрузки не восстанавливаются. Общая дата не доказывает одновременность. Показано до 5 примеров; полные количества указаны отдельно. Самопереводы исключены.",
  };
}

async function replacePatterns(page: Page, make: () => TemporalPatterns | undefined) {
  await page.route(/\/api\/runs\/[^/]+\/nodes\/-?\d+$/, async route => {
    const response = await route.fetch();
    const node = await response.json();
    node.temporal.patterns = make();
    await route.fulfill({response, json: node});
  });
}

async function openPatterns(page: Page) {
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#investigate-node").click();
  const region = page.locator("#investigation-content .temporal-patterns");
  await region.locator(":scope > summary").click();
  return region;
}

test("temporal evidence displays formal thresholds, exact total counts and bounded factual examples", async ({page}) => {
  await replacePatterns(page, patterns);
  const region = await openPatterns(page);
  await expect(region).toContainText("строгий порог: больше 7");
  await expect(region).toContainText("Дней выше порога: 6; показано: 5");
  await expect(region).toContainText("минимум 7 активных дней");
  await expect(region.locator("section").nth(0).locator("tbody tr")).toHaveCount(5);
  await expect(region.locator("section").nth(1)).toContainText("Не менее 3 различных плательщиков");
  await expect(region.locator("section").nth(1).locator("tbody tr").first()).toContainText("5 / 15");
  await expect(region.locator("section").nth(2)).toContainText("Q1 = 5 000 ₸");
  await expect(region.locator("section").nth(2)).toContainText("требуется не менее 8");
  await expect(region.locator("section").nth(2).locator("tbody tr")).toHaveCount(1);
  await expect(region.locator("section").nth(2).locator("tbody tr")).toContainText("15 000 ₸ / 3");
  await expect(region.locator(".temporal-patterns-caveat")).toContainText("намерение не установлено");
  await expect(region.locator(".temporal-patterns-caveat")).toContainText("не доказывает одновременность");
});

test("insufficient temporal history is explicit and old snapshots request recomputation", async ({page}) => {
  let legacy = false;
  await replacePatterns(page, () => {
    if (legacy) return undefined;
    const value = patterns();
    value.activity = {...value.activity, status: "insufficient_history", active_days: 2, spike_day_count: 0, spike_days: []};
    value.synchronous = {...value.synchronous, day_count: 0, days: []};
    value.repeated_amounts = {...value.repeated_amounts, group_count: 0, groups: [], directions: {
      in: {status: "insufficient_transactions", n_transactions: 2, q1_kzt: null},
      out: {status: "insufficient_transactions", n_transactions: 0, q1_kzt: null},
    }};
    return value;
  });
  const region = await openPatterns(page);
  await expect(region).toContainText("Оценка не выполнена: активных дней 2, требуется не менее 7");
  await expect(region).toContainText("Недостаточно переводов в обоих направлениях");
  await expect(region).toContainText("отсутствие оценки не означает отсутствие повторов");
  await expect(region.locator("tbody tr")).toHaveCount(0);
  legacy = true;
  await page.reload();
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#investigate-node").click();
  await expect(page.locator("#investigation-content .temporal-patterns-unavailable")).toContainText("Пересчитайте исходные данные");
  await expect(page.locator("#investigation-content .temporal-patterns")).toHaveCount(0);
});

test("temporal rules and caveats are escaped in the UI and included with facts in the downloaded dossier", async ({page}) => {
  const value = patterns();
  value.activity.rule += ' <img src=x onerror="alert(1)">';
  value.caveat += ' <script>alert(1)</script>';
  await replacePatterns(page, () => value);
  const region = await openPatterns(page);
  await expect(region).toContainText('<img src=x onerror="alert(1)">');
  await expect(region.locator("img,script")).toHaveCount(0);
  await page.locator("#investigation-close").click();
  const saved = page.waitForEvent("download");
  await page.locator("#download-dossier").click();
  const download = await saved;
  const content = await readFile(await download.path(), "utf8");
  expect(content).toContain("## Временные паттерны");
  expect(content).toContain("Дней выше порога: 6; показано: 5");
  expect(content).toContain("2026-07-10");
  expect(content).toContain("строгий порог: больше 7");
  expect(content).toContain("намерение не установлено");
  expect(content).toContain("&lt;img");
  expect(content).not.toContain("<script>");
  expect(content).toContain("SHA-256");
});
