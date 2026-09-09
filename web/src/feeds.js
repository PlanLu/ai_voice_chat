function timeText() {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function deepFind(value, names) {
  if (!value || typeof value !== 'object') return undefined;
  const wanted = new Set(names.map((name) => name.toLowerCase()));
  for (const [key, child] of Object.entries(value)) {
    if (wanted.has(key.toLowerCase()) && child !== null && child !== undefined && child !== '') {
      return child;
    }
  }
  for (const child of Object.values(value)) {
    const found = deepFind(child, names);
    if (found !== undefined) return found;
  }
  return undefined;
}

export class EventLog {
  constructor(container, limit = 300) {
    this.container = container;
    this.limit = limit;
  }

  add(text, data) {
    const item = document.createElement('li');
    item.textContent = `${timeText()}  ${text}${data ? ` · ${JSON.stringify(data)}` : ''}`;
    this.container.prepend(item);
    while (this.container.children.length > this.limit) {
      this.container.lastElementChild.remove();
    }
  }

  clear() {
    this.container.innerHTML = '';
  }
}

export class ConversationView {
  constructor(container, limit = 150) {
    this.container = container;
    this.limit = limit;
    this.activeItems = new Map();
    this.recentFinals = new Map();
    this.finalDedupMs = 5000;
    // 字幕真正写入 DOM（“上屏”）时触发，供延迟测量记录 T1/T2/T3。
    this.onRender = undefined;
  }

  speakerFor(subtitle, config) {
    const userId = deepFind(subtitle, ['userId', 'UserId']);
    if (userId === config.botUserId) return '助手';
    const voiceprintName = deepFind(subtitle, ['voiceprintName', 'VoicePrintName']);
    if (voiceprintName) return voiceprintName;
    const voiceprintId = deepFind(subtitle, [
      'voiceprintId', 'VoicePrintId', 'voiceprint_id', 'voice_print_id',
    ]);
    return config.voiceprints?.[String(voiceprintId ?? '').trim()] || '用户';
  }

  render(subtitle, config) {
    const text = String(deepFind(subtitle, ['text', 'Text']) || '').trim();
    if (!text) return;

    const speaker = this.speakerFor(subtitle, config);
    const userId = deepFind(subtitle, ['userId', 'UserId']);
    const isAssistant = userId === config.botUserId;
    // 单用户页面中，字幕没有携带 UserId 时也使用稳定的用户键。
    // 不能以当前显示名称作为键，否则“用户”变为真名时会新建气泡。
    const key = String(userId || (isAssistant ? config.botUserId : '__local_user__'));
    const final = Boolean(deepFind(subtitle, ['definite', 'Definite']));
    const signature = `${key}:${text}`;
    const now = Date.now();

    for (const [savedSignature, savedAt] of this.recentFinals) {
      if (now - savedAt > this.finalDedupMs) this.recentFinals.delete(savedSignature);
    }

    let item = this.activeItems.get(key);
    if (final && !item && this.recentFinals.has(signature)) return;

    // 延迟测量钩子：在解析完字段、DOM 写入之前触发，避免 querySelector/textContent/
    // trim/scrollToLatest 的耗时污染 T1/T2/T3（实测可减少 5–20ms 系统性偏差）。
    this.onRender?.({ isAssistant, final, text });

    if (!item) item = this.createItem(speaker, isAssistant);

    // 声纹结果可能晚于首个实时字幕到达。每次更新当前分句时都刷新
    // 说话人名称，使“用户”能在匹配成功后立即变为注册姓名。
    item.querySelector('.message-meta').textContent = `${speaker} · ${item.dataset.time}`;
    item.querySelector('.message-text').textContent = text;
    item.classList.toggle('streaming', !final);

    if (final) {
      this.activeItems.delete(key);
      this.recentFinals.set(signature, now);
    } else {
      this.activeItems.set(key, item);
    }

    this.trim();
    this.scrollToLatest();
  }

  createItem(speaker, isAssistant) {
    const item = document.createElement('li');
    item.className = isAssistant ? 'assistant' : 'user';
    item.dataset.time = timeText();
    const meta = document.createElement('span');
    meta.className = 'message-meta';
    meta.textContent = `${speaker} · ${item.dataset.time}`;
    const text = document.createElement('span');
    text.className = 'message-text';
    item.append(meta, text);
    this.container.append(item);
    return item;
  }

  trim() {
    while (this.container.children.length > this.limit) {
      const removed = this.container.firstElementChild;
      for (const [key, item] of this.activeItems) {
        if (item === removed) this.activeItems.delete(key);
      }
      removed.remove();
    }
  }

  scrollToLatest() {
    this.container.scrollTop = this.container.scrollHeight;
  }

  clear() {
    this.container.innerHTML = '';
    this.activeItems.clear();
    this.recentFinals.clear();
  }
}

export { deepFind };
