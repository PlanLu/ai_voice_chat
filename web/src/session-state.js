export const SessionState = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  REQUESTING_MEDIA: 'requesting-media',
  JOINING: 'joining',
  STARTING_AGENT: 'starting-agent',
  ACTIVE: 'active',
  STOPPING: 'stopping',
  RECORDING_VOICEPRINT: 'recording-voiceprint',
  DELETING_VOICEPRINT: 'deleting-voiceprint',
  ERROR: 'error',
});

const BUSY_STATES = new Set([
  SessionState.LOADING,
  SessionState.REQUESTING_MEDIA,
  SessionState.JOINING,
  SessionState.STARTING_AGENT,
  SessionState.STOPPING,
  SessionState.RECORDING_VOICEPRINT,
  SessionState.DELETING_VOICEPRINT,
]);

export class SessionStateMachine {
  constructor(ui) {
    this.ui = ui;
    this.state = SessionState.LOADING;
    this.muted = false;
  }

  transition(state, message, { canStop = false, tone } = {}) {
    this.state = state;
    const busy = BUSY_STATES.has(state);
    const active = state === SessionState.ACTIVE;
    const ready = state === SessionState.READY;
    const recoverableError = state === SessionState.ERROR && !canStop;

    this.ui.start.disabled = !(ready || recoverableError);
    this.ui.register.disabled = !(ready || recoverableError);
    this.ui.deleteVoiceprint.disabled = !(ready || recoverableError);
    this.ui.mute.disabled = !active;
    this.ui.stop.disabled = !(active || canStop);

    const stateTone = tone || (
      state === SessionState.ERROR ? 'error'
        : active || ready ? 'online'
          : busy ? 'busy' : ''
    );
    this.ui.status.textContent = message;
    this.ui.statusDot.className = `dot ${stateTone}`;
  }

  setMuted(muted) {
    this.muted = muted;
    this.ui.mute.textContent = muted ? '恢复麦克风' : '麦克风静音';
  }
}
