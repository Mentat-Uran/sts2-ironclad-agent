const $ = (id) => document.getElementById(id);
const ui = { state: null, status: null, events: [], busy: false };

function esc(value) {
  return String(value ?? "—").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function number(value, fallback = "—") {
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n).toLocaleString("zh-CN") : fallback;
}

function setStatusCard(id, status) {
  const card = $(id);
  card.classList.remove("status-ok", "status-warn", "status-error");
  if (["ok", "ready"].includes(status)) card.classList.add("status-ok");
  else if (["key_missing", "model_not_listed", "checking", "unknown"].includes(status)) card.classList.add("status-warn");
  else card.classList.add("status-error");
}

function renderStatus(data) {
  ui.status = data;
  const game = data.game || {};
  const kev = data.kev || {};
  const luna = data.luna || {};
  setStatusCard("game-status-card", game.status);
  setStatusCard("kev-status-card", kev.status);
  setStatusCard("luna-status-card", luna.status);
  $("game-status").textContent = game.status === "ok" ? "在线" : game.status === "unsupported_bridge" ? "Mod 需更新" : "离线";
  $("game-detail").textContent = game.status === "unsupported_bridge"
    ? `${game.bridge_message || "旧版 STS2 MCP Mod"} · 需要被动状态 schema 2`
    : `${game.state_type || "—"} · ${game.version || "版本未知"}${game.version_compatible === false ? " · 版本不匹配" : ""}`;
  $("kev-status").textContent = kev.status === "ok" ? "在线" : kev.status;
  $("kev-detail").textContent = `${kev.model || "kev-latest"} · ${kev.device || "CUDA 未确认"}`;
  $("luna-status").textContent = luna.status === "ok" && luna.model_listed ? "在线" : luna.status === "ok" ? "已认证" : luna.status;
  $("luna-detail").textContent = luna.key_configured ? `${luna.model || "gpt-6-luna"} · key 已注入进程` : "gpt-6-luna · key 未配置";
  setStatusCard("action-mode-card", data.game_actions_enabled ? "ok" : "key_missing");
  $("action-mode").textContent = data.game_actions_enabled ? "当前进程 LIVE" : "当前进程 DRY-RUN";
  $("gate-mark").textContent = data.game_actions_enabled ? "ON" : "OFF";
  const runGate = data.latest_run_actions_enabled;
  const runGateText = runGate == null
    ? "本局日志尚无开关记录"
    : `本局日志初始开关：${runGate ? "LIVE" : "DRY-RUN"}`;
  $("action-mode-detail").textContent = `仅显示配置，不代表游戏已连接动作端 · ${runGateText}`;
}

function renderResources(player = {}) {
  const hp = Number(player.hp || 0), max = Number(player.max_hp || 0);
  const block = Number(player.block || 0), energy = Number(player.energy || 0), maxEnergy = Number(player.max_energy || 0);
  const gold = Number(player.gold || 0);
  const values = [
    ["hp-resource", hp, max, max ? Math.min(100, hp / max * 100) : 0],
    ["block-resource", block, null, block ? Math.min(100, block * 4) : 0],
    ["energy-resource", energy, maxEnergy, maxEnergy ? Math.min(100, energy / maxEnergy * 100) : 0],
    ["gold-resource", gold, null, Math.min(100, gold / 150 * 100)],
  ];
  for (const [klass, value, total, pct] of values) {
    const el = document.querySelector(`.${klass}`);
    if (!el) continue;
    const label = klass === "hp-resource" ? "生命" : klass === "block-resource" ? "格挡" : klass === "energy-resource" ? "能量" : "金币";
    el.querySelector("span").textContent = label;
    el.querySelector("strong").innerHTML = `${number(value)}${total !== null ? ` <small>/ ${number(total)}</small>` : ""}`;
    el.querySelector(".meter i").style.width = `${pct}%`;
  }
}

function renderEnemies(state) {
  const enemies = Array.isArray(state.enemies) ? state.enemies : [];
  if (!enemies.length) {
    $("combat-board").innerHTML = `<div class="empty-board"><span class="empty-sigil">◎</span><strong>${esc(screenTitle(state.state_type))}</strong><small>当前屏幕没有战斗敌人</small></div>`;
    return;
  }
  $("combat-board").innerHTML = enemies.map((enemy) => {
    const hp = Math.max(0, Number(enemy.hp || 0));
    const max = Math.max(1, Number(enemy.max_hp || hp));
    const intents = Array.isArray(enemy.intents) ? enemy.intents : [];
    const intentText = intents.map((intent) => `${intent.title || intent.type || "意图"} ${intent.label || ""} ${intent.description || ""}`).join(" · ");
    return `<article class="enemy-card"><div class="enemy-top"><strong class="enemy-name">${esc(enemy.name)}</strong><span class="enemy-tag">${esc(enemy.entity_id || "ENEMY")}</span></div><div class="enemy-health"><span>HP</span><b>${number(hp)} / ${number(max)}</b></div><div class="enemy-meter"><i style="width:${Math.min(100, hp / max * 100)}%"></i></div><div class="enemy-intent">${esc(intentText || "暂无可见意图")}</div></article>`;
  }).join("");
}

function renderHand(player = {}) {
  const hand = Array.isArray(player.hand) ? player.hand : [];
  $("hand-count").textContent = `${String(hand.length).padStart(2, "0")} CARDS`;
  if (!hand.length) {
    $("hand-row").innerHTML = `<div class="empty-inline">当前屏幕没有可显示的手牌</div>`;
    return;
  }
  $("hand-row").innerHTML = hand.map((card) => {
    const type = String(card.type || "").toLowerCase();
    return `<article class="card-tile ${esc(type)} ${card.can_play === false ? "card-unplayable" : ""}"><span class="card-cost">${esc(card.cost ?? "—")}</span><strong class="card-name">${esc(card.name)}</strong><span class="card-type">${esc(card.type || "CARD")}${card.is_upgraded ? " +" : ""}</span><span class="card-description">${esc(card.description || "暂无描述")}</span></article>`;
  }).join("");
}

function renderLegal(actions = []) {
  $("legal-count").textContent = `${String(actions.length).padStart(2, "0")} ACTIONS`;
  if (!actions.length) {
    $("legal-list").innerHTML = `<div class="empty-inline">当前屏幕没有支持的合法动作</div>`;
    return;
  }
  $("legal-list").innerHTML = actions.map((action) => `<div class="legal-item"><code class="legal-id">${esc(action.action_id)}</code><span class="legal-description">${esc(action.description)}</span></div>`).join("");
}

function renderMap(state) {
  const map = state.map || {};
  const options = Array.isArray(map.next_options) ? map.next_options : [];
  const boss = map.boss || (Array.isArray(map.bosses) ? map.bosses[0] : null);
  if (state.state_type !== "map" && !boss && !options.length) {
    $("map-summary").innerHTML = `<span class="map-glyph">⌁</span><div><strong>当前屏幕：${esc(screenTitle(state.state_type))}</strong><small>路线数据会在地图屏幕出现时显示</small></div>`;
    $("map-options").replaceChildren();
  } else {
    const run = state.run || {};
    $("map-summary").innerHTML = `<span class="map-glyph">⌁</span><div><strong>${state.state_type === "map" ? "地图路线决策等待中" : "本局 Boss"}</strong><small>当前 Act ${esc(run.act ?? "—")} · 已到第 ${esc(run.floor ?? "—")} 层${boss?.name ? ` · ${esc(boss.name)}` : ""}</small></div>`;
    $("map-options").innerHTML = options.map((node) => `<span class="map-node"><i>${esc(node.row ?? "?")}:${esc(node.col ?? "?")}</i>${esc(node.type || "未知节点")}</span>`).join("") || `<span class="empty-inline">暂无下一层节点</span>`;
  }
  const player = state.player || {};
  $("relic-count").textContent = Array.isArray(player.relics) ? String(player.relics.length).padStart(2, "0") : "—";
  $("potion-count").textContent = Array.isArray(player.potions) ? `${player.potions.length}/${player.max_potion_slots ?? "—"}` : "—";
  $("status-count").textContent = Array.isArray(player.status) ? String(player.status.length).padStart(2, "0") : "—";
}

function screenTitle(screen) {
  return ({ monster: "普通战斗", elite: "精英战斗", boss: "Boss 战斗", map: "地图", rewards: "战后奖励", card_reward: "奖励选牌", shop: "商店", event: "事件", rest_site: "休息点", treasure: "宝箱", hand_select: "战斗选牌", menu: "游戏菜单", unsupported_bridge: "桥接升级必需", unavailable: "游戏状态不可用" })[screen] || screen || "未知";
}

function renderState(data) {
  ui.state = data;
  const stateType = data.state_type || "unknown";
  const bridgeUnavailable = ["unavailable", "unsupported_bridge"].includes(data.status);
  const run = data.run || {};
  const player = data.player || {};
  $("connection-text").textContent = bridgeUnavailable ? "GAME BRIDGE PAUSED" : "LIVE MONITOR";
  $("connection-text").parentElement.classList.toggle("offline", bridgeUnavailable);
  $("screen-name").textContent = screenTitle(stateType);
  $("run-meta").textContent = `ACT ${String(run.act ?? "—").padStart(2, "0")} / FLOOR ${String(run.floor ?? "—").padStart(2, "0")}`;
  $("version-meta").textContent = data.game_version || "v—";
  $("state-health").textContent = data.status === "unsupported_bridge" ? "BRIDGE UPDATE REQUIRED" : data.status === "unavailable" ? "BRIDGE OFFLINE" : data.complete ? "STATE VERIFIED" : "STATE INCOMPLETE";
  $("state-health").className = `state-pill ${bridgeUnavailable ? "offline" : data.complete ? "ready" : "paused"}`;
  renderResources(player);
  renderEnemies(data);
  renderHand(player);
  renderLegal(Array.isArray(data.legal_actions) ? data.legal_actions : []);
  renderMap(data);
  $("run-context").innerHTML = `<div><span>角色</span><strong>${esc(data.character || player.character || "—")}</strong></div><div><span>层数</span><strong>${esc(run.floor ?? "—")} / Act ${esc(run.act ?? "—")}</strong></div><div><span>难度</span><strong>${esc(run.ascension ?? "—")}</strong></div><div><span>状态指纹</span><strong class="mono fingerprint">${esc(data.state_fingerprint || "—")}</strong></div>`;
  const errors = Array.isArray(data.errors) ? data.errors : [];
  $("state-errors").hidden = errors.length === 0;
  $("state-errors").textContent = errors.join(" · ");
}

function eventProvider(event) {
  const provider = String(event.provider || "deterministic").toLowerCase();
  if (provider.includes("luna")) return ["luna", "GPT-6 LUNA"];
  if (provider.includes("kev")) return ["kev", "LOCAL KEV"];
  return ["deterministic", provider === "deterministic" ? "DETERMINISTIC" : provider.toUpperCase()];
}

function describeEvent(event) {
  if (event.event_type === "route_planned") return { title: "路线已规划", summary: event.reason || "Luna 返回路线，控制器验证了地图边与 Boss 终点。" };
  if (event.event_type === "decision_proposed") return { title: event.description || event.action_id || "已生成动作提议", summary: `${screenTitle(event.state_type)} · ${event.reason || event.status || "validated"}` };
  if (event.event_type === "action_submitted") return { title: "动作已提交", summary: `${event.description || event.action_id || "游戏动作"} · ${event.reason || "等待状态回读"}` };
  if (event.event_type === "action_preview") return { title: "动作预览（Dry-run）", summary: event.description || event.action_id || "动作未提交" };
  if (event.event_type === "action_outcome_unknown") return { title: "动作结果待核对", summary: event.reason || "POST 结果未知；控制器未重试。" };
  if (event.event_type === "action_rejected") return { title: "游戏拒绝动作", summary: event.description || event.action_id || "请重新读取状态" };
  return { title: event.event_type || "事件", summary: event.reason || event.description || event.status || "" };
}

function renderEvents(events) {
  ui.events = events;
  $("event-count").textContent = String(events.length).padStart(3, "0");
  if (!events.length) {
    $("trace-list").innerHTML = `<div class="empty-trace"><span>◌</span><strong>还没有 Provider 事件</strong><small>模型决策会在这里按时间出现</small></div>`;
    return;
  }
  const list = $("trace-list");
  const oldScroll = list.scrollTop;
  list.innerHTML = events.slice().reverse().map((event) => {
    const [providerClass, providerLabel] = eventProvider(event);
    const details = describeEvent(event);
    const confidence = Number(event.confidence);
    const hasConfidence = Number.isFinite(confidence);
    const candidates = Array.isArray(event.candidates) ? event.candidates : [];
    const route = Array.isArray(event.route) ? event.route : [];
    const isError = String(event.event_type || "").includes("paused") || String(event.event_type || "").includes("unknown") || String(event.status || "").includes("error");
    const timeText = event.created_at ? new Date(event.created_at).toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
    const candidateHtml = candidates.length ? `<div class="candidate-list">${candidates.map((candidate) => { const probability = Number(candidate.probability ?? event.probabilities?.[candidate.action_id]); const probabilityHtml = Number.isFinite(probability) ? `<small class="candidate-prob">${Math.round(probability * 100)}%</small>` : ""; return `<div class="candidate ${candidate.action_id === event.action_id ? "selected" : ""}"><span class="candidate-main"><b>${candidate.action_id === event.action_id ? "SELECTED" : esc(candidate.action_id || "OPTION")}</b>${esc(candidate.description || candidate.action || "候选动作")}</span>${probabilityHtml}</div>`; }).join("")}</div>` : "";
    const routeHtml = route.length ? `<div class="route-chips">${route.map((node, index) => `<span class="route-chip">${String(index + 1).padStart(2, "0")} · ${esc(node.type || "NODE")} ${esc(node.row)}:${esc(node.col)}</span>`).join("")}</div>` : "";
    const contextHtml = event.state_context ? `<details class="event-context"><summary>查看本次决策的状态输入</summary><pre>${esc(JSON.stringify(event.state_context, null, 2))}</pre></details>` : event.legacy_import ? `<small class="legacy-note">早期轨迹从旧日志导入，未保留当时的状态输入。</small>` : "";
    const confidenceLabel = providerClass === "kev" ? "SELECTIVITY" : "CONFIDENCE";
    const confidenceHtml = hasConfidence ? `<div class="confidence-row"><span>${confidenceLabel}</span><span class="confidence-bar"><i style="width:${Math.max(0, Math.min(100, confidence * 100))}%"></i></span><b>${Math.round(confidence * 100)}%</b></div>` : "";
    const duration = Number(event.duration_ms);
    const durationHtml = Number.isFinite(duration) ? ` · ${Math.round(duration)}ms` : "";
    return `<article class="trace-event provider-${providerClass} ${isError ? "event-error" : ""}"><div class="trace-top"><span class="provider-chip ${providerClass}">${providerLabel}</span><time class="event-time">${timeText}${durationHtml}</time></div><h3 class="event-title">${esc(details.title)}</h3><p class="event-summary">${esc(details.summary)}</p>${confidenceHtml}${candidateHtml}${routeHtml}${contextHtml}</article>`;
  }).join("");
  list.scrollTop = Math.min(oldScroll, list.scrollHeight);
}

function renderRunLogs(data) {
  const runs = Array.isArray(data.runs) ? data.runs : [];
  const latest = runs[0];
  const link = $("run-log-download");
  link.hidden = !latest;
  if (!latest) {
    $("run-log-meta").textContent = "本局记录：等待 Agent 开始决策";
    return;
  }
  const summary = latest.summary || {};
  const status = latest.status === "in_progress" ? "记录中" : latest.status === "complete" ? "胜利完成" : `已结束 · ${latest.outcome || "unknown"}`;
  $("run-log-meta").textContent = `${status} · ${latest.character || "铁甲战士"} · ${number(summary.actions_submitted, "0")} 个已提交动作 · ${number(summary.decisions, "0")} 次判断 · ${latest.run_id || ""}`;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const data = await response.json();
  if (!response.ok && !data.status) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function refreshAll(withStatus = false) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const [state, trace, runs] = await Promise.all([fetchJson("/api/state"), fetchJson("/api/events?limit=120"), fetchJson("/api/runs")]);
    renderState(state);
    renderEvents(trace.events || []);
    renderRunLogs(runs);
    if (withStatus) renderStatus(await fetchJson("/api/status"));
    $("last-sync").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (error) {
    $("connection-text").textContent = "MONITOR ERROR";
    $("connection-text").parentElement.classList.add("offline");
    $("state-health").textContent = "读取失败";
    $("state-health").className = "state-pill offline";
    $("state-errors").hidden = false;
    $("state-errors").textContent = `监控服务错误：${error.message}`;
  } finally { ui.busy = false; }
}

$("refresh-now").addEventListener("click", () => refreshAll(true));
refreshAll(true);
window.setInterval(() => refreshAll(false), 1200);
window.setInterval(() => refreshAll(true), 15000);
