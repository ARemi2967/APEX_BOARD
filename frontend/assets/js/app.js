/* App state + wiring: load players, render KPIs/charts, handle controls. */
(() => {
  const state = {
    players: [], playerId: null, metric: "damage", mainLegend: null, legendRows: [],
    calibration: {}, calibTotals: null, cur: null,
  };

  // 趋势指标：dailyGain=每日从零开始的累计增量折线；cumulative=绝对总分；daily=每日增量柱
  const TREND_METRICS = {
    damage: { label: "伤害", mode: "dailyGain", hist: "damage" },
    kills: { label: "击杀", mode: "dailyGain", hist: "kills" },
    rank_score: { label: "段位分", mode: "cumulative", hist: "rank_score" },
    rank_delta: { label: "段位变化", mode: "daily", hist: "rank_score", seasonAware: true },
  };

  // 传奇中文（官方译名，个别新传奇用音译，可自行校正）
  const LEGEND_CN = {
    "Global": "总览",
    "Bloodhound": "寻血猎犬", "Gibraltar": "直布罗陀", "Lifeline": "命脉",
    "Pathfinder": "探路者", "Wraith": "恶灵", "Bangalore": "班加罗尔",
    "Caustic": "侵蚀", "Mirage": "幻象", "Octane": "动力小子",
    "Wattson": "沃特森", "Crypto": "密客", "Revenant": "亡灵",
    "Loba": "罗芭", "Rampart": "兰伯特", "Horizon": "地平线",
    "Fuse": "暴雷", "Valkyrie": "瓦尔基里", "Seer": "希尔",
    "Ash": "艾许", "Mad Maggie": "疯玛吉", "Newcastle": "纽卡斯尔",
    "Vantage": "万蒂奇", "Catalyst": "卡特莉丝", "Ballistic": "弹道",
    "Conduit": "导线管", "Alter": "变幻", "Sparrow": "琉雀", "Axle": "艾克赛尔",
  };
  const legendCn = (n) => (!n ? "" : LEGEND_CN[n] || n);
  // 段位 -> 配色类
  const tierClass = (name) => {
    const n = (name || "").toLowerCase();
    if (n.includes("predator")) return "predator";
    if (n.includes("master")) return "master";
    if (n.includes("diamond")) return "diamond";
    if (n.includes("platinum")) return "platinum";
    if (n.includes("gold")) return "gold";
    if (n.includes("silver")) return "silver";
    if (n.includes("bronze")) return "bronze";
    return "unranked";
  };

  // 追踪器 / 武器名中文
  const TRACKER_CN = {
    "BR Kills": "击杀", "BR Damage": "伤害",
    "BR SMG kills": "冲锋枪击杀", "BR Sniper kills": "狙击枪击杀", "BR Marksman kills": "神射手击杀",
    "BR Winning kills": "吃鸡击杀",
    "BR Season 21 kills": "S21 击杀", "BR Season 21 wins": "S21 胜场",
    "Droppod: Items for squadmates": "空投：队友物资",
    "Gravity lift: Teammates lifted": "重力升降梯：托举队友",
    "KD": "K/D",
    "kills": "击杀", "damage": "伤害",
    "specialEvent_kills": "击杀", "specialEvent_damage": "伤害",
  };
  // 武器精通名 -> 中文（按官方简中译名；API 冒出新武器时自动套用）
  const WEAPON_CN = {
    "Alternator": "转换者", "R-99": "R-99", "R-301": "R-301", "RE-45": "RE-45",
    "Flatline": "平行步枪", "Hemlok": "赫姆洛克", "Prowler": "猎兽", "Volt": "电能",
    "Havoc": "哈沃克", "Devotion": "专注", "L-STAR": "L-STAR", "Spitfire": "喷火",
    "M600": "喷火", "Rampage": "暴走", "G7": "G7", "30-30": "30-30",
    "Triple Take": "三重式", "Kraber": "克雷贝尔", "Charge Rifle": "充能步枪",
    "Longbow": "长弓", "Sentinel": "哨兵", "Mastiff": "敖犬", "Peacekeeper": "和平捍卫者",
    "EVA-8": "EVA-8", "Mozambique": "莫桑比克", "Wingman": "辅助手枪", "P2020": "P2020",
    "Bocek": "波塞克", "CAR": "C.A.R", "C.A.R": "C.A.R", "Nemesis": "复仇女神",
  };
  // 按关键词长度降序匹配，避免 "R-301 Carbine" 里的 "Car" 误中 C.A.R
  const _WEAPON_KEYS = Object.keys(WEAPON_CN).sort((a, b) => b.length - a.length);
  const trackerCn = (n) => {
    if (!n) return "";
    if (TRACKER_CN[n]) return TRACKER_CN[n];
    for (const en of _WEAPON_KEYS) {
      if (n.includes(en)) return WEAPON_CN[en] + "击杀";
    }
    return n.replace(/^BR\s/, "");
  };
  // 武器伤害名（同上但后缀"伤害"）
  const trackerCnDamage = (n) => {
    if (!n) return "";
    for (const en of _WEAPON_KEYS) if (n.includes(en)) return WEAPON_CN[en] + "伤害";
    return n.replace(/^BR\s/, "").replace(/Kills?$/i, "伤害");
  };

  // 图片走后端代理（避免浏览器直连 api.mozambiquehe.re 被墙）
  const proxyImg = (url) => (url ? `/api/img?url=${encodeURIComponent(url)}` : "");

  // 时间：服务端存的是 UTC（无时区），补 Z 让浏览器转成本地(北京)时间
  const parseTs = (iso) => {
    if (!iso) return null;
    const s = String(iso);
    return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
  };

  const $ = (sel) => document.querySelector(sel);
  const els = {
    select: $("#player-select"), lastUpdated: $("#last-updated"), refresh: $("#refresh-btn"),
    banner: $("#player-banner"), bannerName: $("#banner-name"), bannerUid: $("#banner-uid"),
    bannerRank: $("#banner-rank"), bannerRp: $("#banner-rp"), bannerDeltaRp: $("#banner-delta-rp"),
    rankBadge: $("#rank-badge"),
    todayReport: $("#today-report"),
    trendEmpty: $("#trend-empty"), legendsEmpty: $("#legends-empty"), rankEmpty: $("#rank-empty"),
    trendChart: $("#trend-chart"), legendsList: $("#legends-list"), featuredLegend: $("#featured-legend"),
    rankChart: $("#rank-chart"),
    rtBadge: $("#rt-badge"), weaponChart: $("#weapon-chart"), weaponEmpty: $("#weapon-empty"),
    weaponDamageChart: $("#weapon-damage-chart"), weaponDamageEmpty: $("#weapon-damage-empty"),
    toast: $("#toast"),
    activityList: $("#activity-list"), activityEmpty: $("#activity-empty"),
    recordsTable: $("#records-table"), recordsEmpty: $("#records-empty"),
    rankProgressBadge: $("#rank-progress-badge"),
  };

  function animateValue(node, target) {
    const dur = 850, t0 = performance.now();
    function step(now) {
      const p = Math.min(1, (now - t0) / dur);
      node.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString();
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function setKpi(key, value, animate) {
    const node = document.querySelector(`[data-kpi="${key}"]`);
    if (!node) return;
    if (value == null) { node.textContent = "—"; node.classList.add("is-na"); return; }
    node.classList.remove("is-na");
    if (animate && typeof value === "number") animateValue(node, value);
    else node.textContent = value;
  }

  // KPI 击杀/伤害 = 追踪器合计 + EA 对账缺失量（陈旧追踪器漏掉的部分）
  function renderKpis(cur) {
    const mk = (state.calibTotals && state.calibTotals.kills) || 0;
    const md = (state.calibTotals && state.calibTotals.damage) || 0;
    setKpi("kills", cur.kills == null ? null : cur.kills + mk, true);
    setKpi("damage", cur.damage == null ? null : cur.damage + md, true);
  }

  function showToast(msg) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    requestAnimationFrame(() => els.toast.classList.add("is-visible"));
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      els.toast.classList.remove("is-visible");
      setTimeout(() => { els.toast.hidden = true; }, 250);
    }, 3200);
  }

  function renderCurrent(cur) {
    els.banner.hidden = false;
    els.bannerName.textContent = cur.name || "（无显示名）";
    const rankText = cur.rank_name ? `${cur.rank_name} ${cur.rank_div || ""}`.trim() : "未定级";
    els.bannerRank.textContent = rankText;
    const tc = "t-" + tierClass(cur.rank_name);
    if (cur.rank_img) {
      els.rankBadge.src = proxyImg(cur.rank_img); els.rankBadge.hidden = false;
      els.rankBadge.className = "rank-badge " + tc;
      els.rankProgressBadge.src = proxyImg(cur.rank_img); els.rankProgressBadge.hidden = false;
      els.rankProgressBadge.className = "rank-progress-badge " + tc;
    } else { els.rankBadge.hidden = true; els.rankProgressBadge.hidden = true; }
    els.bannerRp.textContent = cur.rank_score != null ? `${Number(cur.rank_score).toLocaleString()} RP` : "";
    setKpi("rank", rankText);
    setKpi("level", cur.level, true);
    state.cur = cur;
    renderKpis(cur);
    let rtCls = "rt-offline", rtTxt = "⚪ 离线";
    if (cur.is_in_game) { rtCls = "rt-game"; rtTxt = `🎮 游戏中${cur.selected_legend ? " · " + legendCn(cur.selected_legend) : ""}`; }
    else if (cur.is_online) { rtCls = "rt-lobby"; rtTxt = `🟠 大厅中${cur.selected_legend ? " · " + legendCn(cur.selected_legend) : ""}`; }
    els.rtBadge.className = "rt-badge " + rtCls;
    els.rtBadge.textContent = rtTxt;
    const d = parseTs(cur.fetched_at);
    if (d) els.lastUpdated.textContent = `更新于 ${d.toLocaleTimeString()}`;
  }

  function sortLegendRows(rows) {
    rows.sort((a, b) => {
      const aw = (a.kills || 0) + (a.damage || 0), bw = (b.kills || 0) + (b.damage || 0);
      if ((aw > 0) !== (bw > 0)) return aw > 0 ? -1 : 1;
      return (b.kills || 0) - (a.kills || 0);
    });
    return rows;
  }

  function extractLegendStats(legendsObj) {
    const rows = [];
    for (const [name, info] of Object.entries(legendsObj || {})) {
      if (name === "Global") continue;  // 账号聚合桶，不是传奇
      const data = info && info.data;
      const entries = Array.isArray(data) ? data : [];
      let kills = null, damage = null, kfb = null, dfb = null;
      for (const t of entries) {
        const k = String((t && t.key) || "");
        if (k === "specialEvent_kills") kills = t.value; else if (k === "kills") kfb = t.value;
        if (k === "specialEvent_damage") damage = t.value; else if (k === "damage") dfb = t.value;
      }
      rows.push({ name, kills: kills != null ? kills : kfb, damage: damage != null ? damage : dfb });
    }
    return sortLegendRows(rows);
  }

  // 套用 EA 对账校准：陈旧追踪器的传奇用"追踪器值+缺失量"替换，并保留原始值供悬浮查看
  function applyCalibration(rows, cal) {
    if (!cal || !Object.keys(cal).length) return rows;
    const byName = new Map(rows.map((r) => [r.name, r]));
    for (const [name, c] of Object.entries(cal)) {
      const r = byName.get(name);
      if (r) {
        r.rawKills = r.kills; r.rawDamage = r.damage;
        if (c.calibrated_kills != null) r.kills = c.calibrated_kills;
        if (c.calibrated_damage != null) r.damage = c.calibrated_damage;
        r.calibrated = true;
      } else {
        byName.set(name, {
          name, kills: c.calibrated_kills, damage: c.calibrated_damage,
          rawKills: null, rawDamage: null, calibrated: true,
        });
      }
    }
    return sortLegendRows([...byName.values()]);
  }

  function fmtCompact(n) {
    if (n == null) return "—";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
    return n.toLocaleString();
  }

  function renderLegendRows(rows) {
    const maxKills = Math.max(1, ...rows.map((r) => r.kills || 0));
    const maxDamage = Math.max(1, ...rows.map((r) => r.damage || 0));
    const head = `<div class="legend-head"><div>传奇</div><div>击杀</div><div>伤害</div></div>`;
    const body = rows.map((r) => {
      const kpct = r.kills ? (r.kills / maxKills) * 100 : 0;
      const dpct = r.damage ? (r.damage / maxDamage) * 100 : 0;
      const empty = r.kills == null && r.damage == null ? " is-empty" : "";
      const main = r.name === state.mainLegend ? " is-main" : "";
      const calBadge = r.calibrated
        ? `<span class="cal-badge" title="EA 对账修正：击杀 ${r.rawKills == null ? "—" : r.rawKills} → ${fmtCompact(r.kills)}，伤害 ${r.rawDamage == null ? "—" : r.rawDamage} → ${fmtCompact(r.damage)}">校准</span>`
        : "";
      return `<div class="legend-row${empty}${main}">
        <div class="legend-name">${legendCn(r.name)}${calBadge}</div>
        <div class="legend-metric"><div class="bar-track"><div class="bar bar-kills" style="width:${kpct}%"></div></div><span class="metric-val">${fmtCompact(r.kills)}</span></div>
        <div class="legend-metric"><div class="bar-track"><div class="bar bar-damage" style="width:${dpct}%"></div></div><span class="metric-val">${fmtCompact(r.damage)}</span></div>
      </div>`;
    }).join("");
    els.legendsList.innerHTML = head + body;
  }

  function renderFeatured(leg, allTrackers, actMap) {
    if (!leg) { els.featuredLegend.hidden = true; return; }
    const art = leg.img ? `<div class="featured-art" style="background-image:url('${proxyImg(leg.img)}')"></div>` : "";
    const act = (actMap || {})[leg.legend] || {};
    const k7 = act.kills ? "+" + act.kills : "—";
    const d7 = act.damage ? "+" + fmtCompact(act.damage) : "—";
    els.featuredLegend.hidden = false;
    els.featuredLegend.innerHTML = `
      ${art}
      <div class="featured-overlay">
        <div class="featured-head">
          <span class="featured-name">${legendCn(leg.legend)}</span>
          <span class="featured-tag">主打</span>
        </div>
        <div class="featured-stats">
          <div class="featured-stat"><span class="fs-num">${k7}</span><span class="fs-label">近7天击杀</span></div>
          <div class="featured-stat"><span class="fs-num">${d7}</span><span class="fs-label">近7天伤害</span></div>
        </div>
      </div>`;
  }

  function setDeltaEl(node, value) {
    node.classList.remove("is-up", "is-down", "is-flat");
    if (value == null) { node.textContent = ""; return; }
    if (value === 0) { node.textContent = "· 今日"; node.classList.add("is-flat"); return; }
    const up = value > 0;
    node.classList.add(up ? "is-up" : "is-down");
    node.textContent = `${up ? "▲" : "▼"} ${fmtCompact(Math.abs(value))} 今日`;
  }
  const setDelta = (metric, value) => {
    const node = document.querySelector(`[data-delta="${metric}"]`);
    if (node) setDeltaEl(node, value);
  };

  // 今日战报大数字
  function setToday(metric, value) {
    const node = document.querySelector(`[data-today="${metric}"]`);
    if (!node) return;
    node.classList.remove("is-up", "is-down", "is-flat");
    if (value == null) { node.textContent = "—"; node.classList.add("is-flat"); return; }
    if (value === 0) { node.textContent = "0"; node.classList.add("is-flat"); return; }
    const up = value > 0;
    node.classList.add(up ? "is-up" : "is-down");
    node.textContent = (up ? "+" : "−") + fmtCompact(Math.abs(value));
  }

  async function loadCurrent() { renderCurrent(await API.getCurrent(state.playerId)); }

  function dayKey(iso) {
    const d = parseTs(iso);
    return d ? `${d.getMonth() + 1}/${d.getDate()}` : iso;
  }
  // 简单每日增量（击杀/伤害用，不涉及赛季重置）
  function simpleDailyDeltas(points) {
    const byDay = {};
    for (const p of points) {
      const k = dayKey(p.captured_at);
      if (!(k in byDay)) byDay[k] = { first: p.value, last: p.value };
      byDay[k].last = p.value;
    }
    return Object.entries(byDay).map(([day, v]) => ({ day, delta: (v.last ?? 0) - (v.first ?? 0) }));
  }

  // 每日归零累计增量折线：每天减去当天第一个值，午夜自动归零
  function dailyGainLine(points) {
    const dayFirst = {};
    for (const p of points) {
      const k = dayKey(p.captured_at);
      if (!(k in dayFirst) && p.value != null) dayFirst[k] = p.value;
    }
    return points.map((p) => {
      const base = dayFirst[dayKey(p.captured_at)] || 0;
      return { captured_at: p.captured_at, value: p.value == null ? null : p.value - base };
    });
  }

  function dailyDeltas(points) {
    // 按天累加"同赛季且非0"的步长 —— 跳过赛季重置(跨赛季)和定级(涉及0的步)
    const byDay = {};
    for (let i = 1; i < points.length; i++) {
      const prev = points[i - 1], p = points[i];
      const k = dayKey(p.captured_at);
      if (dayKey(prev.captured_at) !== k) continue;
      if (!(k in byDay)) byDay[k] = 0;
      const sameSeason = !p.ranked_season || !prev.ranked_season || p.ranked_season === prev.ranked_season;
      const noZero = (prev.value || 0) > 0 && (p.value || 0) > 0;
      if (sameSeason && noZero && p.value != null && prev.value != null) {
        byDay[k] += p.value - prev.value;
      }
    }
    return Object.entries(byDay).map(([day, delta]) => ({ day, delta }));
  }

  async function loadTrend() {
    const cfg = TREND_METRICS[state.metric] || TREND_METRICS.damage;
    const hist = await API.getHistory(state.playerId, cfg.hist, 7);
    let points = hist.points || [];
    const showEmpty = points.length === 0;
    els.trendChart.style.display = showEmpty ? "none" : "block";
    els.trendEmpty.hidden = !showEmpty;
    if (showEmpty) return;
    if (cfg.mode === "daily") {
      const deltas = cfg.seasonAware ? dailyDeltas(points) : simpleDailyDeltas(points);
      Charts.dailyBar("trend-chart", deltas, cfg.label);
      return;
    }
    if (cfg.mode === "dailyGain") {
      // 每天从0开始的累计增量折线：有变化往上走，午夜归零
      Charts.trend("trend-chart", dailyGainLine(points), cfg.label);
      return;
    }
    Charts.trend("trend-chart", points, cfg.label);
  }

  async function loadLegends() {
    const data = await API.getLegends(state.playerId);
    state.calibration = data.calibration || {};
    state.legendRows = applyCalibration(extractLegendStats(data.legends), state.calibration);
    let mk = 0, md = 0;
    for (const c of Object.values(state.calibration)) { mk += c.missing_kills || 0; md += c.missing_damage || 0; }
    state.calibTotals = { kills: mk, damage: md };
    if (state.cur) renderKpis(state.cur);  // KPI 合计同步补上缺失量
    renderLegendList();
  }
  function renderLegendList() {
    const rows = state.legendRows;
    if (!rows || !rows.length) { els.legendsList.innerHTML = ""; els.legendsEmpty.hidden = false; return; }
    els.legendsEmpty.hidden = true;
    renderLegendRows(rows);
  }

  async function loadRank() {
    const hist = await API.getHistory(state.playerId, "rank_score", 30);
    const points = hist.points || [];
    const showEmpty = points.length < 2;
    els.rankChart.style.display = showEmpty ? "none" : "block";
    els.rankEmpty.hidden = !showEmpty;
    if (!showEmpty) Charts.rank("rank-chart", points);
  }

  async function loadDeltas() {
    const d = await API.getDeltas(state.playerId, 1);
    const today = d.deltas || {};
    // 今日无变化 → 用昨日数据兜底（UI 标注"昨日"）
    const hasActivity = Object.values(today).some((v) => v != null && v !== 0);
    const showData = hasActivity ? today : (d.yesterday || today);
    const isYesterday = !hasActivity && d.yesterday != null;
    // KPI 小徽章（始终显示今日）
    setDelta("level", today.level); setDelta("kills", today.kills);
    setDelta("damage", today.damage);
    setDeltaEl(els.bannerDeltaRp, today.rank_score);
    // 今日/昨日战报大数字
    els.todayReport.hidden = false;
    els.todayReport.classList.toggle("is-yesterday", isYesterday);
    setToday("kills", showData.kills); setToday("damage", showData.damage);
    setToday("rank_score", showData.rank_score);
    const labels = els.todayReport.querySelectorAll(".today-label");
    const prefix = isYesterday ? "昨日" : "今日";
    if (labels.length >= 3) {
      labels[0].textContent = `${prefix}击杀`;
      labels[1].textContent = `${prefix}伤害`;
      labels[2].textContent = `${prefix} RP`;
    }
  }

  async function loadBreakdown() {
    const b = await API.getBreakdown(state.playerId);
    // 武器精通（账号级，各传奇共用）
    const wshow = b.weapons.length > 0;
    els.weaponChart.style.display = wshow ? "block" : "none";
    els.weaponEmpty.hidden = wshow;
    if (wshow) Charts.weaponBar("weapon-chart", b.weapons.map((w) => ({ name: trackerCn(w.name), value: w.value })));
    // 武器伤害
    const wdshow = (b.weapon_damage || []).length > 0;
    els.weaponDamageChart.style.display = wdshow ? "block" : "none";
    els.weaponDamageEmpty.hidden = wdshow;
    if (wdshow) Charts.weaponBar("weapon-damage-chart", b.weapon_damage.map((w) => ({ name: trackerCnDamage(w.name), value: w.value })));
    // 主打传奇按校准后的击杀重新排序（陈旧追踪器会低估场次多的传奇）
    const cal = state.calibration || {};
    const ranked = [...(b.legends || [])].map((l) => {
      const c = cal[l.legend];
      return c ? {
        ...l,
        kills: c.calibrated_kills != null ? c.calibrated_kills : l.kills,
        damage: c.calibrated_damage != null ? c.calibrated_damage : l.damage,
      } : l;
    });
    ranked.sort((a, b2) => (b2.kills || 0) - (a.kills || 0));
    const featured = ranked[0];
    state.mainLegend = featured ? featured.legend : null;
    const act7 = await API.getLegendActivity(state.playerId, 7);
    const actMap = {};
    for (const a of act7) actMap[a.legend] = a;
    renderFeatured(featured, b.trackers || [], actMap);
    renderLegendList();
  }

  async function loadLegendActivity() {
    const rows = await API.getLegendActivity(state.playerId, 1);
    const show = rows.length > 0;
    els.activityList.innerHTML = "";
    els.activityEmpty.hidden = show;
    if (!show) return;
    const maxDmg = Math.max(1, ...rows.map((r) => r.damage));
    els.activityList.innerHTML = rows.map((r) => `
      <div class="activity-row">
        <div class="activity-name">${legendCn(r.legend)}</div>
        <div class="activity-metric"><div class="bar-track"><div class="bar bar-damage" style="width:${(r.damage / maxDmg) * 100}%"></div></div><span class="metric-val">${fmtCompact(r.damage)} 伤</span></div>
        <div class="activity-kills">${r.kills} 杀</div>
      </div>`).join("");
  }

  async function loadLegendDaily() {
    const data = await API.getLegendDaily(state.playerId, 7);
    const show = data.length > 0;
    els.recordsEmpty.hidden = show;
    if (!show) { els.recordsTable.innerHTML = ""; return; }
    const dates = data[0].days.map((d) => d.date.slice(5)); // MM-DD
    let html = `<thead><tr><th>传奇</th>${dates.map((dd) => `<th>${dd}</th>`).join("")}</tr></thead><tbody>`;
    for (const row of data) {
      html += `<tr><td class="rec-name">${legendCn(row.legend)}</td>`;
      for (const d of row.days) {
        if (d.kills || d.damage) {
          html += `<td><span class="rec-k">${d.kills}杀</span><span class="rec-d">${fmtCompact(d.damage)}</span></td>`;
        } else {
          html += `<td><span class="rec-na">—</span></td>`;
        }
      }
      html += "</tr>";
    }
    els.recordsTable.innerHTML = html + "</tbody>";
  }

  async function loadAll() {
    try { await Promise.all([loadCurrent(), loadTrend(), loadLegends(), loadRank(), loadDeltas(), loadBreakdown(), loadLegendActivity(), loadLegendDaily()]); }
    catch (err) { showToast(`错误：${err.message}`); }
  }

  function setMetric(metric) {
    state.metric = metric;
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("is-active", c.dataset.metric === metric));
    loadTrend().catch((e) => showToast(`错误：${e.message}`));
  }

  async function init() {
    document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => setMetric(chip.dataset.metric)));
    els.refresh.addEventListener("click", () => {
      if (state.playerId == null) return;
      loadCurrent().then(() => showToast("已刷新")).catch((e) => showToast(`错误：${e.message}`));
    });
    els.select.addEventListener("change", () => {
      state.playerId = Number(els.select.value) || null;
      if (state.playerId) loadAll();
    });
    window.addEventListener("resize", Charts.resizeAll);
    try { state.players = await API.listPlayers(); }
    catch (e) { els.select.innerHTML = `<option value="">加载玩家失败</option>`; showToast(`错误：${e.message}`); return; }
    if (!state.players.length) { els.select.innerHTML = `<option value="">暂无追踪玩家</option>`; return; }
    els.select.innerHTML = state.players.map((p) => `<option value="${p.id}">${p.display_name || p.username || p.uid} · ${p.platform}</option>`).join("");
    state.playerId = state.players[0].id;
    loadAll();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
