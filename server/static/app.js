/* UKey 管理控制台：原生 JS，无构建工具。所有动态文本一律用 textContent（不用 innerHTML），配合 CSP 防 XSS。 */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const STATE_TEXT = { idle: "空闲", leased: "已分配", connected: "已连接", fault: "故障" };
  const PAGES = ["ports", "agents", "hubs", "audit"];

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "checked" || k === "disabled" || k === "value" || k === "selected") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
  const fmtLeft = (ts) => {
    const s = Math.max(0, Math.floor(ts - Date.now() / 1000));
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };

  async function api(method, url, body) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) { opt.headers["content-type"] = "application/json"; opt.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, opt); } catch (e) { return { ok: false, status: 0, data: { detail: "网络错误：" + e.message } }; }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.status === 401 && url !== "/api/login") { showLogin(); }
    return { ok: res.ok, status: res.status, data };
  }
  const errText = (r) => {
    const d = r.data && r.data.detail;
    return typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`;
  };

  const S = { ports: [], agents: [], hubs: [], audit: [], page: "ports", wz: null };

  // ------------------------------------------------------------------ 登录与路由
  function showLogin() {
    $("#top").classList.add("hidden");
    for (const p of PAGES) $("#page-" + p).classList.add("hidden");
    $("#page-login").classList.remove("hidden");
    closeLive();
  }
  async function afterLogin() {
    $("#page-login").classList.add("hidden");
    $("#top").classList.remove("hidden");
    openLive();
    route();
  }
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const r = await api("POST", "/api/login", { username: f.username.value, password: f.password.value });
    f.password.value = "";
    if (r.ok) { $("#login-error").textContent = ""; afterLogin(); }
    else $("#login-error").textContent = r.status === 401 ? "用户名或密码错误" : errText(r);
  });
  $("#logout").addEventListener("click", async () => { await api("POST", "/api/logout"); showLogin(); });

  function route() {
    const page = (location.hash || "#ports").slice(1);
    S.page = PAGES.includes(page) ? page : "ports";
    for (const p of PAGES) $("#page-" + p).classList.toggle("hidden", p !== S.page);
    document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === S.page));
    if (S.page === "hubs" && S.wz) renderWizard(); else refresh();
  }
  window.addEventListener("hashchange", route);

  async function refresh() {
    if (S.page === "ports") await loadPorts();
    else if (S.page === "agents") await loadAgents();
    else if (S.page === "hubs") await loadHubs();
    else if (S.page === "audit") await loadAudit();
  }

  // ------------------------------------------------------------------ 实时推送
  let ws = null, wsTimer = null, wsDelay = 1000, refreshTimer = null, closing = false;
  function setLive(on) {
    const b = $("#live");
    b.textContent = on ? "实时" : "离线";
    b.className = "badge " + (on ? "on" : "off");
  }
  function openLive() {
    closing = false;
    if (ws) return;
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/console";
    ws = new WebSocket(url);
    ws.onopen = () => { setLive(true); wsDelay = 1000; };
    ws.onmessage = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); };
    ws.onclose = () => {
      ws = null; setLive(false);
      if (!closing) { wsTimer = setTimeout(openLive, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000); }
    };
  }
  function closeLive() { closing = true; clearTimeout(wsTimer); if (ws) ws.close(); }
  setInterval(() => { if (S.page !== "hubs" && !$("#top").classList.contains("hidden")) refresh(); }, 15000);   // 兜底轮询
  setInterval(() => {   // 租约倒计时
    document.querySelectorAll("[data-expires]").forEach((el) => { el.textContent = "剩余 " + fmtLeft(Number(el.dataset.expires)); });
  }, 1000);

  // ------------------------------------------------------------------ 端口总览
  async function loadPorts() {
    const [p, a, hb, au] = await Promise.all([
      api("GET", "/api/ports"), api("GET", "/api/agents"), api("GET", "/api/hubs"), api("GET", "/api/audit?limit=40"),
    ]);
    if (!p.ok) return;
    S.ports = p.data; S.agents = a.data || []; S.hubs = hb.data || []; S.audit = au.data || [];
    renderPorts();
  }
  function renderPorts() {
    const root = clear($("#page-ports"));
    const hubsStatus = Object.fromEntries(S.hubs.map((x) => [x.name, x]));
    const names = [...new Set(S.ports.map((x) => x.hub))];
    const left = h("div");
    for (const name of names) {
      const st = hubsStatus[name];
      const ports = S.ports.filter((x) => x.hub === name);
      const counts = Object.entries(STATE_TEXT).map(([k, t]) => `${t} ${ports.filter((x) => x.state === k).length}`).join(" · ");
      left.append(h("div", { class: "panel" },
        h("div", { class: "hub-head" }, h("h3", {}, name),
          h("span", { class: "badge " + (st && st.online === false ? "off" : "on") }, st && st.online === false ? "离线" : "在线"),
          h("span", { class: "muted" }, counts)),
        h("div", { class: "grid" }, ports.map(portCard))));
    }
    if (!names.length) left.append(h("p", { class: "muted" }, "还没有 Hub。到「Hub 管理」添加。"));
    const online = S.agents.filter((a) => a.online);
    const right = h("div",
      h("div", { class: "panel" }, h("h3", {}, `在线代理（${online.length}/${S.agents.length}）`),
        S.agents.length ? h("ul", { class: "plain" }, S.agents.map((a) => h("li", {},
          h("span", { class: a.online ? "ok" : "muted" }, a.online ? "● " : "○ "), a.agent_id,
          h("span", { class: "muted" }, ` ${a.ip || ""} ${a.status !== "approved" ? "（" + statusText(a.status) + "）" : ""}`)))) : h("p", { class: "muted" }, "暂无代理")),
      h("div", { class: "panel" }, h("h3", {}, "最近操作"),
        h("div", { class: "scroll" }, h("ul", { class: "plain mono" }, S.audit.map((r) => h("li", {},
          `${new Date(r.ts * 1000).toLocaleTimeString()} `, h("span", { class: resClass(r.result) }, r.result), ` ${r.action} ${r.port_id || ""} ${r.agent_id || ""}`))))));
    root.append(h("h2", {}, "端口总览"), h("div", { class: "cols" }, left, right));
  }
  const statusText = (s) => ({ pending: "待批准", approved: "已批准", rejected: "已拒绝" }[s] || s);
  const resClass = (r) => (r === "ok" ? "ok" : r === "warn" || r === "busy" || r === "pending" ? "warn" : "bad");

  function portCard(p) {
    const who = p.agent_id ? `${p.agent_id}${p.client_id ? " / " + p.client_id : ""}` : "";
    const btn = p.state !== "idle"
      ? h("button", { class: "danger", onclick: () => forceRelease(p) }, "强制回收") : null;
    return h("div", { class: "card " + p.state },
      h("div", { class: "num" }, `端口 ${p.port}`),
      h("div", { class: "label", title: p.label || "" }, p.label || "—"),
      h("div", { class: "state", title: p.fault_reason || "" }, STATE_TEXT[p.state] || p.state),
      p.state === "fault" ? h("div", { class: "who" }, (p.fault_reason || "").slice(0, 60)) : h("div", { class: "who" }, who || " "),
      p.lease_expires_at && p.state !== "idle" ? h("div", { class: "who", "data-expires": p.lease_expires_at }, "剩余 " + fmtLeft(p.lease_expires_at)) : null,
      btn);
  }
  async function forceRelease(p) {
    if (!confirm(`强制回收 ${p.port_id}？\n会立即撤销授权，已连接的设备会被强制断开（可能丢失未保存的数据）。`)) return;
    const r = await api("POST", `/api/ports/${encodeURIComponent(p.port_id)}/force-release`);
    if (!r.ok) alert("回收失败：" + errText(r));
    loadPorts();
  }

  // ------------------------------------------------------------------ 代理
  async function loadAgents() {
    const r = await api("GET", "/api/agents");
    if (!r.ok) return;
    S.agents = r.data;
    const root = clear($("#page-agents"));
    const pending = S.agents.filter((a) => a.status === "pending");
    root.append(h("h2", {}, "代理"));
    if (pending.length) root.append(h("div", { class: "notice" }, `有 ${pending.length} 个代理待批准。核对主机名和 IP 后点「批准」。`));
    root.append(h("div", { class: "panel" }, h("table", {},
      h("thead", {}, h("tr", {}, ["代理", "IP", "批准状态", "在线", "Client ID", "Manager", "自检", "最近心跳", "操作"].map((t) => h("th", {}, t)))),
      h("tbody", {}, S.agents.map(agentRow)))),
      h("p", { class: "muted" }, "批准后，Client ID 会在该代理首次申请端口时自动加入对应 Hub 的客户端列表（动态分配）；静态授权请在「Hub 管理 → 编辑」第 4 步勾选。"));
  }
  function agentRow(a) {
    let info = {};
    try { info = JSON.parse(a.info || "{}"); } catch (_) { /* ignore */ }
    const problems = [];
    if (a.status === "approved") {
      if (!a.online) problems.push("离线");
      if (info.manager_mode && info.manager_mode !== "service") problems.push(`Manager 为 ${info.manager_mode}，需修复`);
      if (!info.manager_mode) problems.push("尚未上报 Manager 状态");
    }
    const ok = a.status === "approved" && !problems.length;
    return h("tr", {},
      h("td", {}, a.agent_id, h("div", { class: "muted" }, a.hostname || "")),
      h("td", {}, a.ip || "—"),
      h("td", { class: a.status === "approved" ? "ok" : a.status === "pending" ? "warn" : "bad" }, statusText(a.status)),
      h("td", { class: a.online ? "ok" : "muted" }, a.online ? "在线" : "离线"),
      h("td", { class: "mono" }, a.client_id || "—"),
      h("td", {}, info.manager_mode ? `${info.manager_mode} ${info.manager_version || ""}` : "—"),
      h("td", { class: ok ? "ok" : "warn" }, a.status !== "approved" ? "—" : ok ? "正常" : problems.join("；")),
      h("td", {}, fmtTime(a.last_heartbeat)),
      h("td", {},
        a.status === "pending" ? h("button", { onclick: () => agentAction(a, "approve", "批准") }, "批准") : null,
        a.status === "approved" ? h("button", { class: "secondary", onclick: () => agentAction(a, "run-setup", "重新执行 setup-manager") }, "重新执行 setup-manager") : null,
        " ", h("button", { class: "link", onclick: () => agentAction(a, "reset-token", "重置 token（代理需重新登记）", true) }, "重置 token")));
  }
  async function agentAction(a, action, label, confirmIt) {
    if (confirmIt && !confirm(`${label}：${a.agent_id}？`)) return;
    const r = await api("POST", `/api/agents/${encodeURIComponent(a.agent_id)}/${action}`);
    if (!r.ok) alert(`${label}失败：` + errText(r));
    loadAgents();
  }

  // ------------------------------------------------------------------ 审计
  async function loadAudit() {
    const r = await api("GET", "/api/audit?limit=500");
    if (!r.ok) return;
    S.audit = r.data;
    const root = clear($("#page-audit"));
    const filter = h("input", { placeholder: "按端口 / 代理 / 动作 / 结果过滤", type: "text" });
    const body = h("tbody");
    const draw = () => {
      const q = filter.value.trim().toLowerCase();
      clear(body).append(...S.audit.filter((x) => !q || [x.actor, x.agent_id, x.port_id, x.action, x.result, x.detail].join(" ").toLowerCase().includes(q))
        .map((x) => h("tr", {}, h("td", { class: "mono" }, new Date(x.ts * 1000).toLocaleString()), h("td", {}, x.actor || ""), h("td", {}, x.agent_id || ""),
          h("td", { class: "mono" }, x.port_id || ""), h("td", {}, x.action), h("td", { class: resClass(x.result) }, x.result), h("td", {}, x.detail || ""))));
    };
    filter.addEventListener("input", draw);
    root.append(h("h2", {}, "审计日志"), filter, h("div", { class: "panel scroll tall" }, h("table", {},
      h("thead", {}, h("tr", {}, ["时间", "操作者", "代理", "端口", "动作", "结果", "详情"].map((t) => h("th", {}, t)))), body)));
    draw();
  }

  // ------------------------------------------------------------------ Hub 管理与向导
  async function loadHubs() {
    if (S.wz) return;   // 向导进行中：实时推送不重绘，避免输入框丢焦点
    const r = await api("GET", "/api/hubs/config");
    if (!r.ok) return;
    S.hubCfg = r.data;
    const root = clear($("#page-hubs"));
    root.append(h("h2", {}, "Hub 管理"),
      h("div", { class: "panel" }, h("table", {},
        h("thead", {}, h("tr", {}, ["名称", "地址", "型号 / 序列号", "端口", "状态", "活动租约", "操作"].map((t) => h("th", {}, t)))),
        h("tbody", {}, r.data.hubs.map((x) => h("tr", {}, h("td", {}, x.name), h("td", {}, x.address), h("td", {}, `${x.model || "—"} / ${x.serial || "—"}`),
          h("td", {}, x.ports), h("td", { class: x.online ? "ok" : "bad" }, x.online ? "在线" : "离线"), h("td", {}, x.active_leases),
          h("td", {}, h("button", { class: "secondary", onclick: () => startWizard("edit", x) }, "编辑"), " ",
            h("button", { class: "danger", onclick: () => removeHub(x) }, "移除"))))))),
      h("button", { onclick: () => startWizard("add") }, "＋ 添加 Hub"),
      h("p", { class: "muted" }, "移除只会从服务器配置中删除该 Hub，不改动 Hub 上的配置；有活动租约时不能移除。"));
  }
  async function removeHub(x) {
    if (!confirm(`从服务器配置中移除 ${x.name}？\n不会改动 Hub 上的配置。`)) return;
    const r = await api("DELETE", `/api/hubs/${encodeURIComponent(x.name)}`);
    if (!r.ok) alert("移除失败：" + errText(r)); else alert(r.data.note);
    loadHubs();
  }

  const STEPS = ["基本信息", "连接与识别", "分组规划", "Client ID", "确认并应用"];
  function startWizard(mode, hub) {
    S.wz = { mode, step: mode === "edit" ? 2 : 1, form: { name: mode === "edit" ? hub.name : S.hubCfg.suggested_name, address: hub ? hub.address : "", admin_user: "admin", tls_verify: false, model: hub ? hub.model || "" : "", serial: hub ? hub.serial || "" : "" },
      opts: { grouping: "per-port", authorization: "dynamic", enable_service: false, labels: {}, descriptions: {}, clients: new Set(), manual: [], grants: {} },
      probe: null, plan: null, status: null, err: "", busy: false };
    if (mode === "edit") { probe(true); return; }
    renderWizard();
  }
  function leaveWizard() { S.wz = null; loadHubs(); }

  function stepper(cur) {
    return h("div", { class: "steps" }, STEPS.map((t, i) => h("span", { class: i + 1 === cur ? "cur" : i + 1 < cur ? "done" : "" }, `${i + 1}. ${t}`)));
  }
  async function probe(edit) {
    const w = S.wz, f = w.form;
    w.busy = true; w.err = ""; renderWizard();
    const body = { name: f.name, address: f.address, admin_user: f.admin_user, password: f.password || "", tls_verify: f.tls_verify, model: f.model || null, serial: f.serial || null, edit: !!edit };
    f.password = "";   // 提交后立刻丢弃，不在页面里保留
    const r = await api("POST", "/api/hubs/probe", body);
    w.busy = false;
    if (!r.ok) { w.err = errText(r); if (edit) { alert(w.err); leaveWizard(); return; } w.step = 1; renderWizard(); return; }
    w.probe = r.data; w.step = 2;
    const pr = await api("GET", "/api/ports");   // 编辑：带出已有 UKey 名称
    if (pr.ok) for (const p of pr.data) if (p.hub === w.form.name && p.label) w.opts.labels[p.port] = p.label;
    w.opts.clients = new Set(r.data.clients.map((c) => c.id));
    renderWizard();
  }
  async function planNow() {
    const w = S.wz, o = w.opts;
    const clients = [...new Set([...o.clients, ...o.manual])];
    const body = { session_id: w.probe.session_id, grouping: o.grouping, authorization: o.authorization, enable_service: o.enable_service,
      labels: Object.fromEntries(Object.entries(o.labels).map(([k, v]) => [String(k), v || ""])), descriptions: Object.fromEntries(Object.entries(o.descriptions).filter(([, v]) => v)),
      client_ids: clients, static_grants: o.authorization === "static" ? o.grants : {} };
    const r = await api("POST", "/api/hubs/plan", body);
    w.err = r.ok ? "" : errText(r);
    w.plan = r.ok ? r.data : null;
    return r.ok;
  }

  function renderWizard() {
    const w = S.wz; if (!w) return;
    const root = clear($("#page-hubs"));
    root.append(h("h2", {}, w.mode === "edit" ? `编辑 Hub：${w.form.name}` : "添加 Hub"), stepper(w.step));
    if (w.err) root.append(h("div", { class: "notice bad" }, w.err));
    const step = [null, step1, step2, step3, step4, step5][w.step];
    root.append(step(w));
  }
  const nav = (back, next, nextText = "下一步", nextDisabled = false) => h("div", { class: "actions" },
    back ? h("button", { class: "secondary", onclick: back }, "上一步") : null,
    next ? h("button", { onclick: next, disabled: nextDisabled }, nextText) : null,
    h("button", { class: "link", onclick: leaveWizard }, "取消"));
  const field = (label, input) => h("label", {}, label, input);

  function step1(w) {
    const f = w.form;
    const bind = (key, type) => h("input", { type: type || "text", value: f[key] || "", autocomplete: "off", oninput: (e) => { f[key] = e.target.value; } });
    return h("div", { class: "panel narrowish" },
      h("p", { class: "muted" }, "第 1 步不会连接 Hub。第 2 步只读探测并自动备份；直到第 5 步确认前，不会对 Hub 做任何写操作。"),
      h("div", { class: "row" }, field("名称（唯一）", bind("name")), field("IP 地址 / 主机名", bind("address"))),
      h("div", { class: "row" }, field("管理员用户名", bind("admin_user")), field("管理员密码（提交后不回显）", bind("password", "password"))),
      h("div", { class: "row" }, field("型号（可选，仅作记录）", bind("model")), field("序列号（可选，仅作记录）", bind("serial"))),
      h("label", { class: "inline" }, h("input", { type: "checkbox", checked: f.tls_verify, onchange: (e) => { f.tls_verify = e.target.checked; } }), "校验 Hub 的 TLS 证书（Hub 默认自签，演示通常不勾选）"),
      nav(null, () => probe(false), w.busy ? "探测中…" : "下一步：连接并探测", w.busy));
  }

  function step2(w) {
    const p = w.probe, id = p.identity;
    return h("div", {},
      p.warnings.map((t, i) => h("div", { class: "notice" + (p.blocked && i === 0 ? " bad" : "") }, t)),
      h("div", { class: "panel" }, h("h3", {}, "设备"),
        h("ul", { class: "plain" },
          h("li", {}, "型号：", id.model || "未知", "　序列号：", id.serial || "未知", "　固件：", id.firmware || "读不到", "　端口数：", id.ports),
          h("li", {}, "AnywhereUSB 服务：", h("span", { class: p.service.enabled ? "ok" : "warn" }, p.service.enabled ? "已启用" : "未启用"), `　服务端口：${p.service.port}`),
          h("li", {}, "已自动备份到 backups/", h("span", { class: "mono" }, p.backup)))),
      h("div", { class: "panel" }, h("h3", {}, `现有分组（${p.groups.length}）`), h("table", {}, h("thead", {}, h("tr", {}, ["分组", "描述", "端口"].map((t) => h("th", {}, t)))),
        h("tbody", {}, p.groups.map((g) => h("tr", {}, h("td", { class: "mono" }, g.name), h("td", {}, g.description), h("td", {}, g.ports.join(", ") || "—")))))),
      h("div", { class: "panel" }, h("h3", {}, `现有客户端（${p.clients.length} / ${p.client_limit}）`),
        p.clients.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Client ID", "可访问分组"].map((t) => h("th", {}, t)))),
          h("tbody", {}, p.clients.map((c) => h("tr", {}, h("td", { class: "mono" }, c.id), h("td", {}, c.groups.join(", ") || "—"))))) : h("p", { class: "muted" }, "（空）")),
      nav(w.mode === "edit" ? null : () => { w.step = 1; renderWizard(); }, async () => { w.step = 3; await planNow(); renderWizard(); }, "下一步", p.blocked));
  }

  function step3(w) {
    const p = w.probe, o = w.opts, n = p.identity.ports;
    const pattern = (i) => `${w.form.name}-p${String(i).padStart(2, "0")}`;
    const rows = [];
    for (let i = 1; i <= n; i++) {
      rows.push(h("tr", {}, h("td", {}, i), h("td", { class: "mono" }, `group${String(i).padStart(2, "0")}`),
        h("td", {}, h("input", { type: "text", value: o.labels[i] || "", placeholder: "UKey 显示名", oninput: (e) => { o.labels[i] = e.target.value; } })),
        h("td", {}, h("input", { type: "text", value: o.descriptions[i] || "", placeholder: pattern(i), oninput: (e) => { o.descriptions[i] = e.target.value; } }))));
    }
    const changes = w.plan ? w.plan.changes.filter((c) => !["register", "add_client", "grant"].includes(c.kind)) : [];
    return h("div", {},
      h("div", { class: "panel" },
        h("label", { class: "inline" }, h("input", { type: "radio", name: "grp", checked: o.grouping === "per-port", onchange: () => { o.grouping = "per-port"; } }), "每口一组（端口 N = groupNN，推荐）"),
        h("label", { class: "inline" }, h("input", { type: "radio", name: "grp", checked: o.grouping === "keep", onchange: () => { o.grouping = "keep"; } }), "保留现有分组，仅登记"),
        !p.service.enabled ? h("label", { class: "inline" }, h("input", { type: "checkbox", checked: o.enable_service, onchange: (e) => { o.enable_service = e.target.checked; } }), "启用 AnywhereUSB 服务") : null),
      h("div", { class: "panel" }, h("table", {}, h("thead", {}, h("tr", {}, ["端口", "分组", "UKey 显示名", "分组描述（默认按命名规则）"].map((t) => h("th", {}, t)))), h("tbody", {}, rows))),
      h("div", { class: "panel" }, h("h3", {}, "与当前配置的差异"),
        w.plan ? h("div", {}, h("p", { class: "muted" }, `不变：分组 ${w.plan.unchanged.groups} 项。以下为将要新增 / 修改的内容：`),
          changes.length ? h("ul", { class: "plain" }, changes.map((c) => h("li", {}, c.kind === "move_port" ? "修改 · " : c.kind === "describe" ? "修改 · " : c.kind === "enable_service" ? "修改 · " : "⚠ ", c.text))) : h("p", { class: "ok" }, "分组无需变更"))
          : h("p", { class: "muted" }, "点「刷新差异」查看"),
        h("button", { class: "secondary", onclick: async () => { await planNow(); renderWizard(); } }, "刷新差异")),
      nav(() => { w.step = 2; renderWizard(); }, async () => { w.step = 4; await planNow(); renderWizard(); }));
  }

  function step4(w) {
    const p = w.probe, o = w.opts;
    const known = new Set([...p.clients.map((c) => c.id), ...p.approved_clients.map((c) => c.client_id)]);
    const total = new Set([...p.clients.map((c) => c.id), ...o.clients, ...o.manual]).size;
    const all = [...new Set([...o.clients, ...o.manual])];
    const manualInput = h("input", { type: "text", placeholder: "手动输入 Client ID，多个用空格或逗号分隔" });
    const ckRow = (cid, note) => h("label", { class: "inline" }, h("input", { type: "checkbox", checked: o.clients.has(cid), onchange: (e) => { if (e.target.checked) o.clients.add(cid); else o.clients.delete(cid); renderWizard(); } }), cid, note ? h("span", { class: "muted" }, ` ${note}`) : null);
    const groups = p.groups.map((g) => g.name);
    return h("div", {},
      h("div", { class: "panel" }, h("h3", {}, "客户端列表"),
        h("p", { class: "muted" }, "已批准代理上报的 Client ID 与 Hub 上现有的都可勾选；也可手动输入。"),
        h("div", {}, [...known].map((cid) => ckRow(cid, (p.approved_clients.find((a) => a.client_id === cid) || {}).hostname ? `（${p.approved_clients.find((a) => a.client_id === cid).hostname}）` : ""))),
        o.manual.length ? h("div", {}, h("span", { class: "muted" }, "手动添加："), o.manual.map((cid) => h("span", { class: "badge on" }, cid, " ", h("button", { class: "link", onclick: () => { o.manual = o.manual.filter((x) => x !== cid); renderWizard(); } }, "×")), " ")) : null,
        h("div", { class: "row" }, manualInput, h("button", { class: "secondary", onclick: () => { for (const t of manualInput.value.split(/[\s,]+/).filter(Boolean)) if (!o.manual.includes(t) && !known.has(t)) o.manual.push(t); else if (known.has(t)) o.clients.add(t); renderWizard(); } }, "添加")),
        h("p", { class: total > p.client_limit ? "bad" : "muted" }, `客户端数量 ${total} / ${p.client_limit}`)),
      h("div", { class: "panel" }, h("h3", {}, "授权模式"),
        h("label", { class: "inline" }, h("input", { type: "radio", name: "auth", checked: o.authorization === "dynamic", onchange: () => { o.authorization = "dynamic"; renderWizard(); } }), "动态分配（推荐）：只加入客户端列表，不预先授予分组，由调度引擎按需授权"),
        h("label", { class: "inline" }, h("input", { type: "radio", name: "auth", checked: o.authorization === "static", onchange: () => { o.authorization = "static"; renderWizard(); } }), "静态授权：为每个 Client ID 勾选可访问的分组"),
        o.authorization === "static" ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, [h("th", {}, "Client ID"), ...groups.map((g) => h("th", {}, g.replace("group", "G")))])),
          h("tbody", {}, all.map((cid) => h("tr", {}, h("td", { class: "mono" }, cid), ...groups.map((g) => h("td", {}, h("input", { type: "checkbox", checked: (o.grants[cid] || []).includes(g), onchange: (e) => { const s = new Set(o.grants[cid] || []); if (e.target.checked) s.add(g); else s.delete(g); o.grants[cid] = [...s]; } })))))))) : null),
      nav(() => { w.step = 3; renderWizard(); }, async () => { if (await planNow()) w.step = 5; renderWizard(); }));
  }

  function step5(w) {
    const pl = w.plan, st = w.status;
    const running = st && st.status === "applying";
    const done = st && st.status === "done";
    const failed = st && st.status === "failed";
    const restored = st && st.status === "restored";
    const ck = h("input", { type: "checkbox", id: "confirm-apply", disabled: !!st, onchange: (e) => { $("#apply-btn").disabled = !e.target.checked; } });
    return h("div", {},
      pl.warnings.map((t) => h("div", { class: "notice" }, t)),
      h("div", { class: "panel" }, h("h3", {}, `将执行的变更（${pl.changes.length} 项）`), h("ul", { class: "plain" }, pl.changes.map((c) => h("li", {}, c.text))),
        h("p", { class: "muted" }, `客户端数量 ${pl.clients.count} / ${pl.clients.limit}。逐项写入并写后读校验；任一步失败立即停止，可恢复到第 2 步的备份。`)),
      st ? h("div", { class: "panel" }, h("h3", {}, "进度"), h("ul", { class: "plain prog" }, st.progress.map((x) => h("li", { class: x.status }, x.text, x.detail ? h("span", { class: "bad" }, " — " + x.detail) : null))),
        running ? h("p", { class: "muted" }, "执行中…") : null,
        done ? h("p", { class: "ok" }, "完成：已写入 config.yaml（旧文件已备份）并热加载，无需重启。") : null,
        failed ? h("div", { class: "notice bad" }, "失败并已停止：", st.error, h("div", { class: "actions" }, h("button", { class: "danger", onclick: restoreNow }, `恢复到第 2 步的备份（${st.backup}）`))) : null,
        restored ? h("p", { class: "warn" }, "已恢复到探测时的备份。") : null) : null,
      !st ? h("label", {}, ck, "我已核对以上变更，确认对 Hub 执行写入") : null,
      h("div", { class: "actions" },
        !st ? h("button", { class: "secondary", onclick: () => { w.step = 4; renderWizard(); } }, "上一步") : null,
        !st ? h("button", { id: "apply-btn", disabled: true, onclick: applyNow }, "确认并应用") : null,
        (done || restored || failed) ? h("button", { onclick: leaveWizard }, "返回 Hub 管理") : null,
        !st ? h("button", { class: "link", onclick: leaveWizard }, "取消") : null));
  }
  async function applyNow() {
    const w = S.wz;
    const r = await api("POST", "/api/hubs/apply", { session_id: w.probe.session_id, confirm: true });
    if (!r.ok) { w.err = errText(r); renderWizard(); return; }
    w.status = r.data; renderWizard();
    const poll = async () => {
      if (!S.wz || S.wz !== w) return;
      const s = await api("GET", "/api/hubs/wizard/" + encodeURIComponent(w.probe.session_id));
      if (s.ok) { w.status = s.data; renderWizard(); if (s.data.status === "applying") setTimeout(poll, 500); }
    };
    setTimeout(poll, 400);
  }
  async function restoreNow() {
    const w = S.wz;
    if (!confirm("把 Hub 恢复到第 2 步探测时的备份？")) return;
    const r = await api("POST", `/api/hubs/${encodeURIComponent(w.form.name)}/restore`, { session_id: w.probe.session_id });
    if (!r.ok) { w.err = errText(r); } else { w.status = r.data; w.err = ""; }
    renderWizard();
  }

  // ------------------------------------------------------------------ 启动
  (async () => {
    const r = await api("GET", "/api/session");
    if (r.ok) afterLogin(); else showLogin();
  })();
})();
