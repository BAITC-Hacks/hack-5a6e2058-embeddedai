import cytoscape, { type Core } from "cytoscape";
import "./style.css";
import { downloadDossier, openInvestigation, renderAnalysis, timeline } from "./analysis";
import type {
  Cluster,
  CommunityGraph,
  Edge,
  GraphResponse,
  NodeDetail,
  Report,
  TopNode,
} from "./types";
import {
  $,
  api,
  colors,
  communityColor,
  escapeHtml,
  labels,
  metricLabels,
  money,
  number,
  percent,
} from "./ui";

let runId = "";
let currentReport: Report;
let runSequence = 0;
let communityMode = false;
let cy: Core | undefined;
let graphSequence = 0;
let detailSequence = 0;
let currentGid = "";

$("app").innerHTML = `
  <header class="header"><a class="brand" href="/" aria-label="На главную"><span class="brand-icon">◈</span><span>Граф денег<small>EMBEDDEDAI / FINANCIAL INTELLIGENCE</small></span></a>
    <div class="header-right"><span id="dataset-badge" class="tag">Подключение…</span><button id="upload-open" class="button primary">＋ Загрузить данные</button></div></header>
  <main><div class="heading"><div><p class="eyebrow">АНАЛИТИКА ТРАНЗАКЦИОННОЙ СЕТИ</p><h1>Увидеть связи. Объяснить приоритет.</h1><p class="subtitle">От потока переводов — к обоснованной гипотезе для проверки.</p></div><div class="export-wrap"><label for="export-select">Выгрузить результат</label><select id="export-select"><option value="">Выберите CSV ↓</option><option value="nodes_roles.csv">Все узлы и роли</option><option value="clusters.csv">Кластеры</option><option value="top_nodes.csv">Топ приоритетов</option></select></div></div>
  <div id="alert" class="alert" role="status" hidden></div>
  <section id="metrics" class="metrics" aria-label="Показатели набора"><div class="metric">Загрузка показателей…</div></section>
  <div class="context-strip"><span id="period">—</span><span id="runtime">—</span><button id="method-open" class="text-button">Как устроен расчёт ↗</button></div>
  <section class="workspace">
    <aside class="sidebar"><div class="panel-heading"><h2>Фокус анализа</h2><span class="tiny">01</span></div>
      <form id="search-form"><label for="gid">Найти узел по gid</label><div class="search"><input id="gid" autocomplete="off" placeholder="Полный идентификатор" inputmode="numeric"><button aria-label="Найти узел" title="Найти">↗</button></div></form>
      <div class="filter-row"><label for="role">Роль</label><select id="role"><option value="">Все роли</option>${Object.entries(
        labels,
      )
        .map(([key, label]) => `<option value="${key}">${label}</option>`)
        .join("")}</select></div>
      <div class="filter-row"><label for="cluster">Сообщество</label><select id="cluster"><option value="">Все сообщества</option></select></div>
      <div class="compact-filters"><select id="depth" aria-label="Глубина"><option value="">Все уровни</option>${[0, 1, 2, 3, 4].map((x) => `<option value="${x}">Уровень ${x}</option>`).join("")}</select><label class="checkbox"><input type="checkbox" id="seeds"> Только seed</label></div>
      <button id="reset" class="text-button">Сбросить фильтры</button>
      <div class="panel-heading list-heading"><h2>Приоритет проверки</h2><span class="tag small">TOP 50</span></div><div id="top-list" class="top-list"></div>
    </aside>
    <section class="graph-panel"><div class="graph-toolbar"><div class="tabs"><button id="tab-graph" class="tab active">Карта связей</button><button id="tab-clusters" class="tab">Сообщества</button><button id="tab-analysis" class="tab">Проверки</button></div><div><button id="fit" class="icon-button" title="Вместить граф">⊡</button><button id="full-graph" class="text-button">Весь граф</button></div></div>
      <div class="graph-options"><button id="community-map" class="text-button">Обзор сообществ</button><label for="color-mode">Цвет</label><select id="color-mode"><option value="role">По роли</option><option value="cluster">По сообществу</option></select><button id="graph-png" class="text-button">PNG ↓</button></div><div id="edge-info" class="edge-info" hidden></div><div id="graph-wrapper"><div id="graph" aria-label="Направленный граф транзакций"></div><div id="graph-empty" class="graph-empty" hidden>По этим фильтрам узлов нет</div><div class="graph-caption"><span id="graph-count">Загрузка графа…</span><span>Нажмите на узел, чтобы изучить связи</span></div></div>
      <div id="clusters-view" hidden></div><div id="analysis-view" hidden></div>
      <div id="graph-legend" class="legend">${Object.entries(labels)
        .map(([key, label]) => `<span><i style="background:${colors[key]}"></i>${label}</span>`)
        .join("")}<span class="legend-note">◆ seed · пунктир — граница наблюдения</span></div>
    </section>
    <aside id="detail" class="detail"><div class="empty-card"><span class="empty-symbol">⌘</span><h2>За каждым узлом — факты</h2><p>Выберите узел на карте или в списке приоритетов. Здесь появятся его потоки, роль и основания для проверки.</p><div class="subtle-box">Роль — гипотеза по наблюдаемым данным, а не вывод о виновности.</div></div></aside>
  </section><footer><span>EmbeddedAi · HackAlem 2026</span><span id="limitations">4 уровня · только внутрибанковские переводы · порог 5 000 ₸</span></footer></main>
  <dialog id="upload-dialog"><form id="upload-form"><div class="dialog-title"><h2>Новый набор данных</h2><button type="button" id="upload-close" class="icon-button" aria-label="Закрыть">×</button></div><p>Загрузите три файла организаторов. Исходные ID сохраняются без округления.</p>${["nodes", "edges", "transactions"].map((name) => `<label class="file-label">${name}.parquet<input type="file" name="${name}" accept=".parquet" required></label>`).join("")}<p class="fine-print">До 10 МБ на файл. Результаты доступны по ссылке расчёта до 24 часов или до удаления старых запусков.</p><div id="upload-error" class="alert" hidden></div><button id="analyze-button" class="button primary" type="submit">Рассчитать граф →</button></form></dialog>
  <dialog id="method-dialog"><div class="dialog-title"><h2>Объяснимый расчёт</h2><button id="method-close" class="icon-button" aria-label="Закрыть">×</button></div><div id="method-content"></div></dialog>
  <dialog id="investigation-dialog" class="wide-dialog"><div class="dialog-title"><h2 id="investigation-title">Доказательная карточка</h2><button id="investigation-close" class="icon-button" aria-label="Закрыть">×</button></div><div id="investigation-content"></div></dialog>`;

function alert(message: string) {
  $("alert").textContent = message;
  $("alert").hidden = !message;
}
function roleTag(role: string) {
  return `<span class="role-tag" style="--role-color:${colors[role]}">${escapeHtml(labels[role] ?? role)}</span>`;
}
function onNodeButtons(container: HTMLElement) {
  for (const button of container.querySelectorAll<HTMLElement>("[data-gid]")) {
    button.onclick = () => {
      void selectNode(button.dataset.gid ?? "").catch(showError);
    };
  }
}
function showError(error: unknown) {
  alert(error instanceof Error ? error.message : "Не удалось выполнить действие");
}

async function loadRun(id: string) {
  const sequence = ++runSequence;
  ++detailSequence;
  ++graphSequence;
  const [report, top, clusters] = await Promise.all([
    api<Report>(`/api/runs/${id}`),
    api<TopNode[]>(`/api/runs/${id}/top`),
    api<Cluster[]>(`/api/runs/${id}/clusters`),
  ]);
  if (sequence !== runSequence) return;
  currentReport = report;
  runId = id;
  renderAnalysis(report, id, (gid) => selectNode(gid).catch(showError));
  renderMethod(report);
  localStorage.setItem("money-graph-run", id);
  history.replaceState(null, "", `?run=${id}`);
  currentGid = "";
  $("dataset-badge").textContent = report.synthetic ? "СИНТЕТИЧЕСКИЙ ПРИМЕР" : "ЗАГРУЖЕННЫЙ НАБОР";
  $("dataset-badge").className = `tag ${report.synthetic ? "demo" : "live"}`;
  $("metrics").innerHTML = [
    ["Узлов в сети", number(report.n_nodes), `${number(report.n_seed)} исходных seed`],
    ["Направленных связей", number(report.n_edges), `${number(report.n_transactions)} переводов`],
    ["Сообществ", number(report.n_clusters), `${report.n_components} компонент связности`],
    ["Наблюдаемый оборот", money(report.turnover_kzt), "Сумма переводов, не уникальных денег"],
  ]
    .map(
      ([label, value, hint]) =>
        `<div class="metric"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>`,
    )
    .join("");
  $("period").textContent = `${report.period_from ?? "Нет операций"} — ${report.period_to ?? ""}`;
  $("runtime").textContent =
    `Расчёт ${number(report.runtime_seconds)} с · правила v${report.rules_version}`;
  $("limitations").textContent =
    `${report.n_boundary} узлов на границе · ${report.n_isolates} без рёбер · входящие извне не видны`;
  $("top-list").innerHTML = top
    .map(
      (row) =>
        `<button class="top-item" data-gid="${row.gid}"><span class="rank">${String(row.rank).padStart(2, "0")}</span><span class="top-main"><span class="gid">${row.gid}</span>${roleTag(row.role)}</span><span class="score">${percent(row.priority_score)}</span></button>`,
    )
    .join("");
  onNodeButtons($("top-list"));
  $("cluster").innerHTML =
    '<option value="">Все сообщества</option>' +
    clusters
      .map((c) => `<option value="${c.cluster_id}">#${c.cluster_id} · ${c.n_nodes} узлов</option>`)
      .join("");
  $("clusters-view").innerHTML =
    `<div class="table-scroll"><table><thead><tr><th>Сообщество</th><th>Узлы / seed</th><th>Внутренний оборот</th><th>Гипотеза</th></tr></thead><tbody>${clusters.map((c) => `<tr><td><button class="text-button" data-cluster="${c.cluster_id}">#${c.cluster_id} ↗</button></td><td>${c.n_nodes} / ${c.n_seed}</td><td>${money(c.sum_kzt_internal)}</td><td>${escapeHtml(c.hypothesis)}</td></tr>`).join("")}</tbody></table></div>`;
  for (const button of $("clusters-view").querySelectorAll<HTMLElement>("[data-cluster]")) {
    button.onclick = () => {
      $<HTMLSelectElement>("cluster").value = button.dataset.cluster ?? "";
      resetOtherFilters("cluster");
      switchTab(false);
      void loadGraph().catch(showError);
    };
  }
  alert("");
  resetOtherFilters();
  await loadGraph();
  const first = top[0];
  if (first) await selectNode(first.gid, false);
}

function resetOtherFilters(keep = "") {
  currentGid = "";
  for (const id of ["role", "cluster", "depth"])
    if (id !== keep) $<HTMLSelectElement>(id).value = "";
  $<HTMLInputElement>("seeds").checked = false;
}

async function loadGraph(gid?: string, full = false) {
  const sequence = ++graphSequence;
  communityMode = false;
  updateLegend();
  $("edge-info").hidden = true;
  const params = new URLSearchParams({ limit: full ? "10000" : "350" });
  if (gid) params.set("gid", gid);
  else {
    for (const field of ["role", "cluster", "depth"]) {
      const value = $<HTMLSelectElement>(field).value;
      if (value) params.set(field, value);
    }
    if ($<HTMLInputElement>("seeds").checked) params.set("seeds", "true");
  }
  $("graph-count").textContent = "Загрузка связей…";
  const result = await api<GraphResponse>(`/api/runs/${runId}/graph?${params}`);
  if (sequence !== graphSequence) return;
  $("graph-count").textContent =
    `${number(result.shown)} из ${number(result.matched)} подходящих · всего ${number(result.total)}`;
  $("graph-empty").hidden = result.shown > 0;
  cy?.destroy();
  const elements = [
    ...result.nodes.map((n) => ({
      data: {
        id: n.gid,
        ...n,
        color:
          $<HTMLSelectElement>("color-mode").value === "cluster"
            ? communityColor(n.cluster_id)
            : colors[n.role],
        size: 13 + n.priority_score * 20,
        label: n.gid,
      },
    })),
    ...result.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.src, target: e.dst, ...e } })),
  ];
  cy = cytoscape({
    container: $("graph"),
    elements,
    minZoom: 0.08,
    maxZoom: 4,
    wheelSensitivity: 0.25,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(color)",
          width: "data(size)",
          height: "data(size)",
          "border-width": 2,
          "border-color": "#ffffff",
          "font-size": 10,
          "text-valign": "bottom",
          "text-margin-y": 7,
          color: "#22394d",
        },
      },
      { selector: "node[?is_seed]", style: { shape: "diamond", "border-color": "#19364b" } },
      {
        selector: "node[?boundary_censored]",
        style: { "border-style": "dashed", "border-color": "#687f91" },
      },
      {
        selector: "edge",
        style: {
          width: 1.3,
          "line-color": "#b9cbd6",
          "target-arrow-color": "#a4b9c7",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          opacity: 0.55,
          "arrow-scale": 0.7,
        },
      },
      {
        selector: "node:selected",
        style: {
          label: "data(label)",
          "border-color": "#102f45",
          "border-width": 4,
          "z-index": 100,
        },
      },
      {
        selector: "edge:selected",
        style: { "line-color": "#087f76", "target-arrow-color": "#087f76", width: 3 },
      },
    ],
    layout: {
      name: result.shown > 500 ? "grid" : "cose",
      animate: false,
      randomize: false,
      padding: 35,
      nodeRepulsion: () => 16000,
      idealEdgeLength: () => 75,
    } as cytoscape.LayoutOptions,
  });
  cy.on("tap", "edge", (event) => {
    const e = event.target.data();
    $("edge-info").textContent = `${e.src} → ${e.dst} · ${money(e.sum_kzt)} · ${e.n_tx} переводов`;
    $("edge-info").hidden = false;
  });
  cy.on("tap", "node", (event) => {
    void selectNode(String(event.target.id()), false).catch(showError);
  });
  if (currentGid) cy.getElementById(currentGid).select();
}

function links(rows: Edge[], incoming: boolean) {
  if (!rows.length) return '<p class="muted">В выборке не наблюдаются</p>';
  return `<div class="neighbors">${rows
    .sort((a, b) => b.sum_kzt - a.sum_kzt)
    .map(
      (edge) =>
        `<button data-gid="${incoming ? edge.src : edge.dst}"><span class="gid">${incoming ? edge.src : edge.dst}</span><small>${money(edge.sum_kzt)} · ${edge.n_tx} пер.</small></button>`,
    )
    .join("")}</div>`;
}

async function selectNode(gid: string, focusGraph = true) {
  if (!gid) return;
  const sequence = ++detailSequence;
  const node = await api<NodeDetail>(`/api/runs/${runId}/nodes/${encodeURIComponent(gid)}`);
  if (sequence !== detailSequence) return;
  currentGid = gid;
  alert("");
  switchTab(false);
  $("detail").innerHTML =
    `<div class="panel-heading"><h2>Карточка узла</h2><span class="tag small">#${node.rank}</span></div><p class="node-id gid">${node.gid}</p>${roleTag(node.role)}<div class="node-tags"><span>Уровень ${node.depth}</span><button id="node-cluster" class="text-button">Сообщество #${node.cluster_id} ↗</button>${node.is_seed ? '<span class="tag small">SEED</span>' : ""}</div><div class="score-grid"><div><strong>${percent(node.priority_score)}</strong><span>Приоритет проверки</span></div><div><strong>${percent(node.role_score)}</strong><span>Поддержка правила</span></div></div><p class="fine-print">Место при изменении весов ±20%: ${node.rank_range[0]}–${node.rank_range[1]}. Не доверительный интервал.</p><h3>Почему эта роль</h3><p class="evidence">${escapeHtml(node.evidence)}</p><dl class="node-metrics"><div><dt>Наблюдаемый вход</dt><dd>${money(node.in_kzt)}</dd></div><div><dt>Наблюдаемый выход</dt><dd>${money(node.out_kzt)}</dd></div><div><dt>Плательщики / получатели</dt><dd>${node.in_deg} / ${node.out_deg}</dd></div><div><dt>Переводы: вход / выход</dt><dd>${node.in_tx} / ${node.out_tx}</dd></div><div><dt>Seed-предков</dt><dd>${node.seed_reach}</dd></div></dl><details><summary>Вклад в приоритет и правила</summary>${Object.entries(
      node.priority_parts,
    )
      .map(
        ([key, value]) =>
          `<div class="contribution"><span>${escapeHtml(metricLabels[key] ?? key)}</span><meter min="0" max="0.25" value="${value}"></meter><span>${(value * 100).toFixed(1)} п.п.</span></div>`,
      )
      .join(
        "",
      )}<p class="fine-print">Совпали: ${node.matched_rules.map((r) => escapeHtml(labels[r])).join(", ") || "конкретная роль не установлена"}. Поддержка не является вероятностью виновности.</p></details>${node.warnings.length ? `<div class="warning-box"><h3>Границы наблюдения</h3><ul>${node.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>` : ""}<details open><summary>Что проверить дальше</summary><ul class="check-list">${node.next_checks.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></details><details><summary>Временные признаки · ${node.temporal.active_days} дней</summary>${timeline(node)}</details><button id="investigate-node" class="button primary full-width">Пути, циклы и хронология ↗</button><button id="download-dossier" class="text-button">Скачать справку по узлу ↓</button><button id="focus-neighbors" class="button secondary">Показать окружение узла ↗</button><details open><summary>Входящие связи · ${node.in_deg}</summary>${links(node.incoming, true)}</details><details><summary>Исходящие связи · ${node.out_deg}</summary>${links(node.outgoing, false)}</details>`;
  onNodeButtons($("detail"));
  $("investigate-node").onclick = () => {
    void openInvestigation(runId, node, (gid) => selectNode(gid).catch(showError));
  };
  $("download-dossier").onclick = () => downloadDossier(node, currentReport);
  $("focus-neighbors").onclick = () => {
    void loadGraph(gid).catch(showError);
  };
  $("node-cluster").onclick = () => {
    resetOtherFilters();
    $<HTMLSelectElement>("cluster").value = String(node.cluster_id);
    void loadGraph().catch(showError);
  };
  if (focusGraph || communityMode) await loadGraph(gid);
  cy?.elements().unselect();
  cy?.getElementById(gid).select();
  for (const button of $("top-list").querySelectorAll("button"))
    button.classList.toggle("selected", (button as HTMLElement).dataset.gid === gid);
}
function switchTab(clusters: boolean) {
  $("analysis-view").hidden = true;
  $("tab-analysis").classList.remove("active");
  $("graph-wrapper").hidden = clusters;
  $("clusters-view").hidden = !clusters;
  $("tab-graph").classList.toggle("active", !clusters);
  $("tab-clusters").classList.toggle("active", clusters);
  if (!clusters) cy?.resize();
}
$("investigation-close").onclick = () => $<HTMLDialogElement>("investigation-dialog").close();
$("tab-analysis").onclick = () => {
  $("graph-wrapper").hidden = true;
  $("clusters-view").hidden = true;
  $("analysis-view").hidden = false;
  $("tab-graph").classList.remove("active");
  $("tab-clusters").classList.remove("active");
  $("tab-analysis").classList.add("active");
};
$("tab-graph").onclick = () => switchTab(false);
$("tab-clusters").onclick = () => switchTab(true);
$("community-map").onclick = () => {
  void showCommunityMap().catch(showError);
};
$("color-mode").onchange = () => {
  updateLegend();
  if (!communityMode)
    cy?.nodes().forEach((node) => {
      node.data(
        "color",
        $<HTMLSelectElement>("color-mode").value === "cluster"
          ? communityColor(node.data("cluster_id"))
          : colors[node.data("role")],
      );
    });
};
$("graph-png").onclick = () => {
  if (cy) {
    const a = document.createElement("a");
    a.download = "money-graph.png";
    a.href = cy.png({ full: true, scale: 2, bg: "#ffffff", maxWidth: 4000, maxHeight: 4000 });
    a.click();
  }
};
$("fit").onclick = () => cy?.fit(undefined, 35);
$("full-graph").onclick = () => {
  resetOtherFilters();
  switchTab(false);
  void loadGraph(undefined, true).catch(showError);
};
$("reset").onclick = () => {
  resetOtherFilters();
  $<HTMLInputElement>("gid").value = "";
  switchTab(false);
  void loadGraph().catch(showError);
};
$("search-form").onsubmit = (event) => {
  event.preventDefault();
  void selectNode($<HTMLInputElement>("gid").value.trim()).catch(showError);
};
for (const id of ["role", "cluster", "depth", "seeds"])
  $(id).onchange = () => {
    currentGid = "";
    switchTab(false);
    void loadGraph().catch(showError);
  };
$("export-select").onchange = () => {
  const select = $<HTMLSelectElement>("export-select");
  if (select.value && runId) {
    window.location.href = `/api/runs/${runId}/exports/${select.value}`;
    select.value = "";
  }
};
$("upload-open").onclick = () => $<HTMLDialogElement>("upload-dialog").showModal();
$("upload-close").onclick = () => $<HTMLDialogElement>("upload-dialog").close();
$("method-open").onclick = () => $<HTMLDialogElement>("method-dialog").showModal();
$("method-close").onclick = () => $<HTMLDialogElement>("method-dialog").close();
$("upload-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = $<HTMLButtonElement>("analyze-button");
  button.disabled = true;
  button.textContent = "Проверяем файлы и рассчитываем…";
  $("upload-error").hidden = true;
  try {
    const form = $<HTMLFormElement>("upload-form");
    const result = await api<{ run_id: string }>("/api/analyze", {
      method: "POST",
      body: new FormData(form),
    });
    await loadRun(result.run_id);
    $<HTMLDialogElement>("upload-dialog").close();
    form.reset();
  } catch (error) {
    $("upload-error").textContent = error instanceof Error ? error.message : "Ошибка загрузки";
    $("upload-error").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Рассчитать граф →";
  }
};

async function init() {
  const bootstrap = await api<{ run_id: string }>("/api/bootstrap");
  const saved =
    new URLSearchParams(location.search).get("run") ?? localStorage.getItem("money-graph-run");
  try {
    await loadRun(saved ?? bootstrap.run_id);
  } catch (error) {
    if (saved && saved !== bootstrap.run_id) {
      await loadRun(bootstrap.run_id);
      alert("Сохранённый расчёт недоступен. Открыт начальный набор; можно загрузить файлы заново.");
    } else throw error;
  }
}
void init().catch(showError);

function renderMethod(report: Report) {
  const r = report.rules;
  $("method-content").innerHTML =
    `<p>Правила v${escapeHtml(report.rules_version)}. Приоритет: ${Object.entries(
      r.priority_weights,
    )
      .map(([k, w]) => `${percent(w)} ${metricLabels[k]}`)
      .join(
        " + ",
      )}. Используются процентили положительных значений. Объём = max(вход, выход), изоляты получают приоритет 0.</p><p>Связующий узел: ≥${r.coordinator_min_seeds} seed-предков, ≥${r.coordinator_min_in} плательщиков, ≥${r.coordinator_min_out} получателей, процентиль посредничества ≥${r.coordinator_betweenness_percentile}. Распределитель: ≥${r.distributor_min_out} получателей. Объёмный транзит: выход/вход ${r.transit_ratio_min}–${r.transit_ratio_max}. Консолидатор: ≥${r.consolidator_min_in} плательщиков и выход/вход ≤${r.consolidator_ratio_max}. Конечный: положительный вход без наблюдаемого выхода.</p><p>Seed и граница depth=4 не получают роли по отношению потоков или отсутствию выхода. При пересечении правил порядок: связующий → распределитель → транзит → консолидатор → конечный → периферия. Поддержка роли — эвристика, не вероятность виновности.</p><p>Louvain: суммы обоих направлений, resolution=${r.louvain_resolution}, seed=${r.random_seed}. Изоляты сохранены. Номер сообщества не доказывает существование группы.</p><p>Сопоставление поступлений и списаний: FIFO с окном 1–2 календарных дня. Поступления одного дня не сопоставляются с его списаниями. Без времени суток и остатков нельзя доказать происхождение денег.</p>`;
}
async function showCommunityMap() {
  const sequence = ++graphSequence;
  const result = await api<CommunityGraph>(`/api/runs/${runId}/community-graph`);
  if (sequence !== graphSequence) return;
  switchTab(false);
  communityMode = true;
  updateLegend();
  $("edge-info").hidden = true;
  $("graph-empty").hidden = true;
  $("graph-count").textContent = `${result.nodes.length} сообществ · нажмите, чтобы раскрыть узлы`;
  cy?.destroy();
  cy = cytoscape({
    container: $("graph"),
    elements: [
      ...result.nodes.map((c) => ({
        data: {
          id: `c${c.cluster_id}`,
          cid: c.cluster_id,
          label: `#${c.cluster_id} · ${c.n_nodes}`,
          size: 18 + Math.sqrt(c.n_nodes) * 3,
          color: communityColor(c.cluster_id),
        },
      })),
      ...result.edges.map((e, i) => ({
        data: { id: `ce${i}`, source: `c${e.src}`, target: `c${e.dst}`, ...e },
      })),
    ],
    style: [
      {
        selector: "node",
        style: {
          width: "data(size)",
          height: "data(size)",
          "background-color": "data(color)",
          label: "data(label)",
          "font-size": 10,
          color: "#20384d",
          "text-valign": "bottom",
          "text-margin-y": 5,
        },
      },
      {
        selector: "edge",
        style: {
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "line-color": "#a4b9c7",
          "target-arrow-color": "#a4b9c7",
          opacity: 0.6,
        },
      },
    ],
    layout: { name: "cose", animate: false, randomize: false, padding: 35 },
  });
  cy.on("tap", "node", (event) => {
    resetOtherFilters();
    $<HTMLSelectElement>("cluster").value = String(event.target.data("cid"));
    void loadGraph().catch(showError);
  });
  cy.on("tap", "edge", (event) => {
    const e = event.target.data();
    $("edge-info").textContent =
      `Сообщество #${e.src} → #${e.dst}: ${money(e.sum_kzt)} · ${e.n_edges} связей`;
    $("edge-info").hidden = false;
  });
}

function updateLegend() {
  $("graph-legend").innerHTML = communityMode
    ? "<span>Размер — число участников · цвет — сообщество · стрелка — агрегированный поток</span>"
    : $<HTMLSelectElement>("color-mode").value === "cluster"
      ? "<span>Цвет — сообщество · размер — приоритет · ◆ seed · пунктир — граница</span>"
      : `${Object.entries(labels)
          .map(([key, label]) => `<span><i style="background:${colors[key]}"></i>${label}</span>`)
          .join("")}<span class="legend-note">◆ seed · пунктир — граница наблюдения</span>`;
}
