import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

async function ready(page: Page) {
  await expect(page.locator(".node-id")).toBeVisible();
  await expect(page.locator("#graph-png")).toBeEnabled();
}

// Use the browser's CSS parser to handle color-mix(), then composite against
// the closest opaque ancestor so contrast checks match the rendered panel.
async function contrast(page: Page, selector: string) {
  return page.locator(selector).first().evaluate((element) => {
    const context = document.createElement("canvas").getContext("2d")!;
    function rgb(value: string) {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = value;
      context.fillRect(0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data];
    }
    function luminance(color: number[]) {
      const values = color.slice(0, 3).map((v) => {
        const channel = v / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
    }
    let parent: Element | null = element;
    let background = [255, 255, 255, 255];
    while (parent) {
      const candidate = rgb(getComputedStyle(parent).backgroundColor);
      if (candidate[3] === 255) { background = candidate; break; }
      parent = parent.parentElement;
    }
    const a = luminance(rgb(getComputedStyle(element).color));
    const b = luminance(background);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}

test("saved theme applies before the application bundle and persists across reload", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.addInitScript(() => localStorage.setItem("money-graph-theme", "dark"));
  await page.route((url) => url.pathname === "/", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": "script-src 'self'; object-src 'none'" } });
  });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/assets/*.js", async (route) => { await gate; await route.continue(); });
  await page.goto("/", { waitUntil: "commit" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("#app")).toBeEmpty();
  release();
  await ready(page);
  await expect(page.getByRole("combobox", { name: "Цветовая тема" })).toHaveValue("dark");
  await page.locator("#theme-select").selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("money-graph-theme"))).toBe("light");
  // Remove the setup script so the reload reads the user-selected value.
  const storage = await page.context().storageState();
  const next = await page.context().browser()!.newContext({ storageState: storage, colorScheme: "dark" });
  const fresh = await next.newPage();
  await fresh.goto(page.url());
  await ready(fresh);
  await expect(fresh.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(fresh.locator("#theme-select")).toHaveValue("light");
  await next.close();
});

test("system follows the OS, manual choice wins, keyboard and blocked storage work", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
    Storage.prototype.setItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await ready(page);
  await expect(page.locator("#theme-select")).toHaveValue("system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.locator("#theme-select").focus();
  await page.keyboard.press("End");
  await expect(page.locator("#theme-select")).toHaveValue("dark");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator("#theme-select").selectOption("system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.locator("#gid").fill("9007199254741007");
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText("9007199254741007");
  expect(errors).toEqual([]);
});

test("both themes cover panels, dialogs, timelines and mobile without losing the graph", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await ready(page);
  const gid = await page.locator(".node-id").innerText();
  let graphRequests = 0;
  page.on("request", (request) => { if (/\/graph\?/.test(request.url())) graphRequests++; });
  for (const mode of ["light", "dark"]) {
    await page.locator("#theme-select").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    await expect(page.locator(".node-id")).toHaveText(gid);
    for (const selector of [".subtitle", ".evidence", ".node-metrics dt", ".fine-print", ".role-tag", "#gid", "#theme-select", "#upload-open"])
      expect(await contrast(page, selector), `${mode}: ${selector}`).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: `/tmp/money-graph-theme-${mode}.png`, fullPage: true });
    await page.locator("#investigate-node").click();
    await expect(page.locator("#investigation-content svg")).toBeVisible();
    expect(await contrast(page, "#investigation-dialog p")).toBeGreaterThanOrEqual(4.5);
    expect(await contrast(page, "#investigation-dialog .warning-box")).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: `/tmp/money-graph-theme-${mode}-dialog.png`, fullPage: true });
    await page.locator("#investigation-close").click();
    await page.locator("#upload-open").click();
    expect(await contrast(page, "#upload-dialog .file-label")).toBeGreaterThanOrEqual(4.5);
    await page.locator("#upload-close").click();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator("#theme-select")).toBeVisible();
    await expect(page.locator("#dataset-badge")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/money-graph-theme-${mode}-mobile.png`, fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 });
  }
  expect(graphRequests).toBe(0);
  expect(errors).toEqual([]);
});

test("PNG uses the active graph theme after toggling and preserves role colors", async ({ page }) => {
  await page.goto("/");
  await ready(page);
  const roleColor = await page.locator(".role-tag").first().evaluate((el) => (el as HTMLElement).style.getPropertyValue("--role-color"));
  for (const [theme, expected] of [["dark", [16, 31, 46, 255]], ["light", [251, 252, 253, 255]]] as const) {
    await page.locator("#theme-select").selectOption(theme);
    const pending = page.waitForEvent("download");
    await page.locator("#graph-png").click();
    const download = await pending;
    const bytes = await readFile((await download.path())!);
    expect(bytes.subarray(1, 4).toString()).toBe("PNG");
    const pixel = await page.evaluate(async (base64) => {
      const image = new Image();
      image.src = `data:image/png;base64,${base64}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext("2d")!;
      context.drawImage(image, 0, 0);
      return [...context.getImageData(0, 0, 1, 1).data];
    }, bytes.toString("base64"));
    expect(pixel).toEqual(expected);
    expect(await page.locator(".role-tag").first().evaluate((el) => (el as HTMLElement).style.getPropertyValue("--role-color"))).toBe(roleColor);
  }
});
