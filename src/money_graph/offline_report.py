"""Self-contained, escaped HTML evidence from the existing deterministic result."""

import base64
import hashlib
import html
import json
from typing import Any

from .roles import LABELS

STYLE = """
:root{color-scheme:light dark;--bg:#f2f5fa;--panel:#fff;--ink:#18243a;--muted:#526079;--line:#d4ddea;--accent:#174ba0;--notice:#fff3d1;--focus:#cce3ff}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#101721;--panel:#182230;--ink:#edf3ff;--muted:#b0bfd4;--line:#38465a;--accent:#91bfff;--notice:#382f1c;--focus:#28466c}}
:root[data-theme=dark]{color-scheme:dark;--bg:#101721;--panel:#182230;--ink:#edf3ff;--muted:#b0bfd4;--line:#38465a;--accent:#91bfff;--notice:#382f1c;--focus:#28466c}
:root[data-theme=light]{color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif}
main{max-width:1440px;padding:32px 24px 64px;margin:auto}header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}
h1{font-size:clamp(26px,4vw,40px);line-height:1.2;margin:0 0 12px}h2{font-size:23px;margin-top:0}h3{font-size:17px}
p{margin:8px 0 16px}.muted,small{color:var(--muted)}.eyebrow{font-size:12px;font-weight:700;letter-spacing:.13em;color:var(--accent)}
a{color:var(--accent)}a:hover{text-decoration-thickness:2px}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
nav{display:flex;gap:10px 20px;flex-wrap:wrap;margin:24px 0}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:24px;margin:20px 0}
.notice{background:var(--notice)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.stat{padding:16px;border:1px solid var(--line);border-radius:10px}.stat strong{display:block;font-size:25px}
.roles{display:flex;flex-wrap:wrap;gap:10px}.badge{border:1px solid var(--line);border-radius:30px;padding:5px 12px;white-space:nowrap}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;font-size:14px;text-align:left}
th,td{padding:11px 13px;border-bottom:1px solid var(--line);vertical-align:top}th{background:var(--bg);position:sticky;top:0;z-index:1;white-space:nowrap}tbody tr:last-child td{border-bottom:0}
td.gid{font-variant-numeric:tabular-nums;white-space:nowrap;font-family:ui-monospace,monospace;font-size:12px}td.evidence{min-width:280px;max-width:600px}
td.number{white-space:nowrap;font-variant-numeric:tabular-nums}.node-table{max-height:70vh}tr:target{background:var(--focus);outline:2px solid var(--accent);outline-offset:-2px}
.controls{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0;align-items:end}.controls label{flex:1;min-width:180px}label{display:block;font-size:13px;font-weight:600}
input,select,button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:9px 12px;max-width:100%}input,select{display:block;width:100%;margin-top:5px}button{cursor:pointer}
code,pre{font-family:ui-monospace,monospace;font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:16px;border-radius:8px}code{overflow-wrap:anywhere}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.empty{padding:12px;color:var(--muted)}
@media(max-width:700px){main{padding:20px 12px}header{display:block}.panel{padding:16px}.theme-control{max-width:220px;margin-top:16px}th,td{padding:9px}nav{gap:8px 14px}}
@media print{body{background:#fff;color:#111}main{max-width:none;padding:0}.controls,.theme-control,nav{display:none}.panel{break-inside:avoid;border-color:#ccc}.table-wrap,.node-table{overflow:visible;max-height:none}table{font-size:9px}th{position:static}a{color:#111}tr{break-inside:avoid}}
"""

SCRIPT = """
(() => {
  'use strict';
  const theme = document.getElementById('theme');
  const setTheme = value => {
    if (value === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.dataset.theme = value;
    theme.value = value;
  };
  try {
    const stored = localStorage.getItem('money-graph-report-theme');
    if (['light', 'dark', 'system'].includes(stored)) setTheme(stored);
  } catch (_) { /* file:// storage may be unavailable; the report still works. */ }
  theme.addEventListener('change', () => {
    setTheme(theme.value);
    try { localStorage.setItem('money-graph-report-theme', theme.value); } catch (_) {}
  });
  const body = document.getElementById('node-rows');
  const rows = Array.from(body.rows);
  const search = document.getElementById('node-search');
  const role = document.getElementById('node-role');
  const sort = document.getElementById('node-sort');
  const status = document.getElementById('node-count');
  const texts = new Map(rows.map(row => [row, row.textContent.toLocaleLowerCase('ru')]));
  const filter = () => {
    const query = search.value.trim().toLocaleLowerCase('ru');
    let count = 0;
    for (const row of rows) {
      row.hidden = !texts.get(row).includes(query) || (role.value !== '' && row.dataset.role !== role.value);
      if (!row.hidden) count += 1;
    }
    status.textContent = `Показано ${count} из ${rows.length} узлов`;
    document.getElementById('node-empty').hidden = count !== 0;
  };
  search.addEventListener('input', filter);
  role.addEventListener('change', filter);
  sort.addEventListener('change', () => {
    const key = sort.value;
    const ordered = rows.slice().sort((a, b) => {
      const difference = key === 'role'
        ? a.dataset.roleLabel.localeCompare(b.dataset.roleLabel, 'ru')
        : key === 'rank' ? Number(a.dataset.rank) - Number(b.dataset.rank)
        : Number(b.dataset[key]) - Number(a.dataset[key]);
      return difference || Number(a.dataset.rank) - Number(b.dataset.rank);
    });
    const fragment = document.createDocumentFragment();
    for (const row of ordered) fragment.append(row);
    body.append(fragment);
  });
  document.getElementById('node-reset').addEventListener('click', () => {
    search.value = ''; role.value = ''; filter();
  });
  const revealAnchor = () => {
    const anchor = document.getElementById(location.hash.slice(1));
    if (anchor && anchor.parentElement === body) {
      if (anchor.hidden) { search.value = ''; role.value = ''; filter(); }
      anchor.scrollIntoView({block: 'center'});
    }
  };
  window.addEventListener('hashchange', revealAnchor);
  revealAnchor();
})();
"""


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float | int | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}".replace(",", " ")


def _anchor(gid: Any) -> str:
    # Stable opaque anchors retain arbitrary int64 IDs as text, never JS numbers.
    return "node-" + hashlib.sha256(str(gid).encode("utf-8")).hexdigest()[:24]


def _node_link(gid: Any) -> str:
    return f'<a href="#{_anchor(gid)}">{_text(gid)}</a>'


def _hash_policy(source: str) -> str:
    digest = base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii")
    return f"'sha256-{digest}'"


def _role_criteria(cfg: dict[str, Any]) -> str:
    descriptions = {
        "coordinator": f"Seed-предков ≥ {cfg['coordinator_min_seeds']}; плательщиков ≥ {cfg['coordinator_min_in']}; получателей ≥ {cfg['coordinator_min_out']}; процентиль посредничества ≥ {cfg['coordinator_betweenness_percentile']:.0%}.",
        "distributor": f"Получателей и исходящих операций ≥ {cfg['distributor_min_out']}.",
        "transit": f"Есть исходящая связь; выход/вход ∈ [{cfg['transit_ratio_min']:.0%}; {cfg['transit_ratio_max']:.0%}]. Не seed и не граница.",
        "consolidator": f"Плательщиков ≥ {cfg['consolidator_min_in']}; выход/вход ≤ {cfg['consolidator_ratio_max']:.0%}. Не seed и не граница.",
        "terminal": "Наблюдается входящий объём, исходящих связей нет. Не seed и не граница. Конечный только в данной выборке.",
        "peripheral": "Ни одно из предыдущих правил не сработало либо наблюдений недостаточно.",
    }
    return "".join(
        f"<tr><td>{_text(LABELS[role])}</td><td>{_text(description)}</td></tr>"
        for role, description in descriptions.items()
    )


def _node_row(node: dict[str, Any]) -> str:
    role = node["role"]
    label = LABELS.get(role, role)
    tags = []
    if node["is_seed"]:
        tags.append("seed")
    if node["boundary_censored"]:
        tags.append("граница depth=4")
    if node["isolated"]:
        tags.append("изолят")
    note = f"<br><small>{_text(' · '.join(tags))}</small>" if tags else ""
    warnings = " ".join(node.get("warnings", []))
    evidence = _text(node["evidence"])
    if warnings:
        evidence += f"<details><summary>Ограничения</summary>{_text(warnings)}</details>"
    return (
        f'<tr id="{_anchor(node["gid"])}" data-role="{_text(role)}" '
        f'data-role-label="{_text(label)}" data-rank="{_text(node["rank"])}" '
        f'data-inflow="{_text(node["in_kzt"])}" data-outflow="{_text(node["out_kzt"])}">'
        f'<td class="number">{_number(node["rank"])}</td>'
        f'<td class="gid">{_text(node["gid"])}{note}</td>'
        f'<td>{_text(label)}</td><td class="number">{_number(node["priority_score"] * 100, 2)}</td>'
        f'<td class="number">{_number(node["role_score"] * 100, 2)}</td>'
        f'<td class="number">{_number(node["in_kzt"], 2)}</td>'
        f'<td class="number">{_number(node["out_kzt"], 2)}</td>'
        f'<td class="number">{_number(node["in_deg"])} / {_number(node["out_deg"])}</td>'
        f'<td class="number">{_text(node["cluster_id"])}</td>'
        f'<td class="evidence">{evidence}</td></tr>'
    )


def render_report(result: dict[str, Any]) -> str:
    """Render a validated result without network, new analysis or mutable state."""
    report = result["report"]
    rules = report["rules"]
    nodes = sorted(result["nodes"], key=lambda row: row["rank"])
    provenance = (
        '<p class="badge">Синтетическая демонстрация: вымышленные узлы и переводы</p>'
        if report.get("synthetic") is True
        else ""
    )
    policy = (
        f"default-src 'none'; base-uri 'none'; form-action 'none'; "
        f"style-src {_hash_policy(STYLE)}; script-src {_hash_policy(SCRIPT)}"
    )
    stats = (
        ("Узлов", report["n_nodes"]),
        ("Направленных связей", report["n_edges"]),
        ("Транзакций", report["n_transactions"]),
        ("Сообществ", report["n_clusters"]),
        ("Seed-клиентов", report["n_seed"]),
        ("На границе наблюдения", report["n_boundary"]),
    )
    stat_html = "".join(
        f'<div class="stat"><strong>{_number(value)}</strong>{_text(label)}</div>'
        for label, value in stats
    )
    role_html = "".join(
        f'<span class="badge">{_text(label)}: {_number(report["role_counts"].get(role, 0))}</span>'
        for role, label in LABELS.items()
    )
    warnings = "".join(f"<li>{_text(value)}</li>" for value in report["warnings"])
    top_html = "".join(
        f'<tr><td>{_number(row["rank"])}</td><td class="gid">{_node_link(row["gid"])}</td>'
        f"<td>{_text(LABELS.get(row['role'], row['role']))}</td>"
        f'<td class="number">{_number(row["priority_score"] * 100, 2)}</td>'
        f'<td class="evidence">{_text(row["why"])}</td></tr>'
        for row in result["top"]
    )
    cluster_html = "".join(
        f"<tr><td>{_text(cluster['cluster_id'])}</td><td>{_number(cluster['n_nodes'])}</td>"
        f"<td>{_number(cluster['n_seed'])}</td>"
        f'<td class="number">{_number(cluster["sum_kzt_internal"], 2)}</td>'
        f'<td class="number">{_number(cluster["incoming_kzt"], 2)}</td>'
        f'<td class="number">{_number(cluster["outgoing_kzt"], 2)}</td>'
        f"<td>{', '.join(_node_link(gid) for gid in cluster['top_gids'])}</td>"
        f'<td class="evidence">{_text(cluster["hypothesis"])}</td></tr>'
        for cluster in result["clusters"]
    )
    role_options = "".join(
        f'<option value="{_text(role)}">{_text(label)}</option>' for role, label in LABELS.items()
    )
    node_html = "".join(_node_row(node) for node in nodes)
    sensitivity = report["sensitivity"]
    sensitivity_html = "".join(
        f"<tr><td>{_text(scenario['metric'])}</td>"
        f"<td>{_number((scenario['multiplier'] - 1) * 100)}%</td>"
        f"<td>{_number(scenario['top_overlap'] * 100, 1)}%</td></tr>"
        for scenario in sensitivity["scenarios"]
    )
    hashes = "".join(
        f"<tr><td>{_text(name)}.parquet</td><td><code>{_text(digest)}</code></td></tr>"
        for name, digest in report["input_sha256"].items()
    )
    rules_json = json.dumps(rules, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    rules_digest = hashlib.sha256(rules_json.encode("utf-8")).hexdigest()
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_text(policy)}">
<title>Граф денег — автономный отчёт</title><style>{STYLE}</style></head>
<body><main>
<header><div><p class="eyebrow">ГРАФ ДЕНЕГ · ОТЧЁТ ПО РЕЗУЛЬТАТУ РАСЧЁТА</p>
<h1>От переводов к проверяемым гипотезам</h1>{provenance}
<p class="muted">Период: {_text(report["period_from"] or "нет транзакций")} — {_text(report["period_to"] or "нет транзакций")}.
Правила {_text(report["rules_version"])} · расчёт {_number(report["runtime_seconds"], 3)} с.</p></div>
<label class="theme-control">Цветовая тема<select id="theme"><option value="system">Системная</option><option value="light">Светлая</option><option value="dark">Тёмная</option></select></label></header>
<nav aria-label="Разделы отчёта"><a href="#overview">Обзор</a><a href="#top">TOP-{len(result["top"])}</a><a href="#nodes">Все узлы</a><a href="#clusters">Сообщества</a><a href="#method">Методика</a><a href="#reproducibility">Воспроизводимость</a></nav>
<section class="panel notice" aria-label="Границы выводов"><strong>Гипотезы для аналитика, не обвинения.</strong><ul>{warnings}</ul>
<p>role_score — поддержка правила, priority_score — относительный приоритет проверки. Ни один показатель не является вероятностью преступления. Объёмный транзит не доказывает движение тех же денег.</p></section>
<section id="overview" class="panel"><h2>Наблюдаемый граф</h2><div class="stats">{stat_html}</div>
<p>Оборот рёбер: <strong>{_number(report["turnover_kzt"], 2)} KZT</strong>.
Это сумма наблюдаемых переводов; повторное прохождение одних средств через цепочку учитывается повторно.
Изолированных узлов: {_number(report["n_isolates"])}.</p><div class="roles">{role_html}</div></section>
<section id="top" class="panel"><h2>Первые {len(result["top"])} узлов для проверки</h2>
<p class="muted">Приоритет убывает; при равенстве используется точный gid. Ссылка открывает строку узла в полном списке.</p>
<div class="table-wrap"><table><thead><tr><th>Место</th><th>gid</th><th>Роль</th><th>Приоритет / 100</th><th>Почему проверить</th></tr></thead><tbody>{top_html}</tbody></table></div></section>
<section id="nodes" class="panel"><h2>Все узлы и основания ролей</h2>
<p class="muted">Фильтры меняют только видимый список; исходные оценки и CSV сохраняются. Все идентификаторы передаются как строки.</p>
<div class="controls"><label>Поиск по gid или объяснению<input id="node-search" type="search" placeholder="gid, граница, плательщиков…"></label>
<label>Роль<select id="node-role"><option value="">Все роли</option>{role_options}</select></label>
<label>Сортировка<select id="node-sort"><option value="rank">Приоритет ↓</option><option value="inflow">Входящий объём ↓</option><option value="outflow">Исходящий объём ↓</option><option value="role">Роль А–Я</option></select></label>
<button id="node-reset" type="button">Сбросить фильтры</button></div>
<p id="node-count" role="status">Показано {len(nodes)} из {len(nodes)} узлов</p>
<noscript><p>JavaScript отключён: полный отчёт доступен, поиск и сортировка недоступны. Используйте поиск браузера.</p></noscript>
<div class="table-wrap node-table"><table><thead><tr><th>Место</th><th>gid / ограничения</th><th>Роль</th><th>Приоритет / 100</th><th>Поддержка / 100</th><th>Вход, KZT</th><th>Выход, KZT</th><th>Вход / выход связей</th><th>Сообщество</th><th>Основание и ограничения</th></tr></thead><tbody id="node-rows">{node_html}</tbody></table></div>
<p id="node-empty" class="empty" hidden>Узлы не найдены. Измените поиск или роль.</p></section>
<section id="clusters" class="panel"><h2>Сообщества и наблюдаемые потоки</h2>
<p class="muted">Louvain на неориентированной взвешенной проекции; встречные суммы складываются. Сообщество не доказывает принадлежность к одной организации. Направление сохранено для расчёта внешних потоков.</p>
<div class="table-wrap"><table><thead><tr><th>ID</th><th>Узлов</th><th>Seed</th><th>Внутри, KZT</th><th>Внешний вход, KZT</th><th>Внешний выход, KZT</th><th>Приоритетные узлы</th><th>Гипотеза</th></tr></thead><tbody>{cluster_html}</tbody></table></div></section>
<section id="method" class="panel"><h2>Как получен результат</h2>
<p>Правила проверяются в указанном порядке; основная роль — первое сработавшее правило. Периферия назначается при отсутствии совпадений. Приоритет отделён от роли и ранжирует узлы по наблюдаемым связям и объёмам.</p>
<div class="table-wrap"><table><thead><tr><th>Роль</th><th>Формальное условие</th></tr></thead><tbody>{_role_criteria(rules)}</tbody></table></div>
<p>У seed неполон вход, у границы depth=4 неполон выход: поддержка применимых правил умножается на {_number(rules["incomplete_support_multiplier"], 2)}. Самопереводы входят в общий оборот, но исключены из метрик ролей и потоков между контрагентами.</p>
<p>Посредничество: {_text(report["betweenness_method"])}, опорных вершин {_number(report["betweenness_pivots"])}; Louvain resolution={_number(rules["louvain_resolution"], 2)}, random_seed={_text(rules["random_seed"])}.</p>
<h3>Чувствительность приоритета к весам</h3>
<p>Минимальное сохранение TOP-{_number(sensitivity["top_n"])}: {_number(sensitivity["minimum_top_overlap"] * 100, 1)}%. {_text(sensitivity["caveat"])}</p>
<details><summary>Все сценарии изменения весов</summary><div class="table-wrap"><table><thead><tr><th>Компонента</th><th>Изменение веса</th><th>Сохранение TOP</th></tr></thead><tbody>{sensitivity_html}</tbody></table></div></details></section>
<section id="reproducibility" class="panel"><h2>Файлы и воспроизводимость</h2>
<p>Отчёт создан тем же пайплайном, что и обязательные CSV. Для просмотра достаточно этого HTML-файла: сервер, Node.js, API-ключ и интернет не требуются. JavaScript используется только для локальных фильтров и темы. Для скачивания CSV сохраните файлы рядом с отчётом.</p>
<p><a href="nodes_roles.csv" download>nodes_roles.csv</a> · <a href="clusters.csv" download>clusters.csv</a> · <a href="top_nodes.csv" download>top_nodes.csv</a> · <a href="run_report.json" download>run_report.json</a></p>
<h3>SHA-256 исходных файлов</h3><div class="table-wrap"><table><thead><tr><th>Файл</th><th>SHA-256</th></tr></thead><tbody>{hashes}</tbody></table></div>
<details><summary>Полная конфигурация правил</summary><p>SHA-256 JSON ниже (UTF-8, sort_keys=True, indent=2, без завершающего переноса): <code>{rules_digest}</code></p><pre>{_text(rules_json)}</pre></details>
<p class="muted">Совпадение хешей подтверждает одинаковые входные байты, но не достоверность первоисточника. Ограничения выборки сохраняются для любых вычисленных оценок.</p></section>
</main><script>{SCRIPT}</script></body></html>
"""
