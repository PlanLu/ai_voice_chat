import VERTC, { MediaType, RoomProfileType } from '@volcengine/rtc';

// 音量上报间隔（ms）。延迟测量需要较细的时间粒度，故从默认 300ms 调小。
const AUDIO_REPORT_INTERVAL_MS = 50;

// 从音量上报回调的数据结构中提取归一化到 0-100 的音量值。
function audioLevel(item, fallback) {
  const level = item?.audioPropertiesInfo?.linearVolume ?? item?.linearVolume ?? fallback ?? 0;
  return Math.min(100, level);
}

export class RtcSession {
  constructor({ log, onAgentMessage, onBotJoined, onError, onJoining, onVolume, onRemoteVolume }) {
    this.log = log;
    this.onAgentMessage = onAgentMessage;
    this.onBotJoined = onBotJoined;
    this.onError = onError;
    this.onJoining = onJoining;
    this.onVolume = onVolume;
    this.onRemoteVolume = onRemoteVolume;
    this.engine = undefined;
    this.joined = false;
    this.muted = false;
    this.botObserved = false;
  }

  async join(config) {
    const permission = await VERTC.enableDevices({ audio: true, video: false });
    if (!permission.audio) {
      throw new Error('没有麦克风权限，请在浏览器地址栏中允许麦克风');
    }

    this.engine = VERTC.createEngine(config.appId);
    this.bindEvents(config);
    this.engine.enableAudioPropertiesReport({ interval: AUDIO_REPORT_INTERVAL_MS });
    await this.engine.startAudioCapture();
    this.onJoining();
    await this.engine.joinRoom(
      config.token,
      config.roomId,
      {
        userId: config.userId,
        extraInfo: JSON.stringify({ call_scene: 'RTC-AIGC', user_name: config.userId }),
      },
      {
        isAutoPublish: true,
        isAutoSubscribeAudio: true,
        roomProfileType: RoomProfileType.chat,
      }
    );
    this.joined = true;
    this.muted = false;
    this.botObserved = false;
    this.log.add('已加入房间并发布麦克风');
  }

  bindEvents(config) {
    this.engine.on(VERTC.events.onError, (event) => {
      this.log.add('RTC 错误', event);
      this.onError(`RTC 错误：${event.errorCode ?? '未知'}`);
    });
    this.engine.on(VERTC.events.onUserJoined, (event) => {
      const userId = event.userInfo?.userId || event.userId;
      this.log.add(`用户加入：${userId}`);
      if (userId === config.botUserId) this.markBotObserved();
    });
    this.engine.on(VERTC.events.onUserLeave, (event) => {
      this.log.add(`用户离开：${event.userId}`);
    });
    this.engine.on(VERTC.events.onUserPublishStream, (event) => {
      this.log.add(`收到远端流：${event.userId}`, event.mediaType);
      if (event.userId === config.botUserId) this.markBotObserved();
    });
    this.engine.on(VERTC.events.onAutoplayFailed, () => {
      this.log.add('浏览器阻止了自动播放，请再次点击页面');
      this.onError('请点击页面允许播放声音', true);
    });
    this.engine.on(VERTC.events.onLocalAudioPropertiesReport, (items) => {
      this.onVolume(audioLevel(items?.[0]));
    });
    // 远端(bot)音量上报：用于检测 TTS 首帧播报。部分 SDK 版本无此事件，做存在性保护。
    if (VERTC.events.onRemoteAudioPropertiesReport) {
      this.engine.on(VERTC.events.onRemoteAudioPropertiesReport, (items, totalRemoteVolume) => {
        const list = (Array.isArray(items) ? items : [items]).filter(Boolean);
        const bot = list.find(
          (item) => (item?.userId ?? item?.audioPropertiesInfo?.userId) === config.botUserId
        );
        if (bot) {
          this.onRemoteVolume?.(audioLevel(bot));
        } else if (list.length === 0 && Number(totalRemoteVolume ?? 0) === 0) {
          // 空列表且总音量为 0 可确认 bot 静默；存在其他远端用户时不能用其音量替代 bot。
          this.onRemoteVolume?.(0);
        }
      });
    }
    this.engine.on(VERTC.events.onRoomBinaryMessageReceived, this.onAgentMessage);
    this.engine.on(VERTC.events.onUserBinaryMessageReceived, this.onAgentMessage);
  }

  markBotObserved() {
    this.botObserved = true;
    this.onBotJoined();
  }

  toggleMute() {
    if (!this.engine) return false;
    this.muted = !this.muted;
    if (this.muted) {
      this.engine.unpublishStream(MediaType.AUDIO);
    } else {
      this.engine.publishStream(MediaType.AUDIO);
    }
    return this.muted;
  }

  async leave() {
    if (!this.engine) return;
    try {
      if (this.joined) await this.engine.leaveRoom();
      await this.engine.stopAudioCapture();
    } catch (error) {
      this.log.add('退出 RTC 时出现提示', { message: error.message });
    } finally {
      VERTC.destroyEngine(this.engine);
      this.engine = undefined;
      this.joined = false;
      this.muted = false;
      this.botObserved = false;
      this.onVolume(0);
    }
  }
}
