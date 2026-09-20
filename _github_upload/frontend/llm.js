/* ==============================================================
   llm.js — 生成通道抽象层

   三条通道, 按优先级自动选择:
     1) cloud  : 托管平台下发的免密钥模型通道(需要 publicConfig)
     2) server : 自托管后端, 首选 /api/generate/stream(SSE, 带真实阶段进度),
                 不可用时回退 /api/generate; 服务端配了 LLM_API_KEY 或 Ollama 即可用
     3) none   : 无可用通道, 界面给出明确指引
   ============================================================== */
(function () {
  'use strict';

  const CDN_SDK = ''; // 托管平台的云 SDK 地址, 留空则跳过 cloud 通道

  const state = {
    channel: 'unknown',   // cloud | server | none
    detail: '',
    cloud: null,
    cloudModels: [],
    cloudModel: null,
    server: null,
    preferred: null,      // 进入页选定的通道; null = 走自动探测
    ready: null,
  };

  /**
   * 本次请求要带上的通道覆盖。返回 null 表示「沿用服务端 .env 配置」。
   * 云服务是浏览器直连, 不需要走服务端覆盖。
   */
  function currentOpts() {
    const p = state.preferred;
    if (!p) return null;
    if (p.provider === 'cloud' || p.provider === 'server') return null;
    return {
      provider: p.provider,
      base_url: p.base_url || '',
      api_key: p.api_key || '',
      model: p.model || '',
    };
  }

  function withOpts(req) {
    const o = currentOpts();
    if (!o) return req;
    return Object.assign({}, req, { llm: o });
  }

  function loadScript(src, timeoutMs) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = true;
      let done = false;
      const timer = setTimeout(() => {
        if (!done) { done = true; reject(new Error('SDK 加载超时')); }
      }, timeoutMs || 8000);
      s.onload = () => { if (!done) { done = true; clearTimeout(timer); resolve(); } };
      s.onerror = () => { if (!done) { done = true; clearTimeout(timer); reject(new Error('SDK 加载失败')); } };
      document.head.appendChild(s);
    });
  }

  async function detectCloud() {
    const cfg = window.WB_CLOUD_CONFIG;
    if (!cfg || !cfg.endpoint || !cfg.publishableKey || !CDN_SDK) return null;
    try {
      if (!window.WB_CLOUD_SDK) await loadScript(CDN_SDK);
      const factory = window.WB_CLOUD_SDK && window.WB_CLOUD_SDK.createCloudClient;
      if (!factory) return null;
      const cloud = factory({ endpoint: cfg.endpoint, publishableKey: cfg.publishableKey });
      const models = await cloud.llm.models.list();
      if (!Array.isArray(models) || models.length === 0) {
        return { cloud, models: [], model: null, reason: '平台模型列表为空' };
      }
      const usable = models.filter((m) => m.disabled !== true);
      const model = usable[0] || null;
      return { cloud, models: usable, model, reason: model ? '' : '平台暂无可用模型' };
    } catch (e) {
      return { cloud: null, models: [], model: null, reason: '云服务初始化失败: ' + (e.message || e) };
    }
  }

  async function detectServer() {
    try {
      const r = await fetch('/api/health', { headers: { Accept: 'application/json' } });
      if (!r.ok) return null;
      const d = await r.json();
      const s = d.llm_server_side || {};
      return s.available ? s : null;
    } catch (e) {
      return null;
    }
  }

  async function detect() {
    if (state.ready) return state.ready;
    state.ready = (async () => {
      const pref = state.preferred;

      // ---- 用户在选择页做了明确选择: 严格按选择走, 不再自动探测 ----
      if (pref) {
        if (pref.provider === 'cloud') {
          const c = await detectCloud();
          if (c && c.cloud && c.model) {
            state.channel = 'cloud';
            state.cloud = c.cloud;
            state.cloudModels = c.models;
            state.cloudModel = c.model;
            state.detail = '平台免密钥模型 · ' + (c.model.name || c.model.id);
            return state;
          }
          state.channel = 'none';
          state.detail = (c && c.reason) || '云服务不可用';
          return state;
        }

        if (pref.provider === 'server') {
          // 「沿用服务端 .env 里已配置的 Key」
          const s = await detectServer();
          if (s) {
            state.channel = 'server';
            state.server = s;
            state.detail = (s.provider === 'ollama' ? '本地 Ollama · ' : '服务端模型 · ') + (s.model || '');
            return state;
          }
          state.channel = 'none';
          state.detail = '服务端未配置可用通道（.env 里既没有 LLM_API_KEY，也没有可用的 Ollama）';
          return state;
        }

        // 用户自己填地址/Key: 由本地后端代理转发, 规避浏览器 CORS 限制
        state.channel = 'server';
        state.server = { provider: pref.provider, model: pref.model || '', base_url: pref.base_url || '' };
        state.detail = (pref.provider === 'ollama' ? '本地 Ollama · ' : '自备 API Key · ') + (pref.model || '');
        return state;
      }

      // ---- 没有选择(比如直接访问接口): 沿用原来的自动探测顺序 ----
      // 1) 云服务优先
      const c = await detectCloud();
      if (c && c.cloud && c.model) {
        state.channel = 'cloud';
        state.cloud = c.cloud;
        state.cloudModels = c.models;
        state.cloudModel = c.model;
        state.detail = '平台免密钥模型 · ' + (c.model.name || c.model.id);
        return state;
      }
      // 2) 服务端通道
      const s = await detectServer();
      if (s) {
        state.channel = 'server';
        state.server = s;
        state.detail = (s.provider === 'ollama' ? '本地 Ollama · ' : '服务端模型 · ') + (s.model || '');
        return state;
      }
      // 3) 都不可用
      state.channel = 'none';
      state.detail = (c && c.reason) ? c.reason : '未检测到可用的生成通道';
      return state;
    })();
    return state.ready;
  }

  /** 由进入页调用: 设定本次会话使用的通道, 并让 detect() 重新判定。 */
  function setPreferred(sel) {
    state.preferred = sel || null;
    state.ready = null;
    state.channel = 'unknown';
    state.detail = '';
    state.server = null;
  }

  function status() {
    return { channel: state.channel, detail: state.detail, model: state.cloudModel || state.server };
  }

  /**
   * 云端流式生成, 返回完整文本
   * @param {Array<{role:string,content:string}>} messages
   * @param {(t:string)=>void} onDelta
   */
  async function chatCloud(messages, onDelta, signal) {
    if (!state.cloud || !state.cloudModel) throw new Error('云服务通道未就绪');
    let out = '';
    const req = {
      model: state.cloudModel.id,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    };
    if (state.cloudModel.temperature !== undefined) req.temperature = state.cloudModel.temperature;
    if (state.cloudModel.top_p !== undefined) req.top_p = state.cloudModel.top_p;
    if (signal) req.signal = signal;

    for await (const chunk of state.cloud.llm.chat.completions.create(req)) {
      const delta = chunk.choices && chunk.choices[0] && chunk.choices[0].delta;
      if (delta && delta.content) {
        out += delta.content;
        if (onDelta) onDelta(delta.content);
      }
    }
    return out;
  }

  async function chatServer(req, signal) {
    const r = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(withOpts(req)),
      signal,
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = (d && d.detail && d.detail.message) || (d && d.detail) || d.message || ('HTTP ' + r.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return d;
  }

  /**
   * 服务端流式生成(SSE)。逐事件回调, 最终返回与 chatServer 同构的结果。
   *
   * onEvent 收到的事件(按真实发生顺序):
   *   {event:'stage',      stage:'retrieve'|'think'|'write'|'parse', label}
   *   {event:'stage_done', stage:'retrieve', hits, refs, elapsed_ms}
   *   {event:'tick',       reasoning_chars, content_chars, elapsed_ms}
   *   {event:'retry',      attempt, max_tokens, reason}
   *   {event:'done',       result, elapsed_ms}
   *   {event:'error',      message, detail, status}
   *
   * 若流式通道不可用(老版本后端 / 代理不支持), 抛出带有
   * `streamUnavailable = true` 标记的错误, 调用方应回退到 chatServer()。
   */
  async function chatServerStream(req, onEvent, signal) {
    let r;
    try {
      r = await fetch('/api/generate/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(withOpts(req)),
        signal,
      });
    } catch (e) {
      const err = new Error(e && e.message ? e.message : '网络请求失败');
      err.streamUnavailable = true;
      throw err;
    }

    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      const msg = (d && d.detail && (d.detail.message || d.detail)) || d.message || ('HTTP ' + r.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      // 404/405 说明后端还是老版本, 值得回退重试
      if (r.status === 404 || r.status === 405 || r.status === 501) err.streamUnavailable = true;
      throw err;
    }
    const ct = (r.headers.get('Content-Type') || '').toLowerCase();
    if (!r.body || ct.indexOf('text/event-stream') < 0) {
      const err = new Error('服务端未返回事件流');
      err.streamUnavailable = true;
      throw err;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    let finalResult = null;
    let streamError = null;

    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        // SSE 帧以空行分隔
        const frames = buf.split('\n\n');
        buf = frames.pop();
        for (let i = 0; i < frames.length; i++) {
          const lines = frames[i].split('\n');
          for (let j = 0; j < lines.length; j++) {
            const ln = lines[j];
            if (ln.indexOf('data:') !== 0) continue;
            let obj;
            try {
              obj = JSON.parse(ln.slice(5).trim());
            } catch (e) {
              continue;
            }
            if (obj.event === 'done') finalResult = obj.result;
            else if (obj.event === 'error') streamError = obj;
            if (onEvent) {
              try { onEvent(obj); } catch (e) { /* 回调异常不影响主流程 */ }
            }
          }
        }
      }
    } finally {
      try { reader.releaseLock(); } catch (e) { /* ignore */ }
    }

    if (streamError) {
      const err = new Error(streamError.message || '流式生成失败');
      // 已经开始生成后才失败, 细节已经推给界面了, 不必再回退重跑
      err.streamPartial = true;
      throw err;
    }
    if (!finalResult) throw new Error('流式生成结束但未收到结果');
    return finalResult;
  }

  window.LLM = {
    detect,
    status,
    setPreferred,
    chatCloud,
    chatServer,
    chatServerStream,
    get channel() { return state.channel; },
    get preferred() { return state.preferred; },
    get opts() { return currentOpts(); },
    get modelName() {
      const m = state.cloudModel || state.server;
      return m ? (m.name || m.model || m.id || '') : '';
    },
    reset() { state.ready = null; state.channel = 'unknown'; },
  };
})();
