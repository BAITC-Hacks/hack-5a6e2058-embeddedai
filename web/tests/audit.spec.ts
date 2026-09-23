import { expect, test } from "@playwright/test";

const isolate = "9007199254741007";
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await expect(page.locator(".top-item.selected")).toHaveCount(1);
});

test("a stale search error cannot replace a newer successful search", async ({ page }) => {
  const gate = deferred();
  const requested = deferred();
  await page.route("**/nodes/123456789", async (route) => {
    requested.resolve();
    await gate.promise;
    await route.fulfill({ status: 404, json: { detail: "Такого gid нет в наборе" } });
  });
  await page.locator("#gid").fill("123456789");
  await page.locator("#search-form button").click();
  await requested.promise;
  await page.locator("#gid").fill(isolate);
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText(isolate);
  const response = page.waitForResponse("**/nodes/123456789");
  gate.resolve();
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator("#alert")).toBeHidden();
  await expect(page.locator(".node-id")).toHaveText(isolate);
});

test("a late graph response cannot select a previous node", async ({ page }) => {
  const first = await page.locator(".top-item").nth(1).getAttribute("data-gid");
  const second = await page.locator(".top-item").nth(2).getAttribute("data-gid");
  const gate = deferred();
  const requested = deferred();
  await page.route(`**/graph?*gid=${first}`, async (route) => {
    requested.resolve();
    await gate.promise;
    await route.continue();
  });
  await page.locator(".top-item").nth(1).click();
  await requested.promise;
  await page.locator(".top-item").nth(2).click();
  await expect(page.locator(".node-id")).toHaveText(second!);
  await expect(page.locator(".top-item.selected")).toHaveAttribute("data-gid", second!);
  const response = page.waitForResponse(`**/graph?*gid=${first}`);
  gate.resolve();
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator(".top-item.selected")).toHaveAttribute("data-gid", second!);
});

test("search clears conflicting filters and filters cancel pending selection", async ({ page }) => {
  await page.locator("#role").selectOption("coordinator");
  await page.locator("#gid").fill(isolate);
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText(isolate);
  await expect(page.locator("#graph-count")).toContainText("1 из 1");
  await expect(page.locator("#role")).toHaveValue("");

  const target = await page.locator(".top-item").first().getAttribute("data-gid");
  const gate = deferred();
  const requested = deferred();
  await page.route(`**/nodes/${target}`, async (route) => {
    requested.resolve();
    await gate.promise;
    await route.continue();
  });
  await page.locator(".top-item").first().click();
  await requested.promise;
  await page.locator("#role").selectOption("transit");
  const response = page.waitForResponse(`**/nodes/${target}`);
  gate.resolve();
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator("#role")).toHaveValue("transit");
  await expect(page.locator(".node-id")).toHaveCount(0);
});

test("failed graph request ends loading and offers recovery", async ({ page }) => {
  await page.route("**/graph?*role=transit*", (route) => route.fulfill({ status: 503, json: { detail: "Временная ошибка графа" } }));
  await page.locator("#role").selectOption("transit");
  await expect(page.locator("#alert")).toContainText("Временная ошибка графа");
  await expect(page.locator("#graph-count")).not.toContainText("Загрузка");
  await expect(page.locator("#graph-empty")).toBeVisible();
  await page.locator("#reset").click();
  await expect(page.locator("#graph-empty")).toBeHidden();
  await expect(page.locator("#alert")).toBeHidden();
});

test("synthetic data remain clearly labelled on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("#dataset-badge")).toBeVisible();
  await expect(page.locator("#dataset-badge")).toHaveText("СИНТЕТИЧЕСКИЙ ПРИМЕР");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});

test("a temporary saved-run failure does not silently switch datasets", async ({ page }) => {
  const run = new URL(page.url()).searchParams.get("run")!;
  await page.route(`**/api/runs/${run}**`, (route) => route.fulfill({ status: 503, json: { detail: "Временно недоступен выбранный расчёт" } }));
  await page.goto(`/?run=${run}`);
  await expect(page.locator("#alert")).toContainText("Временно недоступен выбранный расчёт");
  expect(new URL(page.url()).searchParams.get("run")).toBe(run);
  await expect(page.locator("#dataset-badge")).not.toHaveText("СИНТЕТИЧЕСКИЙ ПРИМЕР");
  await page.unroute(`**/api/runs/${run}**`);
  await page.locator("#retry-load").click();
  await expect(page.locator(".node-id")).toBeVisible();
  expect(new URL(page.url()).searchParams.get("run")).toBe(run);
  await expect(page.locator("#alert")).toBeHidden();
});

test("a shared run works even when browser storage is blocked", async ({ page }) => {
  const url = page.url();
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new DOMException("Storage denied", "SecurityError"); };
    Storage.prototype.setItem = () => { throw new DOMException("Storage denied", "SecurityError"); };
  });
  await page.goto(url);
  await expect(page.locator(".node-id")).toBeVisible();
  await expect(page.locator("#alert")).toBeHidden();
  expect(page.url()).toBe(url);
});

test("an old investigation error cannot replace facts for the new node", async ({ page }) => {
  const first = await page.locator(".node-id").innerText();
  const second = await page.locator(".top-item").nth(1).getAttribute("data-gid");
  const gate = deferred();
  const requested = deferred();
  await page.route(`**/nodes/${first}/investigation`, async (route) => {
    requested.resolve();
    await gate.promise;
    await route.fulfill({ status: 503, json: { detail: "Устаревшая ошибка" } });
  });
  await page.locator("#investigate-node").click();
  await requested.promise;
  await page.locator("#investigation-close").click();
  await page.locator(".top-item").nth(1).click();
  await expect(page.locator(".node-id")).toHaveText(second!);
  await page.locator("#investigate-node").click();
  await expect(page.locator("#investigation-content")).toContainText("Пути от seed");
  const response = page.waitForResponse(`**/nodes/${first}/investigation`);
  gate.resolve();
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator("#investigation-title")).toContainText(second!);
  await expect(page.locator("#investigation-content")).toContainText("Пути от seed");
});

test("investigation is discoverable and displayed role support matches its trace", async ({ page }) => {
  const run = new URL(page.url()).searchParams.get("run");
  const gid = await page.locator(".node-id").innerText();
  const node = await (await page.request.get(`/api/runs/${run}/nodes/${gid}`)).json();
  await expect(page.locator("#investigate-node")).toBeInViewport();
  await page.getByText("Вклад в приоритет и правила", { exact: true }).click();
  await expect(page.locator(".rule-trace tbody tr")).toHaveCount(node.rule_trace.length);
  await expect(page.locator(".rule-trace tbody tr").first().locator("td").last()).toHaveText(`${Math.round(node.role_score * 100)}%`);
  await page.locator("#method-open").click();
  await expect(page.locator("#method-content")).toContainText("точный расчёт");
  await expect(page.locator("#method-dialog")).toHaveAccessibleName("Объяснимый расчёт");
});
