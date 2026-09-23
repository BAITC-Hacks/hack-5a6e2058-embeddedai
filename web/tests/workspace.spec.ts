import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

test("queue includes every filtered role, sorts, paginates and preserves local selection with safe CSV", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const run = new URL(page.url()).searchParams.get("run");
  await page.locator("#tab-queue").click();
  await expect(page.locator(".queue-results tbody tr")).toHaveCount(25);
  const first = page.locator("[data-queue-check]").first();
  const gid = await first.getAttribute("data-queue-check");
  await first.check();
  await page.locator(`[data-queue-note="${gid}"]`).fill('=HYPERLINK("https://example.invalid", "проверить")');
  await expect(page.locator("#queue-selected-count")).toHaveText("Выбрано: 1 / 20");
  await page.locator("#queue-next").click();
  await expect(page.locator("#queue-count")).toContainText("26–50");
  await page.locator("[data-queue-check]").first().check();
  await expect(page.locator("#queue-selected-count")).toHaveText("Выбрано: 2 / 20");
  await page.locator("#role").selectOption("consolidator");
  await expect(page.locator("#queue-view")).toBeVisible();
  const expected = await (await page.request.get(`/api/runs/${run}/nodes?role=consolidator&limit=25`)).json();
  await expect(page.locator(".queue-results tbody tr")).toHaveCount(expected.nodes.length);
  await expect(page.locator("#queue-count")).toContainText(`из ${expected.matched}`);
  await page.locator("#role").selectOption("");
  await page.locator("#queue-sort").selectOption("in_kzt");
  await expect(page.locator("#queue-order")).toHaveValue("desc");
  const sorted = await (await page.request.get(`/api/runs/${run}/nodes?sort=in_kzt&order=desc&limit=25`)).json();
  await expect(page.locator("[data-queue-gid]").first()).toHaveText(sorted.nodes[0].gid);
  const pending = page.waitForEvent("download");
  await page.locator("#queue-export").click();
  const exported = await pending;
  const csv = await readFile(await exported.path(), "utf8");
  expect(csv).toContain(`"'${gid}"`);
  expect(csv).toContain(`"'=HYPERLINK(`);
  await page.reload();
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#tab-queue").click();
  await expect(page.locator("#queue-selected-count")).toHaveText("Выбрано: 2 / 20");
  await expect(page.locator(`[data-queue-note="${gid}"]`)).toHaveValue('=HYPERLINK("https://example.invalid", "проверить")');
  await page.locator(`[data-queue-gid="${gid}"]`).click();
  await expect(page.locator("#queue-view")).toBeVisible();
  await expect(page.locator(".node-id")).toHaveText(gid!);
  await page.locator("#queue-search").fill("999999999999999999999999");
  await expect(page.locator(".queue-empty")).toBeVisible();
  await expect(page.locator("#queue-count")).toContainText("0 подходящих");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});

test("AI uses selected string gids and session-only access code, escapes output and opens grounded references", async ({ page }) => {
  await page.route("**/api/assistant/status", (route) => route.fulfill({ json: { enabled: true, model: "test-model", access_required: true } }));
  let payload: {question: string; selected_gids: string[]} | undefined;
  let sentToken = "";
  await page.route("**/assistant", async (route) => {
    payload = route.request().postDataJSON();
    sentToken = route.request().headers()["x-assistant-token"];
    const gid = payload!.selected_gids[0];
    await route.fulfill({ json: {
      answer: '<img src=x onerror="alert(1)"> Получатель установлен по графу.',
      claims: [{ text: "Наблюдается входящий перевод.", gids: [gid, "not-in-facts"] }],
      nodes: payload!.selected_gids.map(id => ({ gid: id, role: "consolidator", evidence: "Два плательщика в выборке." })),
      facts: [{ gid, paths: [{ gids: [payload!.selected_gids[1], gid], edges: [{src: payload!.selected_gids[1], dst: gid, sum_kzt: 30000, n_tx: 3}] }] }],
      limitations: ["Не доказательство происхождения денег."], model: "test-model", usage: {}, query: {operation: "common_recipients", gids: payload!.selected_gids, role: "all", limit: 10, max_hops: 1, min_sources: 0, sort_by: "priority_score", clarification: ""},
    } });
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#tab-queue").click();
  await page.locator("[data-queue-check]").first().check();
  await page.locator("[data-queue-check]").nth(1).check();
  const chosen = await page.locator("[data-queue-check]:checked").evaluateAll(nodes => nodes.map(node => (node as HTMLInputElement).dataset.queueCheck!));
  await page.locator("[data-queue-note]").first().fill("Секретная локальная заметка");
  await page.locator("#queue-ask").click();
  await expect(page.locator("#assistant-view")).toBeVisible();
  await expect(page.locator("#assistant-context")).toContainText("2 узл.");
  await expect(page.locator(".assistant-disclosure")).toContainText("Таблицы графа остаются на сервере");
  await page.locator("#assistant-question").fill("Кто собирает деньги с этих двоих?");
  await page.locator("#assistant-token").fill("test-access-code");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer-text")).toContainText("<img src=x");
  await expect(page.locator("#assistant-result img")).toHaveCount(0);
  expect(payload).toEqual({ question: "Кто собирает деньги с этих двоих?", selected_gids: chosen });
  expect(sentToken).toBe("test-access-code");
  await expect(page.locator(".assistant-paths")).toContainText("3 пер.");
  await expect(page.locator(".assistant-paths [data-assistant-gid]")).toHaveCount(3);
  await page.locator(".assistant-query summary").click();
  await expect(page.locator(".assistant-query")).toContainText("Общие получатели");
  await expect(page.locator(".assistant-query")).toContainText("Все 2 выбранных узлов");
  await expect(page.locator('[data-assistant-gid="not-in-facts"]')).toHaveCount(0);
  await page.locator("[data-assistant-gid]").first().click();
  await expect(page.locator(".node-id")).toHaveText(chosen[0]);
  await expect(page.locator("#graph-wrapper")).toBeVisible();
  await page.reload();
  await page.locator("#tab-assistant").click();
  await expect(page.locator("#assistant-token")).toHaveValue("");
});

test("AI cancellation ignores a late response and API failure stays an actionable error", async ({ page }) => {
  await page.route("**/api/assistant/status", (route) => route.fulfill({ json: { enabled: true, model: "test-model" } }));
  const gate = deferred();
  const requested = deferred();
  let requests = 0;
  await page.route("**/assistant", async (route) => {
    requests++;
    if (requests === 1) {
      requested.resolve();
      await gate.promise;
      await route.fulfill({ json: { answer: "STALE ANSWER", claims: [], nodes: [], facts: [], limitations: [], model: "test-model", usage: {}, query: {} } }).catch(() => {});
    } else await route.fulfill({ status: 503, json: { detail: "Провайдер временно недоступен. Повторите позже." } });
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#tab-assistant").click();
  await page.locator("#assistant-question").fill("Покажи входящие связи");
  await page.locator("#assistant-send").click();
  await requested.promise;
  await expect(page.locator("#assistant-send")).toBeDisabled();
  await page.locator("#assistant-cancel").click();
  gate.resolve();
  await expect(page.locator("#assistant-result")).toContainText("Запрос отменён");
  await page.locator("#assistant-send").click();
  await expect(page.locator("#assistant-result")).toContainText("Провайдер временно недоступен");
  await expect(page.locator(".assistant-answer")).toHaveCount(0);
  await expect(page.locator("#assistant-send")).toBeEnabled();
});

test("AI is optional and unavailable status does not block graph or queue", async ({ page }) => {
  await page.route("**/api/assistant/status", (route) => route.fulfill({ json: { enabled: false, model: "test-model", reason: "Ключ не настроен" } }));
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#tab-assistant").click();
  await expect(page.locator("#assistant-status")).toContainText("Ключ не настроен");
  await expect(page.locator("#assistant-send")).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.locator("#tab-queue").click();
  await expect(page.locator(".queue-results tbody tr")).toHaveCount(25);
});

test("role and community sensitivity are computed on demand with honest limits and node links", async ({ page }) => {
  let requested = false;
  page.on("request", request => { if (request.url().endsWith("/robustness")) requested = true; });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  expect(requested).toBe(false);
  await page.locator("#tab-analysis").click();
  await page.locator("#robustness-load").click();
  await expect(page.locator("#robustness-result")).toContainText("Минимальная доля неизменных ролей");
  await expect(page.locator("#robustness-result")).toContainText("Индекс Рэнда");
  expect(requested).toBe(true);
  await expect(page.locator("#analysis-view")).toContainText("не accuracy");
  await page.getByText("Состав окружения TOP 20 при разных настройках", { exact: true }).click();
  const link = page.locator("[data-robustness-gid]").filter({visible:true}).first();
  const gid = await link.getAttribute("data-robustness-gid");
  await link.click();
  await expect(page.locator(".node-id")).toHaveText(gid!);
});

test("graph layout runs in a cancellable worker while the card and controls remain usable", async ({ page }) => {
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    const pending: (() => void)[] = [];
    Object.assign(window, { releaseLayouts: () => pending.splice(0).forEach(release => release()) });
    window.Worker = class extends NativeWorker {
      postMessage(message: unknown, transfer: Transferable[] = []) {
        pending.push(() => super.postMessage(message, transfer));
      }
    };
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await expect(page.locator("#graph-count")).toHaveText("Раскладываем граф в фоне…");
  await expect(page.locator("#graph-png")).toBeDisabled();
  await page.locator("#theme-select").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator("#gid").fill("9007199254741007");
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText("9007199254741007");
  await expect(page.locator("#graph-count")).toContainText("1 из 1");
  await expect(page.locator("#graph-png")).toBeEnabled();
  await page.evaluate(() => (window as unknown as {releaseLayouts: () => void}).releaseLayouts());
  await expect(page.locator("#graph-count")).toContainText("1 из 1");
  await expect(page.locator(".node-id")).toHaveText("9007199254741007");
});

test("switching runs clears queue state and cancels an outstanding AI answer", async ({ page }) => {
  await page.route("**/api/assistant/status", (route) => route.fulfill({ json: { enabled: true, model: "test-model" } }));
  const gate = deferred();
  const requested = deferred();
  await page.route("**/assistant", async (route) => {
    requested.resolve();
    await gate.promise;
    await route.fulfill({ json: { answer: "PREVIOUS RUN ANSWER", claims: [], nodes: [], facts: [], limitations: [], model: "test-model", usage: {}, query: {} } }).catch(() => {});
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const before = new URL(page.url()).searchParams.get("run");
  await page.locator("#tab-queue").click();
  await page.locator("[data-queue-check]").first().check();
  await page.locator("[data-queue-note]").first().fill("Заметка только старого расчёта");
  await page.locator("#queue-ask").click();
  await page.locator("#assistant-question").fill("Кто получает деньги?");
  await page.locator("#assistant-send").click();
  await requested.promise;
  await page.locator("#upload-open").click();
  for (const name of ["nodes", "edges", "transactions"])
    await page.locator(`input[name=${name}]`).setInputFiles(new URL(`../../var/e2e-data/${name}.parquet`, import.meta.url).pathname);
  await page.locator("#analyze-button").click();
  await expect(page.locator("#upload-dialog")).toBeHidden();
  expect(new URL(page.url()).searchParams.get("run")).not.toBe(before);
  gate.resolve();
  await expect(page.locator("#queue-selected-count")).toHaveText("Выбрано: 0 / 20");
  await expect(page.locator("#assistant-result")).toBeEmpty();
  await page.locator("#tab-queue").click();
  await expect(page.locator("[data-queue-note]").first()).toHaveValue("");
});

test("a missing map chunk gives a recoverable error without blocking factual cards", async ({ page }) => {
  await page.route("**/assets/cytoscape.esm-*.js", route => route.abort());
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await expect(page.locator("#alert")).toContainText("Не удалось загрузить модуль карты");
  await expect(page.locator("#graph")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#graph-png")).toBeDisabled();
  await page.unroute("**/assets/cytoscape.esm-*.js");
  await page.reload();
  await expect(page.locator("#graph-png")).toBeEnabled();
  await expect(page.locator("#alert")).toBeHidden();
});
