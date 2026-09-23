import { expect, test, type Locator, type Page } from "@playwright/test";

// Move only the document: scrollIntoView could scroll an overflow:hidden ancestor
// and accidentally conceal the very clipping regression these tests exercise.
async function showRegion(region: Locator) {
  await region.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    window.scrollBy(0, bounds.top - Math.max(8, (innerHeight - bounds.height) / 2));
  });
}

async function wheelRegion(page: Page, region: Locator, delta = 280) {
  await showRegion(region);
  const before = await region.evaluate((element) => element.scrollTop);
  const point = await region.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const x = bounds.left + Math.min(90, bounds.width / 2);
    const y = Math.max(1, bounds.top) + Math.min(bounds.height, innerHeight - bounds.top) / 2;
    return { x, y, receivesPointer: element.contains(document.elementFromPoint(x, y)) };
  });
  expect(point.receivesPointer).toBe(true);
  await page.mouse.move(point.x, point.y);
  await page.mouse.wheel(0, delta);
  await expect.poll(() => region.evaluate((element) => element.scrollTop)).toBeGreaterThan(before);
  await expect(page.locator(".workspace")).toHaveJSProperty("scrollTop", 0);
}

async function keyboardEnd(page: Page, region: Locator) {
  await showRegion(region);
  await region.evaluate((element) => (element as HTMLElement).focus({ preventScroll: true }));
  await expect(region).toBeFocused();
  await page.keyboard.press("End");
  await expect.poll(() => region.evaluate((element) =>
    element.scrollHeight - element.clientHeight - element.scrollTop,
  )).toBeLessThan(2);
  await expect(page.locator(".workspace")).toHaveJSProperty("scrollTop", 0);
}

async function pointerTarget(target: Locator) {
  await expect(target).toBeInViewport();
  const point = await target.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2;
    const y = bounds.top + bounds.height / 2;
    return { x, y, reachable: element.contains(document.elementFromPoint(x, y)) };
  });
  expect(point.reachable).toBe(true);
  return point;
}

async function dimensions(page: Page) {
  return page.evaluate(() => {
    const workspace = document.querySelector(".workspace")!.getBoundingClientRect();
    const panel = document.querySelector(".graph-panel")!.getBoundingClientRect();
    const toolbar = document.querySelector(".graph-toolbar")!.getBoundingClientRect();
    return { workspace: workspace.height, panel: panel.height, toolbar: toolbar.top - workspace.top };
  });
}

for (const viewport of [
  { width: 1440, height: 1080 },
  { width: 1366, height: 768 },
  { width: 820, height: 1180 },
  { width: 390, height: 844 },
]) {
  test(`panels scroll to their last results at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/");
    await expect(page.locator(".top-item")).toHaveCount(50);
    await expect(page.locator(".node-id")).toBeVisible();
    const initial = await dimensions(page);

    const top = page.locator("#top-list");
    await wheelRegion(page, top);
    await keyboardEnd(page, top);
    await expect(top.locator(".rank").last()).toHaveText("50");
    await pointerTarget(top.locator(".top-item").last());

    await page.locator("#tab-queue").click();
    const queue = page.locator("#queue-view");
    await expect(queue.locator("tbody tr")).toHaveCount(25);
    const opened = await dimensions(page);
    expect(Math.abs(opened.workspace - initial.workspace)).toBeLessThan(2);
    expect(Math.abs(opened.panel - initial.panel)).toBeLessThan(2);
    expect(Math.abs(opened.toolbar - initial.toolbar)).toBeLessThan(2);
    await wheelRegion(page, queue);
    await keyboardEnd(page, queue);
    await pointerTarget(queue.locator("[data-queue-gid]").last());
    await expect(page.locator("#queue-count")).toContainText("1–25");
    const next = await pointerTarget(page.locator("#queue-next"));
    // A physical click proves pagination was already reachable, without Locator
    // click's automatic scrolling repairing the layout on our behalf.
    await page.mouse.click(next.x, next.y);
    await expect(page.locator("#queue-count")).toContainText("26–50");
    await expect(page.locator(".workspace")).toHaveJSProperty("scrollTop", 0);

    for (const tab of ["clusters", "analysis"]) {
      await page.locator(`#tab-${tab}`).click();
      const region = page.locator(`#${tab}-view`);
      await expect(region).toBeVisible();
      await wheelRegion(page, region);
      await keyboardEnd(page, region);
      const last = tab === "clusters"
        ? region.locator("[data-cluster]").last()
        : region.locator("#robustness-load");
      await pointerTarget(last);
      const current = await dimensions(page);
      expect(Math.abs(current.panel - initial.panel)).toBeLessThan(2);
    }

    const detail = page.locator("#detail");
    const hasInnerScroll = await detail.evaluate((element) => element.scrollHeight > element.clientHeight + 2);
    if (hasInnerScroll) {
      await wheelRegion(page, detail);
      await keyboardEnd(page, detail);
    } else {
      // The phone layout intentionally lets the complete card scroll with the page.
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    }
    await pointerTarget(detail.locator(":scope > :last-child"));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("a long AI answer remains scrollable through its final limitation without moving the workspace", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.route("**/api/assistant/status", route => route.fulfill({ json: { enabled: true, model: "scroll-test" } }));
  await page.route("**/assistant", route => route.fulfill({ json: {
    answer: Array.from({ length: 40 }, (_, index) => `Наблюдение ${index + 1}: требуется проверка связей.`).join("\n"),
    claims: [], nodes: [], facts: [],
    limitations: ["Последнее ограничение тестового ответа."],
    model: "scroll-test", usage: {}, query: { operation: "node_summary", gids: [], role: "all" },
  } }));
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const initial = await dimensions(page);
  await page.locator("#assistant-open").click();
  await page.locator("#assistant-question").fill("Покажи подробный профиль узла");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer-text")).toContainText("Наблюдение 40");
  const region = page.locator("#assistant-view");
  await page.locator(".assistant-limitations summary").click();
  await region.evaluate(element => {element.scrollTop = 0;});
  await wheelRegion(page, region);
  await keyboardEnd(page, region);
  await pointerTarget(page.getByText("Последнее ограничение тестового ответа.", { exact: true }));
  expect(Math.abs((await dimensions(page)).panel - initial.panel)).toBeLessThan(2);
});
