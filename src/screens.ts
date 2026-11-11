// What goes on the 72x16 front display, as lists of elements.
//
//   ┌──────────────────────────────────┐
//   │ ▄██▄  busyclaude                 │   mascot 24x16 at the left, played by the bar from
//   │ █▀▀█  allow Bash?                │   animations/clawd.anim; two lines of text beside it;
//   │ ▀▀▀▀  ━━━━━━━━━━━━━━━            │   under them, the fuse of a prompt that can be answered
//   └──────────────────────────────────┘
//
// Every screen uses the same element ids, so drawing one replaces the other and only the ids a
// screen does not use have to be cleared.

import type { AnimationElement, DisplayElement, RectangleElement, TextElement } from "@shared/device";
import type { Usage } from "./mailbox.ts";

const MASCOT = "animations/clawd.anim";
const WIPE = "animations/wipe.anim";

/**
 * The fuses there are: animations/fuse15.anim and so on. Which one a prompt gets follows from the
 * "Answer time" setting, by way of the hook. `step` is how many seconds apart the file's sections
 * stand; one a second is more than a ten-minute fuse needs, where a single pixel lasts thirteen.
 */
export const FUSES: ReadonlyArray<{ seconds: number; step: number }> = [
  { seconds: 15, step: 1 },
  { seconds: 30, step: 1 },
  { seconds: 60, step: 1 },
  { seconds: 300, step: 5 },
  { seconds: 600, step: 10 },
];

const SCREEN_W = 72;

/** Where the text starts: the mascot's canvas and a pixel of air. */
const TEXT_X = 25;
const TEXT_W = SCREEN_W - TEXT_X;

/** The `small` font is 6px tall and drawn 2px below its y: these put it on rows 2-7 and 9-14. */
const LINE_Y = [0, 7] as const;
const FUSE_Y = 15;

const WHITE = "#FFFFFFFF";

/** Draw order. Set on every element, or the firmware decides by position in the request. */
const Z_MASCOT = 10;
const Z_TEXT = 30;
const Z_TRACK = 40;
const Z_FILL = 50;
const Z_WIPE = 60;

/** "5h 100%" is the widest a usage label gets, about 28px, so the bars start past that. */
const BAR_X = 55;
const BAR_W = 15;
const BAR_H = 3;
/** Centred on each line's ink. */
const BAR_Y = [3, 10] as const;
const TRACK = "#303030FF";

/** Every id a screen may use, for knowing what to clear. */
export const IDS = {
  mascot: "m",
  lines: ["t0", "t1"],
  fuse: "f",
  bars: ["u0", "v0", "u1", "v1"],
  wipe: "w",
} as const;

function mascot(section: string, loop: boolean): AnimationElement {
  return { id: IDS.mascot, type: "animation", path: MASCOT, section, loop, x: 0, y: 0, z_index: Z_MASCOT };
}

/** How a line that does not fit moves: pixels a minute, and the pauses before and between passes, in ms. */
interface Scroll {
  rate: number;
  start: number;
  repeat: number;
}

/** A project name or a status that runs long: unhurried. */
const STROLL: Scroll = { rate: 900, start: 900, repeat: 2200 };

/**
 * The command in a prompt: twice as fast, so that the hundred characters the hook allows pass
 * once within the shortest answer time (about eleven seconds on a bar, where they were timed).
 * Slower, and the fuse burns out before the end of the line.
 */
const HURRY: Scroll = { rate: 1800, start: 700, repeat: 1500 };

function text(row: 0 | 1, value: string, width: number, scroll: Scroll = STROLL): TextElement {
  return {
    id: IDS.lines[row],
    type: "text",
    text: value,
    font: "small",
    color: WHITE,
    x: TEXT_X,
    y: LINE_Y[row],
    width,
    scroll_rate: scroll.rate,
    scroll_start_delay: scroll.start,
    scroll_repeat_delay: scroll.repeat,
    z_index: Z_TEXT,
  };
}

function rect(id: string, x: number, y: number, width: number, color: string, z: number): RectangleElement {
  return {
    id,
    type: "rectangle",
    x,
    y,
    width,
    height: BAR_H,
    fill: "solid",
    fill_colors: [color],
    border_width: 0,
    z_index: z,
  };
}

/** Green, amber, red: the colours of done, permission and error, so a full bar reads as trouble. */
function usageColor(percent: number): string {
  if (percent < 50) return "#00FF66FF";
  if (percent < 85) return "#FF8800FF";
  return "#FF2244FF";
}

/** The mascot and two lines: a notification, or an entry of the history. */
export function notice(section: string, title: string, body: string): DisplayElement[] {
  return [mascot(section, true), text(0, title, TEXT_W), text(1, body, TEXT_W)];
}

/**
 * A notice that can be answered, with the fuse burning under it.
 * @param secondsLeft - How long the hook will still take an answer.
 * @param window - How long it had to begin with.
 */
export function prompt(section: string, title: string, body: string, secondsLeft: number, window: number): DisplayElement[] {
  // The shortest fuse that is long enough, so the bar starts full.
  let burns = FUSES[FUSES.length - 1]!;
  for (let i = FUSES.length - 1; i >= 0; i--) if (FUSES[i]!.seconds >= window) burns = FUSES[i]!;
  // A fuse carries a section for every `step` seconds left, so a prompt that comes back to the
  // screen picks up where its time stands. Rounded up to the next section the file has: one that
  // started a little early would go out while the hook was still listening.
  const left = Math.max(1, Math.min(burns.seconds, Math.ceil(secondsLeft)));
  const start = Math.min(burns.seconds, Math.ceil(left / burns.step) * burns.step);
  const fuse: AnimationElement = {
    id: IDS.fuse,
    type: "animation",
    path: `animations/fuse${burns.seconds}.anim`,
    section: `t${start}`,
    loop: false,
    x: TEXT_X,
    y: FUSE_Y,
    z_index: Z_TEXT,
  };
  // What it asks for is the line that has to be read before a button is pressed.
  return [mascot(section, true), text(0, title, TEXT_W), text(1, body, TEXT_W, HURRY), fuse];
}

/** The mascot, and what is left of the 5-hour and weekly limits as two labelled bars. */
export function usage(section: string, value: Usage): DisplayElement[] {
  const elements: DisplayElement[] = [mascot(section, true)];
  const rows = [
    { label: "5h", percent: value.five },
    { label: "7d", percent: value.week },
  ];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]!;
    const percent = Math.max(0, Math.min(100, row.percent));
    const y = BAR_Y[i as 0 | 1];
    // The label stops short of the bar, so a long one scrolls instead of running under it.
    elements.push(text(i as 0 | 1, `${row.label} ${Math.round(percent)}%`, BAR_X - TEXT_X - 1));
    elements.push(rect(IDS.bars[i * 2]!, BAR_X, y, BAR_W, TRACK, Z_TRACK));
    // Rounded up, so any usage at all is a lit pixel; a rectangle cannot be zero wide.
    const filled = Math.max(1, Math.min(BAR_W, Math.ceil((percent / 100) * BAR_W)));
    elements.push(rect(IDS.bars[i * 2 + 1]!, BAR_X, y, filled, percent > 0 ? usageColor(percent) : TRACK, Z_FILL));
  }
  return elements;
}

/** What line two says while the wheel picks an option. The options themselves are in the terminal: only the number fits here. */
export function optionText(choice: number, count: number): string {
  return `option ${choice}/${count}`;
}

/** Line two alone, for changing it without redrawing, and so restarting, anything that moves. */
export function bodyLine(body: string): DisplayElement[] {
  return [text(1, body, TEXT_W)];
}

/** The mascot answering a button: a nod for yes, a shake for no, played once. */
export function verdict(section: "allow" | "deny", title: string, body: string): DisplayElement[] {
  return [mascot(section, false), text(0, title, TEXT_W), text(1, body, TEXT_W)];
}

/** A CRT switching off, over whatever is up. Four frames, played once by the bar. */
export function wipe(): DisplayElement[] {
  return [{ id: IDS.wipe, type: "animation", path: WIPE, section: "default", loop: false, x: 0, y: 0, z_index: Z_WIPE }];
}

/** The ids of `elements`. */
export function idsOf(elements: DisplayElement[]): string[] {
  const ids: string[] = [];
  for (let i = 0; i < elements.length; i++) ids.push(elements[i]!.id);
  return ids;
}
