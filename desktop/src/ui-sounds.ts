type UiSoundKind = "nav" | "list" | "highlight";

let audioContext: AudioContext | null = null;
let masterGain: GainNode | null = null;
let lastPlayAt = 0;

const MASTER_VOLUME = 0.35;
const MIN_GAP_MS = 48;

function ensureAudioContext(): AudioContext | null {
  const AudioContextClass =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextClass) return null;
  if (!audioContext) {
    audioContext = new AudioContextClass();
    masterGain = audioContext.createGain();
    masterGain.gain.value = MASTER_VOLUME;
    masterGain.connect(audioContext.destination);
  }
  return audioContext;
}

function unlockSound() {
  const ctx = ensureAudioContext();
  if (ctx?.state === "suspended") void ctx.resume();
}

function playTone(options: {
  frequency: number;
  endFrequency?: number;
  type?: OscillatorType;
  peak: number;
  attack: number;
  decay: number;
  delay?: number;
  lowpass?: number;
}) {
  const ctx = ensureAudioContext();
  if (!ctx || !masterGain) return;
  if (ctx.state === "suspended") void ctx.resume();

  const when = ctx.currentTime + (options.delay ?? 0);
  const oscillator = ctx.createOscillator();
  oscillator.type = options.type ?? "sine";
  oscillator.frequency.setValueAtTime(options.frequency, when);
  if (options.endFrequency) {
    oscillator.frequency.exponentialRampToValueAtTime(
      options.endFrequency,
      when + options.attack + options.decay * 0.85,
    );
  }

  const envelope = ctx.createGain();
  envelope.gain.setValueAtTime(0.0001, when);
  envelope.gain.exponentialRampToValueAtTime(options.peak, when + options.attack);
  envelope.gain.exponentialRampToValueAtTime(
    0.0001,
    when + options.attack + options.decay,
  );
  envelope.connect(masterGain);

  if (options.lowpass) {
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = options.lowpass;
    oscillator.connect(filter);
    filter.connect(envelope);
  } else {
    oscillator.connect(envelope);
  }

  oscillator.start(when);
  oscillator.stop(when + options.attack + options.decay + 0.03);
}

function playUiSound(kind: UiSoundKind) {
  const now = performance.now();
  if (now - lastPlayAt < MIN_GAP_MS) return;
  lastPlayAt = now;

  if (kind === "nav") {
    playTone({
      frequency: 1720,
      endFrequency: 1480,
      peak: 0.36,
      attack: 0.002,
      decay: 0.06,
      lowpass: 4200,
    });
    return;
  }

  if (kind === "list") {
    playTone({
      frequency: 1980,
      endFrequency: 1600,
      type: "triangle",
      peak: 0.42,
      attack: 0.003,
      decay: 0.07,
      lowpass: 5000,
    });
    playTone({
      frequency: 3960,
      peak: 0.1,
      attack: 0.002,
      decay: 0.04,
    });
    return;
  }

  playTone({
    frequency: 520,
    endFrequency: 880,
    peak: 0.4,
    attack: 0.015,
    decay: 0.16,
    lowpass: 3200,
  });
  playTone({
    frequency: 780,
    endFrequency: 1240,
    peak: 0.2,
    attack: 0.02,
    decay: 0.12,
    delay: 0.04,
  });
}

function enteredElement(event: PointerEvent, selector: string): HTMLElement | null {
  const element = (event.target as HTMLElement | null)?.closest<HTMLElement>(selector);
  if (!element) return null;
  const previous = event.relatedTarget;
  if (previous instanceof Node && element.contains(previous)) return null;
  return element;
}

export function setupUiSounds() {
  window.addEventListener("pointerdown", unlockSound, { once: true });
  window.addEventListener("keydown", unlockSound, { once: true });

  document.addEventListener("pointerover", (event) => {
    if (
      enteredElement(event, ".nav-item, .settings-tab, .report-kind-tab")
    ) {
      playUiSound("nav");
      return;
    }
    if (
      enteredElement(
        event,
        ".group-item, .monitored-group-item, .provider-item, .msg",
      )
    ) {
      playUiSound("list");
      return;
    }
    if (enteredElement(event, ".report-title-item, .theme-card")) {
      playUiSound("highlight");
    }
  });
}
