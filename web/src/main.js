import './style.css';

import { createAgentMessageHandler } from './agent-messages.js';
import { api } from './api.js';
import { ConversationView, EventLog } from './feeds.js';
import { RtcSession } from './rtc-session.js';
import { SessionState, SessionStateMachine } from './session-state.js';
import { VoiceprintRecorder } from './voiceprint-recorder.js';

const ui = Object.fromEntries([
  'statusDot', 'status', 'room', 'user', 'voiceprint', 'start', 'register',
  'mute', 'stop', 'meter', 'conversation', 'clearConversation', 'log', 'clear',
  'registerPanel', 'registerName', 'registerConfirm', 'registerCancel',
  'registerProgress', 'registerCountdown', 'registerHint', 'registerBar',
  'voiceprintMeter',
].map((id) => [id, document.getElementById(id)]));

const conversation = new ConversationView(ui.conversation, 150);
const log = new EventLog(ui.log, 300);
const state = new SessionStateMachine(ui);
const voiceprintRecorder = new VoiceprintRecorder(20);

let config;
let taskId;
let botTimer;

const handleAgentMessage = createAgentMessageHandler({
  getConfig: () => config,
  conversation,
  log,
  onError: (message) => state.transition(
    SessionState.ERROR,
    message,
    { canStop: Boolean(taskId) }
  ),
});

const rtc = new RtcSession({
  log,
  onAgentMessage: handleAgentMessage,
  onJoining: () => state.transition(SessionState.JOINING, '正在加入 RTC 房间…'),
  onBotJoined: () => {
    clearTimeout(botTimer);
    state.transition(SessionState.ACTIVE, 'AI 已进房，对话进行中');
  },
  onError: (message, warning = false) => {
    if (warning && taskId) {
      state.transition(SessionState.ACTIVE, message, { tone: 'warn' });
      return;
    }
    state.transition(SessionState.ERROR, message, {
      canStop: Boolean(taskId || rtc.engine),
    });
  },
  onVolume: (level) => {
    ui.meter.style.width = `${level}%`;
  },
});

function updatePublicConfig(nextConfig) {
  config = nextConfig;
  ui.room.textContent = config.roomId;
  ui.user.textContent = config.userId;
  ui.voiceprint.textContent = `${config.voiceprintCount} 个`;
}

async function startConversation() {
  state.transition(SessionState.REQUESTING_MEDIA, '正在申请麦克风权限…');
  try {
    await rtc.join(config);
    state.transition(SessionState.STARTING_AGENT, '正在启动 AI 助手…');
    const session = await api('/api/session/start', {
      method: 'POST',
      body: JSON.stringify({ roomId: config.roomId, userId: config.userId }),
    });
    taskId = session.taskId;
    state.transition(SessionState.STARTING_AGENT, 'AI 任务已受理，等待助手进房…');
    log.add(session.reused ? '复用已有 AI 任务' : 'AI OpenAPI 已受理', {
      taskId,
      requestId: session.openApi?.requestId,
      result: session.openApi?.result,
    });

    clearTimeout(botTimer);
    botTimer = setTimeout(() => {
      if (rtc.botObserved) return;
      state.transition(
        SessionState.ERROR,
        'AI 任务已受理，但助手 15 秒内未进房，请查看运行日志',
        { canStop: true }
      );
      log.add('诊断：助手未进房', {
        likely: '检查 AI 音视频互动方案、跨服务授权、模型额度和 TargetUserId',
        requestId: session.openApi?.requestId,
      });
    }, 15000);
  } catch (error) {
    log.add('启动失败', { message: error.message });
    clearTimeout(botTimer);
    await rtc.leave();
    taskId = undefined;
    state.transition(SessionState.ERROR, error.message);
  }
}

async function stopConversation() {
  state.transition(SessionState.STOPPING, '正在结束对话…', { canStop: false });
  try {
    if (taskId) {
      const result = await api('/api/session/stop', {
        method: 'POST',
        body: JSON.stringify({ roomId: config.roomId, taskId }),
      });
      if (result.stopped !== true) {
        throw new Error('服务端未确认云端 AI 任务已停止');
      }
      log.add('AI 任务已停止');
    }
  } catch (error) {
    log.add('停止 AI 任务失败', { message: error.message });
    clearTimeout(botTimer);
    await rtc.leave();
    state.setMuted(false);
    state.transition(
      SessionState.ERROR,
      `已退出 RTC 房间，但云端 AI 任务停止失败，请点击“结束对话”重试：${error.message}`,
      { canStop: true }
    );
    return;
  }

  taskId = undefined;
  clearTimeout(botTimer);
  await rtc.leave();
  state.setMuted(false);
  state.transition(SessionState.READY, '已结束，云端 AI 任务和 RTC 连接均已停止');
}

function toggleMute() {
  const muted = rtc.toggleMute();
  state.setMuted(muted);
  state.transition(
    SessionState.ACTIVE,
    muted ? '麦克风已静音' : '对话进行中，请直接讲话',
    { tone: muted ? 'warn' : 'online' }
  );
}

function resetVoiceprintDialog() {
  ui.registerName.disabled = false;
  ui.registerConfirm.disabled = false;
  ui.registerCancel.disabled = false;
  ui.registerProgress.hidden = true;
  ui.registerBar.value = 0;
  ui.voiceprintMeter.style.width = '0%';
  ui.registerCountdown.textContent = '准备录音';
  ui.registerHint.textContent = '请保持自然、连续地说话';
}

function openVoiceprintDialog() {
  resetVoiceprintDialog();
  ui.registerName.value = '';
  ui.registerPanel.showModal();
  ui.registerName.focus();
}

function closeVoiceprintDialog() {
  if (ui.registerPanel.open) ui.registerPanel.close();
  resetVoiceprintDialog();
}

function cancelVoiceprint() {
  if (voiceprintRecorder.active) {
    voiceprintRecorder.cancel();
    return;
  }
  closeVoiceprintDialog();
}

async function registerVoiceprint() {
  const name = ui.registerName.value.trim();
  if (!name) {
    ui.registerName.focus();
    ui.registerHint.textContent = '请先输入姓名';
    return;
  }

  state.transition(SessionState.RECORDING_VOICEPRINT, '正在录制声纹…');
  ui.registerName.disabled = true;
  ui.registerConfirm.disabled = true;
  ui.registerProgress.hidden = false;
  ui.registerHint.textContent = '请保持自然、连续地说话';

  try {
    const audio = await voiceprintRecorder.record(({ elapsed, remaining, volume }) => {
      ui.registerBar.value = elapsed;
      ui.registerCountdown.textContent = `还剩 ${Math.ceil(remaining)} 秒`;
      ui.voiceprintMeter.style.width = `${volume}%`;
      ui.registerHint.textContent = volume < 6 ? '声音有点小，请靠近麦克风' : '正在采集，继续说话';
    });
    if (!audio) {
      closeVoiceprintDialog();
      state.transition(SessionState.READY, '已取消声纹录音', { tone: 'warn' });
      return;
    }

    ui.registerBar.value = voiceprintRecorder.durationSeconds;
    ui.voiceprintMeter.style.width = '0%';
    ui.registerCountdown.textContent = '正在上传并注册…';
    ui.registerHint.textContent = '请稍候，不要关闭页面';
    const result = await api('/api/voiceprint/register', {
      method: 'POST',
      body: JSON.stringify({ name, audio }),
    });
    log.add(`声纹注册成功：${result.name}`, { voiceprintId: result.voiceprintId });
    updatePublicConfig(await api('/api/config'));
    closeVoiceprintDialog();
    state.transition(SessionState.READY, '声纹注册成功，可以开始对话');
  } catch (error) {
    log.add('声纹注册失败', { message: error.message });
    ui.registerName.disabled = false;
    ui.registerConfirm.disabled = false;
    ui.registerCountdown.textContent = '录制或注册失败';
    ui.registerHint.textContent = error.message;
    state.transition(SessionState.ERROR, error.message);
  }
}

async function init() {
  state.transition(SessionState.LOADING, '正在读取配置…');
  try {
    updatePublicConfig(await api('/api/config'));
    state.transition(SessionState.READY, '准备就绪');
  } catch (error) {
    log.add('配置检查失败', { message: error.message });
    state.transition(SessionState.ERROR, error.message);
  }
}

ui.start.addEventListener('click', startConversation);
ui.stop.addEventListener('click', stopConversation);
ui.mute.addEventListener('click', toggleMute);
ui.register.addEventListener('click', openVoiceprintDialog);
ui.registerConfirm.addEventListener('click', registerVoiceprint);
ui.registerCancel.addEventListener('click', cancelVoiceprint);
ui.registerPanel.addEventListener('cancel', (event) => {
  event.preventDefault();
  cancelVoiceprint();
});
ui.clear.addEventListener('click', () => log.clear());
ui.clearConversation.addEventListener('click', () => conversation.clear());
window.addEventListener('beforeunload', () => {
  if (taskId) {
    navigator.sendBeacon('/api/session/stop', new Blob([
      JSON.stringify({ roomId: config.roomId, taskId }),
    ], { type: 'application/json' }));
  }
});

init();
