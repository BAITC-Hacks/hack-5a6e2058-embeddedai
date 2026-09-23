import { expect, test } from "@playwright/test";

test("profile comparison explains abstention for an isolated seed", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#gid").fill("9007199254741007");
  await page.locator("#search-form button").click();
  await expect(page.locator(".node-id")).toHaveText("9007199254741007");
  await page.locator("#node-profile summary").click();
  await expect(page.locator("#node-profile")).toContainText("0 сигналов");
  await expect(page.locator("#node-profile")).toContainText("исключён из сравнения профилей");
  await expect(page.locator("#node-profile")).toContainText("не доказательство нарушения");
  await expect(page.locator("#alert")).toBeHidden();
});

test("an unusual profile displays its numerical rule without hiding the main role", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const target = await page.locator(".top-item").nth(1).getAttribute("data-gid");
  const reason = "Число плательщиков: 200 > 43.75 (Q3 + 3×IQR; Q1=5.75, Q3=15.25). Колено 1, 20 активных узлов.";
  // The standard synthetic demo has no flagged cohort. Exercise the UI contract
  // with a valid, explicit signal; numerical detection is covered in Python.
  await page.route(`**/nodes/${target}`, async (route) => {
    const response = await route.fetch();
    const node = await response.json();
    node.anomaly_profile = {
      cohort_depth: 1,
      cohort_size: 20,
      minimum_cohort_size: 20,
      signals: [{ metric: "in_deg", value: 200, threshold: 43.75, q1: 5.75, q3: 15.25, text: reason }],
      caveat: "Сигналы не меняют роль и приоритет. Не доказательство нарушения или дробления.",
    };
    await route.fulfill({ response, json: node });
  });
  await page.locator(".top-item").nth(1).click();
  await expect(page.locator(".node-id")).toHaveText(target!);
  await expect(page.locator("#node-profile")).toHaveAttribute("open", "");
  await expect(page.locator("#node-profile li")).toHaveText(reason);
  await expect(page.locator("#node-profile")).toContainText("Сигналы не меняют роль и приоритет");
  await expect(page.locator(".score-grid")).toBeVisible();
  await expect(page.locator("#detail > .evidence")).not.toBeEmpty();
  await expect(page.locator("#alert")).toBeHidden();
});
