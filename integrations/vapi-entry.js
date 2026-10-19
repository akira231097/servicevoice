import VapiPackage from '@vapi-ai/web';

// The published package is CommonJS with exports.default. ESM bundlers may
// expose that exports object as the default rather than the constructor itself.
const Vapi = typeof VapiPackage === 'function' ? VapiPackage : VapiPackage.default;
if (typeof Vapi !== 'function') throw new Error('sdk-unavailable');

// Vapi 3.0.0 builds/plays a DOM sink before emitting `audio`. Its stale-build,
// participant-leave and replacement branches call only player.remove(), which
// does not pause a live MediaStream. Use the pinned SDK's builder hook so every
// allocated sink is owned even while play() is pending. This wrapper changes
// playback-resource cleanup, not the SDK's signaling or track-selection logic.
export default class ServiceVoiceVapi extends Vapi {
  ownedAudioPlayers = new Set();
  // The app creates one SDK instance per VoiceSession. Teardown is terminal:
  // late events from an old call must never start playback for a later session.
  audioClosing = false;

  async handleTrackStarted(event) {
    if (this.audioClosing) return;
    return super.handleTrackStarted(event);
  }

  async audioPlayerBuild(track, participantId, volume) {
    const player = document.createElement('audio');
    const nativeRemove = player.remove.bind(player);
    let retired = false;
    player.remove = () => {
      if (!retired) {
        retired = true;
        // Detach a sink, never stop the underlying possibly shared track.
        try { player.pause(); } catch { /* Continue detaching. */ }
        try { player.srcObject = null; } catch { /* Continue removing. */ }
        this.ownedAudioPlayers.delete(player);
      }
      nativeRemove();
    };
    this.ownedAudioPlayers.add(player);
    try {
      player.dataset.participantId = participantId;
      if (typeof volume === 'number') player.volume = volume;
      player.muted = false;
      player.autoplay = true;
      document.body.appendChild(player);
      if (track != null) {
        player.srcObject = new MediaStream([track]);
        await player.play();
      }
      return player;
    } catch (error) {
      // Teardown can reject a pending play(). The SDK has already invalidated
      // this build, so let its stale-result path discard it without a new error.
      if (retired) return player;
      player.remove();
      throw error;
    }
  }

  retireAudioPlayers(participantId) {
    for (const player of [...this.ownedAudioPlayers]) {
      if (participantId === undefined || player.dataset.participantId === participantId) player.remove();
    }
  }

  detachAudioPlayer(participantId) {
    // Invalidate first: pausing a pending play() may reject its promise.
    super.detachAudioPlayer(participantId);
    this.retireAudioPlayers(participantId);
  }

  async stop() {
    this.audioClosing = true;
    this.pendingAudioBuilds.clear();
    this.retireAudioPlayers();
    try { return await super.stop(); }
    finally {
      this.pendingAudioBuilds.clear();
      this.retireAudioPlayers();
    }
  }

  async cleanup() {
    this.audioClosing = true;
    this.pendingAudioBuilds.clear();
    this.retireAudioPlayers();
    try { return await super.cleanup(); }
    finally {
      this.pendingAudioBuilds.clear();
      this.retireAudioPlayers();
    }
  }
}
