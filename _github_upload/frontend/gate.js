/* ==============================================================
   gate.js — 进入生成界面前的「模型通道选择页」

   三张卡片:
     本地 Ollama  —— 模型跑在自己机器上, 数据不出内网
     模型 API     —— 任意 OpenAI 兼容接口; 可沿用服务端 .env 的 Key, 也可自己填
     云服务       —— 平台免密钥模型(未开通, 只展示不误导)

   安全约定: 用户自己填的 Key 只落在本机 localStorage, 每次生成时随请求
   发给本地后端临时使用, 不写入服务端文件。界面上明确告知用户这一点。
   ============================================================== */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const STORE_KEY = 'copy_assistant_channel_v1';

  const FALLBACK = {
    openai: { base_url: 'https://api.example.com/v1', model: '' },
    ollama: { base_url: 'http://127.0.0.1:11434', model: 'qwen2.5:7b' },
  };

  const S = {
    info: null,           // /api/channels 的返回
    card: 'api',          // api | ollama | cloud
    keyMode: 'server',    // api 卡片下: server(沿用 .env) | own(自己填)
    saved: null,
    open: false,
    busy: false,
    _resolve: null,
  };

  // ------------------------------------------------------------- 存储
  function loadSaved() {
    try {
      const d = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      return d && typeof d === 'object' ? d : null;
    } catch (e) {
      return null;
    }
  }

  function persist() {
    const data = {
      card: S.card,
      keyMode: S.keyMode,
      autoEnter: $('#gateAuto').checked,
      api: { base_url: $('#apiBaseUrl').value.trim(), key: $('#apiKey').value.trim(), model: $('#apiModel').value.trim() },
      ollama: { base_url: $('#olBaseUrl').value.trim(), model: $('#olModel').value.trim() },
      verifiedAt: Math.floor(Date.now() / 1000),
    };
    try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch (e) { /* ignore */ }
    S.saved = data;
    return data;
  }

  // ------------------------------------------------------------- 渲染
  function fillModels(which, models) {
    const list = which === 'ollama' ? $('#olModelList') : $('#apiModelList');
    if (!list) return;
    list.innerHTML = (models || []).map((m) => '<option value="' + String(m).replace(/"/g, '&quot;') + '"></option>').join('');
  }

  function pickCard() {
    const c = S.card;
    $('#cfgApi').hidden = c !== 'api';
    $('#cfgOllama').hidden = c !== 'ollama';
    $('#cfgCloud').hidden = c !== 'cloud';
    if (c === 'api') $('#ownKeyRow').hidden = S.keyMode !== 'own';
    Array.prototype.forEach.call(document.querySelectorAll('.ch-card'), (el) => {
      el.classList.toggle('active', el.dataset.id === c);
    });
    updateEnterLabel();
  }

  function updateEnterLabel() {
    const btn = $('#gateEnter');
    if (S.busy) return;
    btn.disabled = S.card === 'cloud';
    $('.gbtn-label', btn).textContent = S.card === 'cloud' ? '云服务未开通' : '测试并进入';
  }

  function setStatus(kind, msg) {
    const el = $('#gateStatus');
    if (!kind) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.className = 'gate-status ' + kind;
    el.innerHTML = '<span>' + (kind === 'ok' ? '✓' : kind === 'err' ? '⚠' : '') + '</span><span>' + msg + '</span>';
  }

  function setBusy(b) {
    S.busy = b;
    const btn = $('#gateEnter');
    btn.disabled = b || S.card === 'cloud';
    btn.classList.toggle('loading', b);
    $('.gbtn-label', btn).textContent = b ? '正在连接…' : (S.card === 'cloud' ? '云服务未开通' : '测试并进入');
    $('.gbtn-spinner', btn).hidden = !b;
  }

  // ------------------------------------------------------------- 组装选择
  function buildSelection() {
    if (S.card === 'cloud') return null;
    if (S.card === 'ollama') {
      return {
        provider: 'ollama',
        base_url: $('#olBaseUrl').value.trim() || FALLBACK.ollama.base_url,
        api_key: '',
        model: $('#olModel').value.trim() || FALLBACK.ollama.model,
        label: '本地 Ollama',
      };
    }
    // api 卡片
    const base = $('#apiBaseUrl').value.trim() || FALLBACK.openai.base_url;
    const model = $('#apiModel').value.trim();
    if (S.keyMode === 'server') {
      return { provider: 'server', base_url: base, api_key: '', model: model, label: '服务端已配置 Key' };
    }
    return {
      provider: 'openai',
      base_url: base,
      api_key: $('#apiKey').value.trim(),
      model: model || FALLBACK.openai.model,
      label: '自备 API Key',
    };
  }

  // ------------------------------------------------------------- 测试连通
  async function testChannel(sel) {
    const r = await fetch('/api/channels/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: sel.provider,
        base_url: sel.base_url || '',
        api_key: sel.api_key || '',
        model: sel.model || '',
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((d && d.detail && (d.detail.message || d.detail)) || '探测失败 HTTP ' + r.status);
    return d;
  }

  async function onEnter(force) {
    if (S.busy) return;
    const sel = buildSelection();
    if (!sel) return;

    if (sel.provider === 'openai' && !sel.api_key) {
      setStatus('err', '请填写 API Key，或改为沿用服务端已配置的 Key');
      $('#apiKey').focus();
      return;
    }

    setStatus('warn', '正在连接模型通道…');
    setBusy(true);
    let res = null;
    let err = null;
    try {
      res = await testChannel(sel);
    } catch (e) {
      err = e;
    }
    setBusy(false);

    if (res && res.ok) {
      if (sel.provider === 'ollama') fillModels('ollama', res.models);
      else fillModels('api', res.models);
      setStatus('ok', res.message + (res.latency_ms ? '　·　' + res.latency_ms + ' ms' : ''));
      const data = persist();
      setTimeout(() => resolveOut(Object.assign({}, sel, { verified: true, models: res.models || [] }), data), 420);
      return;
    }

    const msg = err ? err.message : (res && res.message) || '连接失败';
    setStatus('err', msg + (force ? '' : '　（可点下方「仍然进入」跳过检测）'));
    $('#gateForce').hidden = false;
  }

  // ------------------------------------------------------------- 开关
  function resolveOut(sel, data) {
    const fn = S._resolve;
    S._resolve = null;
    hide();
    if (fn) fn(sel);
  }

  function hide() {
    const el = $('#gate');
    el.classList.remove('in');
    S.open = false;
    setTimeout(() => { if (!S.open) el.hidden = true; }, 220);
  }

  // ------------------------------------------------------------- 初始化
  function applySaved(saved) {
    const s = saved || {};
    S.card = s.card === 'ollama' || s.card === 'cloud' ? s.card : 'api';
    S.keyMode = s.keyMode === 'own' ? 'own' : 'server';

    const api = s.api || {};
    const ol = s.ollama || {};
    $('#apiBaseUrl').value = api.base_url || (S.info && S.info.server && S.info.server.base_url) || FALLBACK.openai.base_url;
    $('#apiModel').value = api.model || (S.info && S.info.server && S.info.server.model) || FALLBACK.openai.model;
    $('#apiKey').value = api.key || '';
    $('#olBaseUrl').value = ol.base_url || pickOllamaHint();
    $('#olModel').value = ol.model || (S.info && S.info.ollama && S.info.ollama.model) || FALLBACK.ollama.model;
    $('#keyModeServer').checked = S.keyMode === 'server';
    $('#keyModeOwn').checked = S.keyMode === 'own';
    $('#gateAuto').checked = !!s.autoEnter;
  }

  function pickOllamaHint() {
    const cfg = (S.info && S.info.ollama) || {};
    const base = cfg.base_url || '';
    // .env 默认值是为 Docker 场景写的, 本机直跑时换成 127.0.0.1
    if (!base || base.indexOf('host.docker.internal') >= 0) return cfg.local_hint || FALLBACK.ollama.base_url;
    return base;
  }

  function renderBadges() {
    const info = S.info || {};
    const srv = info.server || {};
    const apiBadge = $('#badgeApi');
    if (srv.available) {
      apiBadge.textContent = '服务端已配好';
      apiBadge.className = 'ch-badge ok';
    } else {
      apiBadge.textContent = '需自备 Key';
      apiBadge.className = 'ch-badge warn';
    }
    $('#badgeOllama').textContent = '需本机已启动';
    $('#badgeOllama').className = 'ch-badge';

    const hint = $('#serverKeyHint');
    hint.textContent = srv.available
      ? '（服务端 .env 已配置：' + (srv.model || '—') + '）'
      : '（服务端未配置 Key，此选项不可用）';
    $('#keyModeServer').disabled = !srv.available;
    if (!srv.available && S.keyMode === 'server') {
      S.keyMode = 'own';
      $('#keyModeOwn').checked = true;
      $('#keyModeServer').checked = false;
    }

    const cloud = info.cloud || {};
    $('#cloudNote').textContent = cloud.reason || '云服务未开通';
  }

  function bindOnce() {
    Array.prototype.forEach.call(document.querySelectorAll('.ch-card'), (el) => {
      el.onclick = () => {
        if (el.disabled) return;
        S.card = el.dataset.id;
        setStatus(null);
        $('#gateForce').hidden = true;
        pickCard();
      };
    });

    $('#keyModeServer').onchange = () => { S.keyMode = 'server'; pickCard(); };
    $('#keyModeOwn').onchange = () => { S.keyMode = 'own'; pickCard(); };

    $('#gateEnter').onclick = () => onEnter(false);
    $('#gateForce').onclick = () => {
      const sel = buildSelection();
      if (!sel) return;
      const data = persist();
      resolveOut(Object.assign({}, sel, { verified: false }), data);
    };

    // 按需拉模型列表(探测成功后填 datalist); 这里再提供一次静默预取
    $('#gateAuto').onchange = () => { persist(); };
  }

  async function loadInfo() {
    try {
      const r = await fetch('/api/channels', { headers: { Accept: 'application/json' } });
      S.info = await r.json();
    } catch (e) {
      S.info = { server: { available: false }, ollama: FALLBACK.ollama, cloud: { available: false, reason: '服务端未响应' } };
    }
  }

  async function ensureInit() {
    if (!$('#gate').dataset.bound) {
      bindOnce();
      $('#gate').dataset.bound = '1';
    }
    if (!S.info) await loadInfo();
  }

  /** 进入页主流程: 返回用户选定的通道。已勾选「下次自动使用」时直接返回上次的选择。 */
  async function run() {
    await ensureInit();
    const saved = loadSaved();
    if (saved && saved.autoEnter) {
      const sel = selectionFromSaved(saved);
      if (sel) return sel;
    }
    return open();
  }

  function selectionFromSaved(saved) {
    if (!saved) return null;
    if (saved.card === 'ollama') {
      const ol = saved.ollama || {};
      return {
        provider: 'ollama',
        base_url: ol.base_url || pickOllamaHint(),
        api_key: '',
        model: ol.model || FALLBACK.ollama.model,
        label: '本地 Ollama',
      };
    }
    if (saved.card !== 'api') return null;
    const api = saved.api || {};
    if (saved.keyMode === 'own') {
      if (!api.key) return null;
      return { provider: 'openai', base_url: api.base_url || FALLBACK.openai.base_url, api_key: api.key, model: api.model || FALLBACK.openai.model, label: '自备 API Key' };
    }
    const srv = (S.info && S.info.server) || {};
    if (!srv.available) return null;
    return { provider: 'server', base_url: api.base_url || '', api_key: '', model: api.model || srv.model || '', label: '服务端已配置 Key' };
  }

  /** 强制显示(顶栏「切换模型」用) */
  async function open() {
    await ensureInit();
    applySaved(S.saved || loadSaved());
    renderBadges();
    pickCard();
    setStatus(null);
    $('#gateForce').hidden = true;
    $('#gate').hidden = false;
    requestAnimationFrame(() => $('#gate').classList.add('in'));
    S.open = true;
    return new Promise((resolve) => { S._resolve = resolve; });
  }

  window.Gate = {
    run,
    open,
    isOpen() { return S.open; },
    current() { return S.saved ? selectionFromSaved(S.saved) : null; },
    /** 提供给「读取已保存选择」的外部用途 */
    saved: loadSaved,
    _const: FALLBACK,
  };
})();
