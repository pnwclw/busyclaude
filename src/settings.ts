// The settings described by appmeta/settings.json, read once at startup.

import { getAppSettings } from "@shared/device";

/** Must match "version" in appmeta/settings.json. */
const VERSION = 3;

export interface Settings {
  sound: boolean;
  led: boolean;
  /** Follow a finished turn with the usage numbers. */
  usage: boolean;
}

/**
 * Mirrors the "default" of every field in appmeta/settings.json that the app itself reads. The
 * one it does not is `wait`, the answer time: the hook reads that, because it is the hook that
 * waits, and passes it on with each prompt as `window`.
 */
export const DEFAULTS: Settings = { sound: true, led: true, usage: true };

function flag(values: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const value = values[key];
  return typeof value === "boolean" ? value : fallback;
}

/** A failure or a version mismatch falls back to the defaults, so the app still shows the notification. */
export async function loadSettings(app: string): Promise<Settings> {
  try {
    const { version, values } = await getAppSettings(app);
    if (version !== VERSION) return DEFAULTS;
    return {
      sound: flag(values, "sound", DEFAULTS.sound),
      led: flag(values, "led", DEFAULTS.led),
      usage: flag(values, "usage", DEFAULTS.usage),
    };
  } catch (err) {
    console.error(`${app}: settings unavailable (${err instanceof Error ? err.message : String(err)})`);
    return DEFAULTS;
  }
}
