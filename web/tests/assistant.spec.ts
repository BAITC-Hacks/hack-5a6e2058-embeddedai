import { expect, test } from "@playwright/test";

const reply = {
  answer: "Сначала проверьте наблюдаемые связи и ограничения.",
  claims: [], nodes: [], facts: [], limitations: [], model: "test-model", usage: {},
  query: {steps: [{tool: "overview", arguments: {}, summary: "Проверена структура графа."}]},
  conversation_id: "test-conversation", actions: [], followups: [],
};

test("global AI composer is above the dataset and stays available from every workspace view", async ({page}) => {
  await page.route("**/api/assistant/status", route => route.fulfill({json: {enabled: true, model: "test-model"}}));
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const placement = await page.evaluate(() => ({
    composer: document.querySelector("#assistant-question")!.getBoundingClientRect().top,
    metrics: document.querySelector("#metrics")!.getBoundingClientRect().top,
    inWorkspace: Boolean(document.querySelector(".workspace #assistant-question")),
  }));
  expect(placement.composer).toBeLessThan(placement.metrics);
  expect(placement.inWorkspace).toBe(false);
  await expect(page.locator("#assistant-question")).toBeInViewport();
  await expect(page.locator("#tab-assistant")).toHaveCount(0);
  for (const view of ["queue", "clusters", "analysis", "graph"]) {
    await page.locator(`#tab-${view}`).click();
    await expect(page.locator("#assistant-question")).toBeVisible();
  }
  for (const theme of ["light", "dark"]) {
    await page.locator("#theme-select").selectOption(theme);
    await page.setViewportSize({width: 390, height: 844});
    await page.locator("#assistant-open").click();
    await expect(page.locator("#assistant-question")).toBeFocused();
    await expect(page.locator("#assistant-question")).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test("AI remembers a conversation, sends actual context, links grounded citations and only navigates on explicit click", async ({page}) => {
  await page.route("**/api/assistant/status", route => route.fulfill({json: {enabled: true, model: "test-model"}}));
  const requests: Record<string, any>[] = [];
  await page.route("**/assistant", async route => {
    const request = route.request().postDataJSON();
    requests.push(request);
    const gid = request.context.active_gid ?? "9007199254741007";
    await route.fulfill({json: {...reply,
      answer: `Проверьте [gid:${gid}]. Неподтверждённая ссылка [gid:999]. <script>bad()</script>`,
      nodes: [{gid, role: "consolidator", evidence: "Наблюдается входящий поток."}],
      facts: [{gid, in_kzt: 123456, in_deg: 5}],
      followups: ["А почему именно он?"],
      actions: [{type: "show_view", label: "Открыть очередь", gid: null, view: "queue"}],
    }});
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const activeGid = await page.locator(".node-id").textContent();
  await page.locator("#assistant-question").fill("С чего начать?");
  await page.locator("#assistant-question").press("Enter");
  await expect(page.locator(".assistant-answer")).toHaveCount(1);
  expect(requests[0]).toMatchObject({question: "С чего начать?", selected_gids: [], conversation_id: null,
    context: {active_gid: activeGid, active_tab: "graph", filters: {role: null, cluster: null, depth: null, seeds: false}}});
  expect(requests[0]).not.toHaveProperty("history");
  await expect(page.locator(".assistant-answer-text script")).toHaveCount(0);
  await expect(page.locator(".assistant-answer-text")).toContainText("<script>bad()</script>");
  await expect(page.locator(`.assistant-answer-text [data-assistant-gid="${activeGid}"]`)).toBeVisible();
  await expect(page.locator('[data-assistant-gid="999"]')).toHaveCount(0);
  await page.locator(".assistant-facts summary").click();
  await expect(page.locator(".assistant-facts")).toContainText("Плательщики");
  await expect(page.locator(".assistant-facts")).toContainText("123");
  await expect(page.locator("#graph-wrapper")).toBeVisible();
  await expect(page.locator("#queue-view")).toBeHidden();
  await page.locator("[data-assistant-action]").click();
  await expect(page.locator("#queue-view")).toBeVisible();
  await page.locator("[data-assistant-followup]").click();
  await expect(page.locator("#assistant-question")).toHaveValue("А почему именно он?");
  expect(requests).toHaveLength(1);
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer")).toHaveCount(2);
  expect(requests[1].conversation_id).toBe("test-conversation");
  expect(requests[1].context.active_tab).toBe("queue");
  await page.locator("#assistant-new").click();
  await expect(page.locator("#assistant-result")).toBeEmpty();
  await page.locator("#role").selectOption("consolidator");
  await page.locator("#assistant-question").fill("Объясни эту выборку");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer")).toHaveCount(1);
  expect(requests[2].conversation_id).toBe(null);
  expect(requests[2].context.filters.role).toBe("consolidator");
  expect(requests[2].context.active_gid).toBe(null);
  await page.reload();
  await expect(page.locator("#assistant-result")).toBeEmpty();
});

test("an expired dialogue clears continuation without issuing an automatic paid retry", async ({page}) => {
  await page.route("**/api/assistant/status", route => route.fulfill({json: {enabled: true, model: "test-model"}}));
  const requests: Record<string, any>[] = [];
  await page.route("**/assistant", async route => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 2) await route.fulfill({status: 409, json: {detail: "Диалог истёк. Повторите вопрос, чтобы начать новый."}});
    else await route.fulfill({json: reply});
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  await page.locator("#assistant-question").fill("Обзор графа");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer")).toHaveCount(1);
  await page.locator("#assistant-question").fill("Что дальше?");
  await page.locator("#assistant-send").click();
  await expect(page.locator("#assistant-result .alert")).toContainText("Диалог истёк");
  expect(requests).toHaveLength(2);
  expect(requests[1].conversation_id).toBe("test-conversation");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer")).toHaveCount(2);
  expect(requests[2].conversation_id).toBe(null);
});

test("AI receives the ordered visible queue page with search and sort, without local notes", async ({page}) => {
  await page.route("**/api/assistant/status", route => route.fulfill({json: {enabled: true, model: "test-model"}}));
  let payload: Record<string, any> | undefined;
  await page.route("**/assistant", async route => {
    payload = route.request().postDataJSON();
    await route.fulfill({json: reply});
  });
  await page.goto("/");
  await expect(page.locator(".node-id")).toBeVisible();
  const run = new URL(page.url()).searchParams.get("run");
  await page.locator("#tab-queue").click();
  const searched = page.waitForResponse(response => response.url().includes("/nodes?") && response.url().includes("search=900"));
  await page.locator("#queue-search").fill("900");
  await searched;
  await page.locator("#queue-sort").selectOption("in_kzt");
  await expect(page.locator("#queue-order")).toHaveValue("desc");
  const firstPage = await (await page.request.get(`/api/runs/${run}/nodes?search=900&sort=in_kzt&order=desc&offset=0&limit=25`)).json();
  await expect(page.locator("[data-queue-gid]").first()).toHaveText(firstPage.nodes[0].gid);
  await expect(page.locator("#queue-next")).toBeEnabled();
  await page.locator("#queue-next").click();
  const secondPage = await (await page.request.get(`/api/runs/${run}/nodes?search=900&sort=in_kzt&order=desc&offset=25&limit=25`)).json();
  await expect(page.locator("[data-queue-gid]").first()).toHaveText(secondPage.nodes[0].gid);
  await expect(page.locator("#queue-count")).toContainText("26–50");
  await page.locator("[data-queue-note]").first().fill("Личная заметка, не отправлять");
  await page.locator("#assistant-question").fill("Почему первый в этой очереди?");
  await page.locator("#assistant-send").click();
  await expect(page.locator(".assistant-answer")).toHaveCount(1);
  expect(payload?.context.queue).toEqual({
    visible_gids: secondPage.nodes.slice(0,20).map((node: {gid: string}) => node.gid),
    search: "900", sort: "in_kzt", order: "desc", offset: 25,
    matched: secondPage.matched, has_more_visible: true,
  });
  expect(JSON.stringify(payload)).not.toContain("Личная заметка");
  expect(payload?.selected_gids).toEqual([]);
});
