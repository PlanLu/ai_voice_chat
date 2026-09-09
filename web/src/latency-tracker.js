// RTC 网页模式延迟测量。
// 为每一轮“一问一答”记录 5 个时间点（单位 ms，全部使用 performance.now() 单一时钟）：
//   T0 开始说话     —— 本端麦克风音量连续若干帧越过阈值（取首个越阈帧，去抖）
//   T1 ASR 首字上屏  —— 用户气泡第一条非空字幕（在解析后、DOM 写入前触发）
//   T2 ASR 识别结束  —— 助手回复前，用户最后一条 definite=true 终句字幕
//   T3 回复首字上屏  —— 助手气泡（botUserId）第一条非空字幕（同 T1，采集点前置）
//   T4 TTS 首帧播报  —— 用户识别结束后，bot 远端音量从静默到有声音的边缘，
//                        需先经历 BOT_SILENCE_BEFORE_ONSET_MS 静默期，再连续
//                        BOT_ONSET_FRAMES 帧越阈，才认定为真实首帧（去抖+边缘检测）。
// 结束对话后由 summarize() 汇总三项指标平均值：
//   指标1 = T1 - T0，指标2 = T3 - T2，指标3 = T4 - T3。
// 注意：
//   1) 由助手字幕兜底填充的 T1/T2（说明该轮没抓到用户字幕）不计入指标1/指标2，
//      以免出现 0ms/极小的假样本污染平均值。
//   2) 对话结束时若当前轮尚未完成（缺 T3 或 T4），标记 aborted，不参与任何指标。

const STATE = Object.freeze({
  IDLE: 'idle',                   // 等待用户开口
  USER_SPEAKING: 'user-speaking', // 已记录 T0，等待用户字幕
  WAIT_REPLY: 'wait-reply',       // 已记录 T2（用户终句），等待 T3/T4
  WAIT_TTS: 'wait-tts',           // 已记录 T3，等待 T4
});

// 音量阈值（0-100 归一化，与页面音量条同尺度）。如实际环境误判可调这几个常量。
const LOCAL_ONSET_THRESHOLD = 6;             // 判定“开始说话”的本端音量绝对阈值
const LOCAL_NOISE_MARGIN = 4;                // 相对环境噪声底至少要超出的量
const LOCAL_ONSET_FRAMES = 3;                // 需连续多少帧越阈才确认开口（去抖，50ms/帧≈150ms）
const BOT_AUDIO_THRESHOLD = 8;               // 判定“TTS 首帧播报”的远端音量绝对阈值（原 4 太低，易被底噪触发）
const BOT_NOISE_MARGIN = 4;                  // 相对 bot 侧噪声底至少要超出的量
const BOT_ONSET_FRAMES = 2;                  // 需连续多少帧越阈才确认 TTS 首帧（去抖，50ms/帧≈100ms）
const BOT_SILENCE_BEFORE_ONSET_MS = 300;     // T4 之前必须先经历的静默期，用于识别“静默→有声”边缘，
                                             // 过滤用户说话期的回声残留和 bot 说话中的短暂波动
const BOT_TAIL_SILENCE_MS = 600;             // bot 停止发声后需静默多久才允许开启新一轮

function average(values) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

export class LatencyTracker {
  constructor({ log, onTurn } = {}) {
    this.log = log;
    this.onTurn = onTurn;
    this.reset();
  }

  reset() {
    this.accepting = true;
    this.turns = [];
    this.current = null;
    this.state = STATE.IDLE;
    this.noiseFloor = 0;
    this.botNoiseFloor = 0;
    this.lastBotActiveAt = Number.NEGATIVE_INFINITY;
    this.botSilenceStartedAt = null;
    this.onsetRun = 0;
    this.onsetCandidate = null;
    this.botOnsetRun = 0;
    this.botOnsetCandidate = null;
  }

  // 本端音量上报：在 IDLE 时检测“开始说话”(T0)。
  // 去抖：连续 LOCAL_ONSET_FRAMES 帧越阈才确认，T0 记为首个越阈帧的时间。
  onLocalVolume(level) {
    if (!this.accepting) return;
    const now = performance.now();
    if (this.state !== STATE.IDLE) {
      this.onsetRun = 0;
      this.onsetCandidate = null;
      return;
    }
    // bot 尾音未消散前不开新一轮，避免回声/抢话造成误触发。
    if (now - this.lastBotActiveAt < BOT_TAIL_SILENCE_MS) {
      this.onsetRun = 0;
      this.onsetCandidate = null;
      return;
    }
    const threshold = Math.max(LOCAL_ONSET_THRESHOLD, this.noiseFloor + LOCAL_NOISE_MARGIN);
    if (level < threshold) {
      // 静音期缓慢跟踪环境噪声底，让阈值自适应麦克风灵敏度。
      this.noiseFloor = this.noiseFloor * 0.9 + level * 0.1;
      this.onsetRun = 0;
      this.onsetCandidate = null;
      return;
    }
    if (this.onsetRun === 0) this.onsetCandidate = now;
    this.onsetRun += 1;
    if (this.onsetRun >= LOCAL_ONSET_FRAMES) {
      this.startTurn(this.onsetCandidate ?? now, 'volume');
      this.onsetRun = 0;
      this.onsetCandidate = null;
    }
  }

  // 远端(bot)音量上报：在用户识别结束后检测“TTS 首帧播报”(T4)。
  // 从 WAIT_REPLY 就开始监听，因此即使 bot 音频早于助手字幕(T3)到达也能抓到真实首帧。
  // 判定升级：
  //   1) 阈值自适应 —— max(BOT_AUDIO_THRESHOLD, botNoiseFloor + BOT_NOISE_MARGIN)；
  //   2) 边缘检测 —— 首个越阈帧之前必须经历 BOT_SILENCE_BEFORE_ONSET_MS 的静默期，
  //      以过滤用户说话期回声残留、bot 说话中的短暂波动；
  //   3) 去抖 —— 连续 BOT_ONSET_FRAMES 帧越阈才确认，T4 记为首个越阈帧时间。
  onRemoteVolume(level) {
    if (!this.accepting) return;
    const now = performance.now();
    const threshold = Math.max(BOT_AUDIO_THRESHOLD, this.botNoiseFloor + BOT_NOISE_MARGIN);
    if (level < threshold) {
      // 静默期缓慢跟踪 bot 侧噪声底，让阈值自适应远端底噪/编码残留。
      this.botNoiseFloor = this.botNoiseFloor * 0.9 + level * 0.1;
      if (this.botSilenceStartedAt == null) this.botSilenceStartedAt = now;
      this.botOnsetRun = 0;
      this.botOnsetCandidate = null;
      return;
    }
    this.lastBotActiveAt = now;
    // 首个越阈帧：要求前置静默期足够长，否则视为“仍在响”不启动边缘检测。
    if (this.botOnsetRun === 0) {
      const hadEnoughSilence = this.botSilenceStartedAt != null
        && now - this.botSilenceStartedAt >= BOT_SILENCE_BEFORE_ONSET_MS;
      // 一旦进入有声区间就结束本次静默期，不能让持续有声的时间被误算为前置静默。
      this.botSilenceStartedAt = null;
      if (!hadEnoughSilence) return;
      this.botOnsetCandidate = now;
    }
    this.botOnsetRun += 1;
    if (!this.current || this.current.t4 != null) return;
    if (this.state !== STATE.WAIT_REPLY && this.state !== STATE.WAIT_TTS) return;
    if (this.botOnsetRun >= BOT_ONSET_FRAMES) {
      this.current.t4 = this.botOnsetCandidate ?? now;
      this.botOnsetRun = 0;
      this.botOnsetCandidate = null;
      this.maybeComplete();
    }
  }

  // 字幕上屏：区分用户/助手，记录 T1/T2/T3。
  onSubtitle({ isAssistant, final }) {
    if (!this.accepting) return;
    const now = performance.now();
    if (isAssistant) {
      if (this.current && this.current.t3 == null
        && (this.state === STATE.WAIT_REPLY || this.state === STATE.USER_SPEAKING)) {
        // 没抓到用户字幕时用助手首字兜底，标记来源以便从指标1/2 中剔除。
        if (this.current.t1 == null) {
          this.current.t1 = now;
          this.current.t1Source = 'assistant-fallback';
        }
        if (this.current.t2 == null) {
          this.current.t2 = now;
          this.current.t2Source = 'assistant-fallback';
        }
        this.current.t3 = now;
        this.state = STATE.WAIT_TTS;
        this.maybeComplete();
      }
      return;
    }

    // 用户字幕：若音量未触发 T0（阈值不匹配等），用首条用户字幕兜底开启一轮，
    // 并标记 t0Source='subtitle'，该轮指标1 不计入平均。
    if (this.state === STATE.IDLE) this.startTurn(now, 'subtitle');
    if (!this.current) return;
    if (this.current.t1 == null) {
      this.current.t1 = now;
      this.current.t1Source = 'user';
    }
    // T2 取“助手回复前用户最后一条终句”：每次 definite 都刷新，直到 T3 到达。
    if (final && this.state !== STATE.WAIT_TTS) {
      this.current.t2 = now;
      this.current.t2Source = 'user';
      this.state = STATE.WAIT_REPLY;
    }
  }

  startTurn(now, t0Source) {
    this.current = {
      index: this.turns.length + 1,
      t0: now,
      t0Source,
      t1: null,
      t1Source: null,
      t2: null,
      t2Source: null,
      t3: null,
      t4: null,
      aborted: false,
    };
    this.state = STATE.USER_SPEAKING;
    // 新一轮开始，重置 bot 边缘检测的累积状态，避免跨轮残留导致 T4 立即触发。
    this.botOnsetRun = 0;
    this.botOnsetCandidate = null;
  }

  // T3、T4 都到位即完成一轮（两者先后顺序不限）。
  maybeComplete() {
    if (this.current && this.current.t3 != null && this.current.t4 != null) this.completeTurn();
  }

  completeTurn() {
    const turn = this.current;
    this.turns.push(turn);
    this.current = null;
    this.state = STATE.IDLE;
    this.onTurn?.(turn, this.metricsFor(turn));
  }

  metricsFor(turn) {
    const { t0, t1, t2, t3, t4 } = turn;
    // 未完成轮（对话被中止时）不参与任何指标。
    if (turn.aborted) return { metric1: null, metric2: null, metric3: null };
    // 指标1/2 仅在 T1/T2 来自真实用户字幕时才有意义；兜底填充的样本剔除。
    const metric1 = (t0 != null && t1 != null && turn.t0Source === 'volume'
      && turn.t1Source === 'user') ? t1 - t0 : null;
    const metric2 = (t2 != null && t3 != null && turn.t2Source === 'user') ? t3 - t2 : null;
    // 指标3 允许为负：负值说明 TTS 音频早于助手字幕上屏（真实的音画顺序问题）。
    const metric3 = (t3 != null && t4 != null) ? t4 - t3 : null;
    return { metric1, metric2, metric3 };
  }

  // 结束对话时调用：冲刷未完成的当前轮，返回汇总结果。
  // 未完成轮标记 aborted=true，test.log 中会显式注明且不参与任何指标平均。
  finalize() {
    this.accepting = false;
    if (this.current) {
      this.current.aborted = true;
      this.turns.push(this.current);
      this.current = null;
      this.state = STATE.IDLE;
    }
    return this.summarize();
  }

  summarize() {
    const m1 = [];
    const m2 = [];
    const m3 = [];
    const turns = this.turns.map((turn) => {
      const metrics = this.metricsFor(turn);
      if (metrics.metric1 != null) m1.push(metrics.metric1);
      if (metrics.metric2 != null) m2.push(metrics.metric2);
      if (metrics.metric3 != null) m3.push(metrics.metric3);
      return { ...turn, ...metrics };
    });
    return {
      totalTurns: this.turns.length,
      turns,
      averages: { metric1: average(m1), metric2: average(m2), metric3: average(m3) },
      counts: { metric1: m1.length, metric2: m2.length, metric3: m3.length },
    };
  }
}

export { STATE as LATENCY_STATE };
