import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const unsafeGid = "9007199254741007";
test.beforeEach(async ({page}) => {
  await page.goto("/");
  await expect(page.locator(".top-item")).toHaveCount(50);
  await expect(page.locator(".node-id")).toBeVisible();
});

test("exact identifiers, isolated seed, unknown ID and error recovery", async ({page}) => {
  await page.locator("#gid").fill(unsafeGid);
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText(unsafeGid);
  await expect(page.locator("#graph-count")).toContainText("1 из 1");
  await expect(page.locator("#detail")).toContainText("не имеет наблюдаемых рёбер");
  await page.locator("#gid").fill("123456789");
  await page.locator("#search-form button").click();
  await expect(page.locator("#alert")).toContainText("Такого gid нет");
  await expect(page.locator("#graph")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#graph-count")).toHaveText("Граф не загружен");
  await expect(page.locator("#gid")).toHaveValue("123456789");
  await page.locator(".top-item").first().click();
  await expect(page.locator("#alert")).toBeHidden();
});

test("evidence paths, daily amounts and downloadable dossier", async ({page}) => {
  const gid = await page.locator(".node-id").innerText();
  await page.locator("#investigate-node").click();
  await expect(page.locator("#investigation-dialog")).toBeVisible();
  await expect(page.locator("#investigation-content")).toContainText("Пути от seed");
  await expect(page.locator("#investigation-content svg")).toBeVisible();
  await page.locator("#investigation-close").click();
  const pending = page.waitForEvent("download");
  await page.locator("#download-dossier").click();
  const download = await pending;
  const text = await readFile(await download.path(), "utf8");
  expect(text).toContain(gid);
  expect(text).toContain("SHA-256");
  expect(text).toContain("не доверительный интервал");
});

test("sensitivity, removal simulation, community overview and PNG", async ({page}) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (/invalid|not a valid/i.test(message.text())) errors.push(message.text()); });
  await page.locator("#tab-analysis").click();
  await expect(page.locator(".sensitivity-grid > div")).toHaveCount(12);
  await page.locator("#simulate").click();
  await expect(page.locator("#resilience-result")).toContainText("Удалено 5 активных узлов");
  await expect(page.locator("#resilience-result tbody tr")).toHaveCount(3);
  await page.locator("#community-map").click();
  await expect(page.locator("#graph-count")).toContainText("сообществ");
  const pending = page.waitForEvent("download");
  await page.locator("#graph-png").click();
  const download = await pending;
  const bytes = await readFile(await download.path());
  expect(bytes.subarray(1,4).toString()).toBe("PNG");
  await page.locator("#tab-clusters").click();
  await page.locator("[data-cluster]").first().click();
  await expect(page.locator("#graph-wrapper")).toBeVisible();
  await expect(page.locator("#graph-count")).not.toContainText("сообществ");
  expect(errors).toEqual([]);
});

test("upload, strict CSV, refresh persistence and mobile layout", async ({page}) => {
  await page.locator("#upload-open").click();
  for (const name of ["nodes", "edges", "transactions"]) {
    const path = fileURLToPath(new URL(`../../var/e2e-data/${name}.parquet`, import.meta.url));
    await page.locator(`input[name=${name}]`).setInputFiles(path);
  }
  await page.locator("#analyze-button").click();
  await expect(page.locator("#upload-dialog")).toBeHidden();
  await expect(page.locator("#dataset-badge")).toHaveText("ЗАГРУЖЕННЫЙ НАБОР");
  const run = new URL(page.url()).searchParams.get("run");
  const pending = page.waitForEvent("download");
  await page.locator("#export-select").selectOption("nodes_roles.csv");
  const download = await pending;
  const csv = await readFile(await download.path(), "utf8");
  expect(csv.split("\n")[0]).toBe("gid,role,role_score,cluster_id,priority_score,evidence");
  expect(csv).toContain(unsafeGid);
  await page.reload();
  await expect(page.locator(".top-item")).toHaveCount(50);
  expect(new URL(page.url()).searchParams.get("run")).toBe(run);
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.locator("#tab-analysis").click();
  await page.locator("#simulate").click();
  await expect(page.locator("#resilience-result")).toContainText("Удалено 5");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});
