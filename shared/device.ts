// The bar's HTTP API. Everything the app sends to the BUSY Bar goes through here.
//
// Requests leave one at a time, in the order they were asked for: the web server that answers
// them shares a CPU with everything else on the bar, and a draw overtaking the clear before it
// would leave the wrong picture up.

export type Font = "tiny" | "small" | "normal" | "condensed" | "bold" | "large";

interface Placed {
  /** The device replaces an element that has the same id. */
  id: string;
  x: number;
  y: number;
  /** Set on every element: without one the firmware orders by position in the request. */
  z_index: number;
}

export interface TextElement extends Placed {
  type: "text";
  text: string;
  font: Font;
  /** #RRGGBBAA. */
  color: string;
  /** Clips the text; anything wider scrolls inside it. */
  width: number;
  scroll_rate: number;
  scroll_start_delay: number;
  scroll_repeat_delay: number;
}

export interface AnimationElement extends Placed {
  type: "animation";
  /** Relative to the app's package: "animations/clawd.anim". */
  path: string;
  /** A named range of frames inside the file. */
  section: string;
  loop: boolean;
}

export interface RectangleElement extends Placed {
  type: "rectangle";
  width: number;
  height: number;
  fill: "solid";
  fill_colors: string[];
  border_width: 0;
}

export type DisplayElement = TextElement | AnimationElement | RectangleElement;

export interface SettingsDocument {
  version: number;
  values: Record<string, unknown>;
}

/** Device address, set at build time with `busy-cli build --addr`; `localhost` on the bar itself. */
const addr = __BUSY_ADDR__;
const API = `${/^https?:\/\//i.test(addr) ? addr : `http://${addr}`}/api`;

const JSON_HEADERS = { "Content-Type": "application/json" };

/** The tail of the queue: each request starts when the one before it has been answered. */
let tail: Promise<void> = Promise.resolve();

/**
 * Queues a request whose answer nobody waits for. A failure is reported and the queue moves on.
 * @param expected - A status that is not a failure for this request, besides 2xx.
 */
function post(app: string, label: string, path: string, method: string, body: string, expected?: number): void {
  tail = tail.then(() =>
    fetch(`${API}${path}`, { method, headers: JSON_HEADERS, body }).then(
      (res) => {
        if (!res.ok && res.status !== expected) console.error(`${app}: ${label} failed: HTTP ${res.status}`);
      },
      (err) => {
        console.error(`${app}: ${label} failed: ${err instanceof Error ? err.message : String(err)}`);
      },
    ),
  );
}

/** Draws elements. `led` blinks the status LED in that colour. */
export function displayDraw(app: string, elements: DisplayElement[], led?: string): void {
  const request: Record<string, unknown> = { application_name: app, priority: 50, elements };
  if (led !== undefined) request["led_notification_color"] = led;
  post(app, "draw", "/display/draw", "POST", JSON.stringify(request));
}

/** What the bar answers when asked to remove an element that is not there. */
const ALREADY_GONE = 400;

/**
 * Removes elements by id.
 *
 * The bar may have removed them first: on firmware 1.3 a short BACK wipes everything an app has
 * drawn before the app hears about the button. Asking for an element that is gone is refused
 * with a 400, which here means the work is already done.
 */
export function displayClear(app: string, ids: string[]): void {
  post(app, "clear", "/display/draw", "DELETE", JSON.stringify({ application_name: app, element_ids: ids }), ALREADY_GONE);
}

/** Plays a sound from the app's package: "sounds/done.snd". */
export function audioPlay(app: string, path: string): void {
  post(app, "play", "/audio/play", "POST", JSON.stringify({ application_name: app, path }));
}

/** The values set on the app's Setup screen. */
export async function getAppSettings(app: string): Promise<SettingsDocument> {
  const res = await fetch(`${API}/apps/settings?app_id=${encodeURIComponent(app)}`);
  if (!res.ok) throw new Error(`settings: HTTP ${res.status}`);
  return (await res.json()) as SettingsDocument;
}
