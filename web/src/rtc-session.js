import VERTC, { MediaType, RoomProfileType } from '@volcengine/rtc';

export class RtcSession {
  constructor({ log, onAgentMessage, onBotJoined, onError, onJoining, onVolume }) {
    this.log = log;
    this.onAgentMessage = onAgentMessage;
    this.onBotJoined = onBotJoined;
    this.onError = onError;
    this.onJoining = onJoining;
    this.onVolume = onVolume;
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
    this.engine.enableAudioPropertiesReport({ interval: 300 });
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
      const level = Math.min(
        100,
        items?.[0]?.audioPropertiesInfo?.linearVolume
          ?? items?.[0]?.linearVolume
          ?? 0
      );
      this.onVolume(level);
    });
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
