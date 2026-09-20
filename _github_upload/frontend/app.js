/* ==============================================================
   app.js — 商品描述文案助手 · 前端主逻辑
   ============================================================== */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const S = {
    platforms: [],
    styles: [],
    platform: 'taobao',
    style: 'auto',
    n: 3,
    sp: [],
    minLen: null,
    maxLen: null,
    useRefs: true,
    current: null,     // 最近一次生成结果
    historyTab: 'all',
    busy: false,
  };

  const LS_KEY = 'copy_assistant_form_v1';

  // ---------------------------------------------------------------- 工具
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = (d && d.detail && (d.detail.message || d.detail)) || d.message || ('请求失败 HTTP ' + r.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return d;
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add('show'));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => { el.hidden = true; }, 220);
    }, 1800);
  }

  function saveForm() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        title: $('#title').value, sp: S.sp, audience: $('#audience').value,
        priceInfo: $('#priceInfo').value, platform: S.platform, style: S.style, n: S.n,
        minLen: $('#minLen').value, maxLen: $('#maxLen').value, useRefs: S.useRefs,
      }));
    } catch (e) { /* ignore */ }
  }

  function loadForm() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      if (d.title) $('#title').value = d.title;
      if (d.audience) $('#audience').value = d.audience;
      if (d.priceInfo) $('#priceInfo').value = d.priceInfo;
      if (d.platform) S.platform = d.platform;
      if (d.style) S.style = d.style;
      if (d.n) S.n = d.n;
      if (d.minLen) $('#minLen').value = d.minLen;
      if (d.maxLen) $('#maxLen').value = d.maxLen;
      if (d.sp && d.sp.length) { S.sp = d.sp; renderSp(); }
      S.useRefs = d.useRefs !== false;
      $('#useRefs').checked = S.useRefs;
    } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- 初始化
  async function boot() {
    loadForm();

    // 进入生成界面前, 先过「模型通道选择页」
    if (window.Gate) {
      try {
        const sel = await Gate.run();
        if (sel) LLM.setPreferred(sel);
      } catch (e) {
        // 选择页出问题不应挡住整个应用, 退回自动探测
        console.warn('通道选择页异常, 回退自动探测:', e);
      }
    }

    await Promise.all([loadMeta(), loadHealth()]);
    renderPlatforms();
    renderStyles();
    syncCountSeg();
    bindEvents();
    refreshFavCount();
  }

  async function loadMeta() {
    try {
      const [p, s] = await Promise.all([api('/api/platforms'), api('/api/styles')]);
      S.platforms = p.items || [];
      S.styles = s.items || [];
      if (!S.platforms.some((x) => x.id === S.platform)) S.platform = S.platforms[0] ? S.platforms[0].id : 'taobao';
      if (!S.styles.some((x) => x.id === S.style)) S.style = 'auto';
    } catch (e) {
      S.platforms = [{ id: 'taobao', name: '淘宝/天猫', icon: '袋', desc: '', length: [70, 140] }];
      S.styles = [{ id: 'auto', name: '自动匹配', desc: '' }];
    }
  }

  async function loadHealth() {
    const cIndex = $('#chipIndex'), cModel = $('#chipModel');
    try {
      const h = await api('/api/health');
      const r = h.retriever || {};
      if (r.ready) {
        cIndex.className = 'chip chip-ok';
        cIndex.innerHTML = '<i></i>语料 ' + fmtNum(r.titles) + ' 条标题';
      } else {
        cIndex.className = 'chip chip-err';
        cIndex.innerHTML = '<i></i>索引未就绪';
        cIndex.title = r.error || '';
      }
    } catch (e) {
      cIndex.className = 'chip chip-err';
      cIndex.innerHTML = '<i></i>后端未连接';
    }

    const st = await LLM.detect();
    const pf = LLM.preferred;
    if (st.channel === 'none') {
      cModel.className = 'chip chip-warn';
      cModel.innerHTML = '<i></i>无生成通道';
      cModel.title = st.detail;
    } else {
      cModel.className = 'chip chip-ok';
      cModel.innerHTML = '<i></i>' + esc(st.channel === 'cloud' ? '平台模型' : '服务端模型');
      cModel.title = st.detail;
    }
    const via = pf ? (pf.label || '') + (pf.model ? ' · ' + pf.model : '') : '';
    cModel.title = (st.detail || '') + (via ? '　（' + via + '）' : '') + '　— 点击切换模型通道';
    cModel.classList.add('chip-btn');
  }

  /** 顶栏模型 chip: 重新打开通道选择页 */
  async function onSwitchModel() {
    if (!window.Gate) return;
    let sel = null;
    try {
      sel = await Gate.open();
    } catch (e) {
      toast('打开通道选择页失败：' + (e.message || e));
      return;
    }
    if (!sel) return;
    LLM.setPreferred(sel);
    await loadHealth();
    toast('已切换到 ' + (sel.label || '新通道') + (sel.model ? ' · ' + sel.model : ''));
  }

  function fmtNum(n) {
    if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
    return String(n);
  }

  // ---------------------------------------------------------------- 渲染
  function renderPlatforms() {
    const g = $('#platformGrid');
    g.innerHTML = S.platforms.map((p) => `
      <button class="platform-card ${p.id === S.platform ? 'active' : ''}" data-id="${esc(p.id)}" title="${esc(p.desc)}">
        <span class="pc-top"><span class="pc-icon">${esc(p.icon || '·')}</span><span class="pc-name">${esc(p.name)}</span></span>
        <span class="pc-len">${p.length ? p.length[0] + '-' + p.length[1] + ' 字' : ''}</span>
      </button>`).join('');
    $$('.platform-card', g).forEach((b) => {
      b.onclick = () => { S.platform = b.dataset.id; renderPlatforms(); saveForm(); };
    });
  }

  function renderStyles() {
    const r = $('#styleRow');
    r.innerHTML = S.styles.map((s) => `
      <button class="chip-opt ${s.id === S.style ? 'active' : ''}" data-id="${esc(s.id)}" title="${esc(s.desc)}">${esc(s.name)}</button>
    `).join('');
    $$('.chip-opt', r).forEach((b) => {
      b.onclick = () => { S.style = b.dataset.id; renderStyles(); saveForm(); };
    });
  }

  function renderSp() {
    const w = $('#spWrap');
    $$('.tag', w).forEach((t) => t.remove());
    const inp = $('#spInput');
    S.sp.forEach((v, i) => {
      const t = document.createElement('span');
      t.className = 'tag';
      t.innerHTML = esc(v) + '<button title="移除">✕</button>';
      t.querySelector('button').onclick = () => { S.sp.splice(i, 1); renderSp(); saveForm(); };
      w.insertBefore(t, inp);
    });
  }

  function syncCountSeg() {
    $$('#countSeg button').forEach((b) => b.classList.toggle('active', Number(b.dataset.v) === S.n));
  }

  // ---------------------------------------------------------------- 事件
  function bindEvents() {
    $('#title').addEventListener('input', saveForm);
    $('#audience').addEventListener('input', saveForm);
    $('#priceInfo').addEventListener('input', saveForm);
    $('#minLen').addEventListener('input', saveForm);
    $('#maxLen').addEventListener('input', saveForm);
    $('#useRefs').addEventListener('change', (e) => { S.useRefs = e.target.checked; saveForm(); });

    const spInput = $('#spInput');
    spInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
        e.preventDefault();
        const v = spInput.value.trim().replace(/[,，]/g, '');
        if (v && S.sp.length < 10 && !S.sp.includes(v)) { S.sp.push(v); renderSp(); saveForm(); }
        spInput.value = '';
      } else if (e.key === 'Backspace' && !spInput.value && S.sp.length) {
        S.sp.pop(); renderSp(); saveForm();
      }
    });
    spInput.addEventListener('blur', () => {
      const v = spInput.value.trim().replace(/[,，]/g, '');
      if (v && S.sp.length < 10 && !S.sp.includes(v)) { S.sp.push(v); renderSp(); saveForm(); spInput.value = ''; }
    });

    $$('#countSeg button').forEach((b) => {
      b.onclick = () => { S.n = Number(b.dataset.v); syncCountSeg(); saveForm(); };
    });

    $('#btnGenerate').onclick = onGenerate;

    $('#btnExample').onclick = () => {
      $('#title').value = '2024春秋新款男士连帽卫衣 宽松潮流纯棉情侣外套';
      S.sp = ['纯棉亲肤', '宽松显瘦', '情侣款'];
      renderSp();
      $('#audience').value = '18-28岁学生党/年轻上班族';
      $('#priceInfo').value = '';
      saveForm();
      toast('已填入示例，可直接点生成');
    };

    document.addEventListener('keydown', (e) => {
      // 选择页打开时不要触发下面的生成快捷键
      if (window.Gate && Gate.isOpen()) return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); onGenerate(); }
      if (e.key === 'Escape') closeDrawer();
    });

    $('#refsToggle').onclick = () => {
      const blk = $('#refsBlock'), body = $('#refsBody');
      const open = blk.classList.toggle('open');
      body.hidden = !open;
    };

    $('#btnHistory').onclick = openDrawer;
    $('#chipModel').onclick = onSwitchModel;
    $('#btnCloseDrawer').onclick = closeDrawer;
    $('#scrim').onclick = closeDrawer;
    $$('.tab').forEach((t) => {
      t.onclick = () => {
        S.historyTab = t.dataset.tab;
        $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
        loadHistory();
      };
    });
    let sTimer = null;
    $('#historySearch').addEventListener('input', () => {
      clearTimeout(sTimer);
      sTimer = setTimeout(loadHistory, 260);
    });
  }

  // ---------------------------------------------------------------- 进度条
  // 进度不是假的: 完全由后端 SSE 推来的真实事件驱动。
  //   retrieve 完成        -> 10%           (真实命中数/参照数)
  //   think  阶段          -> 按 reasoning_content 真实累计字数推进
  //   write  阶段          -> 按 content 真实累计字数推进
  //   parse  阶段          -> 90%
  //   done                 -> 100%
  // 显示值用 rAF 平滑逼近目标值, 并且只增不减, 长等待期间也不会看起来像死机。
  const STAGE_LABEL = {
    retrieve: '检索同类真实文案',
    think: '模型思考中',
    write: '正在撰写文案',
    parse: '解析与合规检查',
  };

  // 经验常数(实测 deepseek-flash): 单次思考约 900~1500 字; 文案按 115 字/条估总量
  const THINK_FULL_CHARS = 1300;
  const CHARS_PER_ITEM = 115;

  const P = {
    active: false,
    raf: 0,
    hideTimer: null,
    prefix: '',
    stage: '',
    target: 0,
    shown: 0,
    reasonChars: 0,
    contentChars: 0,
    n: 3,
    retrieveDone: false,
    refsCount: 0,
    hitsCount: 0,
    finished: false,
    t0: 0,
  };

  function fmtInt(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function fmtSec(ms) {
    return (ms / 1000).toFixed(1) + 's';
  }

  // 阶段允许到达的进度上限, 保证任何阶段都不会自行爬到 100%
  function stageCeil() {
    if (P.finished) return 1;
    if (P.stage === 'parse') return 0.985;
    if (P.stage === 'think' || P.stage === 'write') return 0.9;
    if (!P.retrieveDone) return 0.08;
    return 0.1;
  }

  // 当前状态折算出的目标进度(0~1)
  function progressTarget() {
    if (P.finished) return 1;
    if (P.stage === 'parse') return 0.9;
    if (!P.retrieveDone) return 0.04;
    if (!P.stage || P.stage === 'retrieve') return 0.1;

    const LO = 0.1, SPAN = 0.8;
    let g;
    if (P.stage === 'think') {
      // 思考最多折算到生成段的 45%
      g = Math.min(0.45, (P.reasonChars / THINK_FULL_CHARS) * 0.45);
    } else if (P.stage === 'write') {
      const expect = Math.max(120, P.n * CHARS_PER_ITEM);
      g = 0.45 + 0.55 * Math.min(1, P.contentChars / expect);
    } else {
      g = 0;
    }
    return LO + SPAN * Math.min(g, 0.97);
  }

  function renderProgress() {
    $('#gpBar').style.width = (P.shown * 100).toFixed(2) + '%';
    $('#gpPct').textContent = Math.round(P.shown * 100) + '%';

    const bits = [];
    if (P.reasonChars) bits.push('思考 <b>' + fmtInt(P.reasonChars) + '</b> 字');
    if (P.contentChars) bits.push('已写 <b>' + fmtInt(P.contentChars) + '</b> 字');
    if (P.retrieveDone && P.refsCount) bits.push('参照 <b>' + P.refsCount + '</b> 条');
    if (P.t0) bits.push('已用 <b>' + fmtSec(Date.now() - P.t0) + '</b>');
    $('#gpMeta').innerHTML = bits.join('');
  }

  function progressLoop() {
    if (!P.active) return;
    const t = progressTarget();
    if (t > P.target) P.target = t;              // 目标单调不减
    const cap = Math.min(stageCeil(), P.target + 0.03);
    P.shown += (cap - P.shown) * 0.12;           // 平滑逼近
    if (!P.finished && P.shown > cap) P.shown = cap;
    renderProgress();
    P.raf = requestAnimationFrame(progressLoop);
  }

  function progressStart(n, prefix) {
    progressReset();
    P.active = true;
    P.prefix = prefix || '';
    P.stage = '';
    P.target = 0;
    P.shown = 0;
    P.reasonChars = 0;
    P.contentChars = 0;
    P.n = n || 3;
    P.retrieveDone = false;
    P.refsCount = 0;
    P.hitsCount = 0;
    P.finished = false;
    P.t0 = Date.now();

    const el = $('#genProgress');
    el.hidden = false;
    el.classList.remove('done', 'error');
    $('#gpStage').textContent = '准备中…';
    $('#gpMeta').innerHTML = '';
    renderProgress();
    P.raf = requestAnimationFrame(progressLoop);
  }

  function progressEvent(ev) {
    if (!P.active || !ev) return;
    if (ev.event === 'stage') {
      P.stage = ev.stage;
      $('#gpStage').textContent = P.prefix + (ev.label || STAGE_LABEL[ev.stage] || '生成中');
    } else if (ev.event === 'stage_done' && ev.stage === 'retrieve') {
      P.retrieveDone = true;
      P.hitsCount = ev.hits || 0;
      P.refsCount = ev.refs || 0;
    } else if (ev.event === 'tick') {
      P.reasonChars = ev.reasoning_chars || 0;
      P.contentChars = ev.content_chars || 0;
    } else if (ev.event === 'retry') {
      // 新一轮尝试, 计数从 0 重新开始; 目标进度因单调不减会保持住
      P.reasonChars = 0;
      P.contentChars = 0;
      $('#gpStage').textContent = P.prefix + '输出被截断，自动重试（第 ' + ev.attempt + ' 次）';
    }
  }

  function progressFinish(ok) {
    if (!P.active) return;
    P.active = false;
    P.finished = true;
    if (P.raf) { cancelAnimationFrame(P.raf); P.raf = 0; }
    const el = $('#genProgress');
    el.classList.add(ok ? 'done' : 'error');
    $('#gpStage').textContent = ok ? '生成完成' : '生成中断';
    if (ok) {
      P.shown = 1;
      renderProgress();
      clearTimeout(P.hideTimer);
      P.hideTimer = setTimeout(() => {
        if (!P.active && P.finished) { el.hidden = true; el.classList.remove('done'); }
      }, 700);
    } else {
      // 失败时保留进度条, 让用户看到卡在哪个阶段
      renderProgress();
    }
  }

  function progressReset() {
    clearTimeout(P.hideTimer);
    P.hideTimer = null;
    if (P.raf) { cancelAnimationFrame(P.raf); P.raf = 0; }
    P.active = false;
    P.finished = false;
    P.shown = 0;
    P.target = 0;
    const el = $('#genProgress');
    if (el) {
      el.hidden = true;
      el.classList.remove('done', 'error');
    }
  }

  // ---------------------------------------------------------------- 生成
  function collectReq(extra) {
    const req = {
      title: $('#title').value.trim(),
      platform: S.platform,
      style: S.style,
      n: S.n,
      selling_points: S.sp.slice(),
      audience: $('#audience').value.trim(),
      price_info: $('#priceInfo').value.trim(),
      use_references: S.useRefs,
    };
    const mn = parseInt($('#minLen').value, 10);
    const mx = parseInt($('#maxLen').value, 10);
    if (!isNaN(mn) && !isNaN(mx) && mn < mx) { req.min_len = mn; req.max_len = mx; }
    return Object.assign(req, extra || {});
  }

  function setBusy(b) {
    S.busy = b;
    const btn = $('#btnGenerate');
    btn.disabled = b;
    btn.classList.toggle('loading', b);
    $('.btn-label', btn).textContent = b ? '正在生成…' : '生成文案';
    if (b) {
      $('#emptyState').hidden = true;
      $('#cards').innerHTML = '';
      $('#refsBlock').hidden = true;
      $('#genError').classList.remove('show');
      $('#warnBanner').hidden = true;
      $('#skeleton').hidden = false;
    } else {
      $('#skeleton').hidden = true;
    }
  }

  function showError(msg) {
    const el = $('#genError');
    el.textContent = '生成失败：' + msg;
    el.classList.add('show');
    if (!S.current) $('#emptyState').hidden = false;
  }

  /**
   * 服务端通道生成: 优先走 SSE 流式接口(带真实阶段进度),
   * 只有在"流式通道本身不可用"时才回退到一次性接口。
   * 已经生成到一半再失败的不回退, 否则等于白烧一遍 token。
   */
  async function generateViaServer(req, onEvent) {
    try {
      return await LLM.chatServerStream(req, onEvent);
    } catch (e) {
      if (!e || !e.streamUnavailable) throw e;
      if (onEvent) onEvent({ event: 'stage', stage: 'write', label: '模型生成中（非流式）' });
      return await LLM.chatServer(req);
    }
  }

  async function onGenerate() {
    if (S.busy) return;
    const title = $('#title').value.trim();
    if (title.length < 2) { toast('请先填写商品标题'); $('#title').focus(); return; }

    setBusy(true);
    progressStart(S.n, '');
    const t0 = Date.now();
    try {
      const st = await LLM.detect();
      let res;
      if (st.channel === 'cloud') {
        const prep = await api('/api/prepare', { method: 'POST', body: JSON.stringify(collectReq()) });
        progressEvent({ event: 'stage_done', stage: 'retrieve', hits: (prep.hits || []).length, refs: (prep.references || []).length });
        progressEvent({ event: 'stage', stage: 'write', label: '平台模型生成中' });
        let raw = '';
        let acc = 0;
        try {
          // chatCloud 本身就是流式, 可以拿真实增量驱动进度
          raw = await LLM.chatCloud(prep.messages, (delta) => {
            acc += delta.length;
            progressEvent({ event: 'tick', reasoning_chars: 0, content_chars: acc });
          });
        } catch (e) {
          throw new Error('平台模型调用失败：' + (e.message || e));
        }
        progressEvent({ event: 'stage', stage: 'parse', label: '解析与合规检查' });
        res = await api('/api/finalize', {
          method: 'POST',
          body: JSON.stringify({
            raw, expect: S.n, params: prep.params, references: prep.references,
            hits: prep.hits, engine: 'cloud', model: LLM.modelName, latency_ms: Date.now() - t0,
          }),
        });
      } else if (st.channel === 'server') {
        res = await generateViaServer(collectReq(), progressEvent);
      } else {
        throw new Error('未检测到可用的生成通道。请配置平台模型、后端 LLM_API_KEY 或启动本地 Ollama。');
      }
      // 一条都没解析出来时, 进度条走失败态, 与下方错误提示保持一致
      progressFinish(!!(res && res.items && res.items.length));
      renderResult(res);
    } catch (e) {
      progressFinish(false);
      showError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  function renderResult(res) {
    S.current = res;
    const items = res.items || [];
    if (!items.length) {
      $('#cards').innerHTML = '';
      $('#emptyState').hidden = false;
      $('#warnBanner').hidden = true;
      showError((res.warnings && res.warnings[0]) || '模型未返回可解析的文案');
      return;
    }
    $('#emptyState').hidden = true;
    renderWarnings(res.warnings || []);
    renderCards(items, res.references || []);
    renderRefs(res.hits || [], res.references || []);

    const meta = [];
    if (res.engine) meta.push(esc(res.engine.replace('server:', '')));
    if (res.model) meta.push(esc(res.model));
    if (res.latency_ms) meta.push((res.latency_ms / 1000).toFixed(1) + ' s');
    meta.push(items.length + ' 条');
    $('#resultMeta').innerHTML = meta.map((m) => `<span class="meta-tag">${m}</span>`).join('');
  }

  function gradeLabel(g) {
    return { ok: '合规', mid: '需留意', high: '风险', low: '提示' }[g] || '提示';
  }

  function renderWarnings(list) {
    const el = $('#warnBanner');
    if (!list.length) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.innerHTML = list.map((w) => `<div><span>⚠</span><span>${esc(w)}</span></div>`).join('') +
      '<button class="warn-close">知道了</button>';
    $('.warn-close', el).onclick = () => { el.hidden = true; };
  }

  function renderCards(items, refs) {
    const box = $('#cards');
    box.innerHTML = items.map((it, i) => `
      <div class="copy-card" data-i="${i}">
        <div class="cc-head">
          <span class="cc-angle">${esc(it.angle || '文案')}</span>
          <span class="cc-len">${it.char_count || it.text.length} 字</span>
          <span class="cc-actions">
            <button class="icon-btn act-again" title="换一条">↻</button>
            <button class="icon-btn act-copy" title="复制">⧉</button>
            <button class="icon-btn act-fav" title="收藏到历史">☆</button>
          </span>
        </div>
        <div class="cc-text">${esc(it.text)}</div>
        <div class="cc-foot">
          <span class="grade grade-${esc(it.grade || 'ok')}">${gradeLabel(it.grade)}</span>
          ${(it.issues || []).length ? '<div class="issue-list">' + it.issues.map((x) =>
            `<div class="issue ${esc(x.level)}">⚠ <span><b>${esc(x.word)}</b> ${esc(x.msg)}</span></div>`).join('') + '</div>' : ''}
        </div>
      </div>`).join('');

    $$('.copy-card', box).forEach((card) => {
      const i = Number(card.dataset.i);
      $('.act-copy', card).onclick = async () => {
        await copyText(items[i].text);
        toast('已复制到剪贴板');
      };
      $('.act-fav', card).onclick = async () => {
        if (!S.current || !S.current.id) { toast('该结果未入库，无法收藏'); return; }
        try {
          const cur = $('.act-fav', card).classList.contains('on');
          await api(`/api/history/${S.current.id}/favorite?fav=${cur ? 'false' : 'true'}`, { method: 'POST' });
          $('.act-fav', card).classList.toggle('on', !cur);
          card.classList.toggle('fav', !cur);
          $('.act-fav', card).textContent = !cur ? '★' : '☆';
          toast(!cur ? '已加入收藏' : '已取消收藏');
          refreshFavCount();
        } catch (e) { toast('操作失败：' + e.message); }
      };
      $('.act-again', card).onclick = () => onRegenerateOne(card, i);
    });
  }

  async function onRegenerateOne(card, idx) {
    if (S.busy) return;
    if (!S.current || !S.current.items || S.current.items.length < 2) { toast('只有一条文案，直接重新生成即可'); return; }
    const others = S.current.items.filter((_, i) => i !== idx).map((x) => x.text);
    S.busy = true;                 // 只做重入保护: 不能用 setBusy, 它会清空已渲染的卡片
    card.style.opacity = '.45';
    progressStart(1, '换一条 · ');
    try {
      const st = await LLM.detect();
      let res;
      if (st.channel === 'cloud') {
        const prep = await api('/api/prepare', { method: 'POST', body: JSON.stringify(collectReq({ n: 1, avoid: others })) });
        progressEvent({ event: 'stage_done', stage: 'retrieve', hits: (prep.hits || []).length, refs: (prep.references || []).length });
        progressEvent({ event: 'stage', stage: 'write', label: '平台模型生成中' });
        let acc = 0;
        const raw = await LLM.chatCloud(prep.messages, (delta) => {
          acc += delta.length;
          progressEvent({ event: 'tick', reasoning_chars: 0, content_chars: acc });
        });
        progressEvent({ event: 'stage', stage: 'parse', label: '解析与合规检查' });
        res = await api('/api/finalize', {
          method: 'POST',
          body: JSON.stringify({ raw, expect: 1, params: prep.params, references: prep.references, hits: prep.hits, engine: 'cloud', model: LLM.modelName, save: false }),
        });
      } else if (st.channel === 'server') {
        res = await generateViaServer(collectReq({ n: 1, avoid: others }), progressEvent);
      } else { throw new Error('无可用生成通道'); }

      const nu = (res.items || [])[0];
      if (!nu) throw new Error((res.warnings && res.warnings[0]) || '未解析出文案');
      S.current.items[idx] = nu;
      progressFinish(true);
      renderCards(S.current.items, S.current.references || []);
      if (res.warnings && res.warnings.length) renderWarnings(res.warnings);
      toast('已换一条');
    } catch (e) {
      progressFinish(false);
      toast('换一条失败：' + e.message);
    } finally {
      card.style.opacity = '';
      S.busy = false;
    }
  }

  async function copyText(t) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(t);
        return;
      }
    } catch (e) { /* fallthrough */ }
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
  }

  function renderRefs(hits, refs) {
    const blk = $('#refsBlock');
    if (!hits.length && !refs.length) { blk.hidden = true; return; }
    blk.hidden = false;
    $('#refsCount').textContent = refs.length + ' 条 / ' + hits.length + ' 个相似商品';

    const html = hits.map((h) => `
      <div class="ref-group">
        <div class="ref-title">
          <span class="tag">${esc(h.cat || '未分类')}</span>
          <span>${esc(h.title)}</span>
          <span class="pct">相似 ${Math.round((h.similarity || 0) * 100)}%</span>
        </div>
        ${(h.descs || []).map((d) => `<div class="ref-desc">${esc(d.desc)}</div>`).join('')}
      </div>`).join('');
    $('#refsBody').innerHTML = html;
  }

  // ---------------------------------------------------------------- 抽屉
  function openDrawer() {
    $('#drawer').classList.add('open');
    $('#drawer').setAttribute('aria-hidden', 'false');
    $('#scrim').hidden = false;
    loadHistory();
  }
  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#drawer').setAttribute('aria-hidden', 'true');
    $('#scrim').hidden = true;
  }

  async function loadHistory() {
    const body = $('#drawerBody');
    body.innerHTML = '<div class="empty-tip">加载中…</div>';
    try {
      const q = encodeURIComponent($('#historySearch').value.trim());
      const d = await api(`/api/history?limit=60&favorite_only=${S.historyTab === 'fav'}&keyword=${q}`);
      if (!d.items || !d.items.length) {
        body.innerHTML = '<div class="empty-tip">' + (S.historyTab === 'fav' ? '还没有收藏' : '还没有生成记录') + '</div>';
        return;
      }
      body.innerHTML = d.items.map((h) => `
        <div class="hist-item" data-id="${esc(h.id)}">
          <div class="hist-top">
            <div class="hist-title">${esc(h.title)}</div>
            <div class="hist-time">${fmtTime(h.created_at)}</div>
          </div>
          <div class="hist-preview">${esc(h.preview || '')}</div>
          <div class="hist-foot">
            <span class="hist-tag">${esc(platformName(h.platform))}</span>
            <span class="hist-tag">${h.n_items} 条</span>
            <span style="flex:1"></span>
            <button class="icon-btn h-fav ${h.favorite ? 'on' : ''}" title="收藏">${h.favorite ? '★' : '☆'}</button>
            <button class="icon-btn h-open" title="查看">↗</button>
            <button class="icon-btn h-del" title="删除">🗑</button>
          </div>
        </div>`).join('');

      $$('.hist-item', body).forEach((el) => {
        const id = el.dataset.id;
        $('.h-fav', el).onclick = async (e) => {
          e.stopPropagation();
          const on = $('.h-fav', el).classList.contains('on');
          await api(`/api/history/${id}/favorite?fav=${on ? 'false' : 'true'}`, { method: 'POST' });
          loadHistory(); refreshFavCount();
        };
        $('.h-open', el).onclick = (e) => { e.stopPropagation(); openHistoryItem(id); };
        $('.h-del', el).onclick = async (e) => {
          e.stopPropagation();
          if (!confirm('确定删除这条记录？')) return;
          await api(`/api/history/${id}`, { method: 'DELETE' });
          loadHistory(); refreshFavCount();
        };
      });
    } catch (e) {
      body.innerHTML = '<div class="empty-tip">加载失败：' + esc(e.message) + '</div>';
    }
  }

  async function openHistoryItem(id) {
    try {
      const d = await api('/api/history/' + id);
      progressReset();
      S.current = { id: d.id, items: d.items, references: d.refs || [], hits: [], engine: d.engine, model: d.model, latency_ms: d.latency_ms };
      $('#emptyState').hidden = true;
      renderCards(d.items, d.refs || []);
      if (d.params) {
        if (d.params.title) $('#title').value = d.params.title;
        S.platform = d.params.platform || S.platform;
        S.style = d.params.style || S.style;
        renderPlatforms(); renderStyles();
      }
      $('#refsBlock').hidden = true;
      $('#resultMeta').innerHTML = '<span class="meta-tag">历史记录</span><span class="meta-tag">' +
        fmtTime(d.created_at) + '</span>';
      closeDrawer();
    } catch (e) { toast('读取失败：' + e.message); }
  }

  async function refreshFavCount() {
    try {
      const s = await api('/api/stats');
      $('#favCount').textContent = (s.usage && s.usage.favorites) || 0;
    } catch (e) { /* ignore */ }
  }

  function platformName(id) {
    const p = S.platforms.find((x) => x.id === id);
    return p ? p.name : (id || '');
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return (sameDay ? '' : (d.getMonth() + 1) + '/' + d.getDate() + ' ') + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
