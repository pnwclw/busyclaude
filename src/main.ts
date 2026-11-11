// BusyClaude: what Claude Code wants from you, on the BUSY Bar.
//
// A hook on the computer writes the inbox (see mailbox.ts) and launches this app. The app shows
// what is in it, one item at a time, and closes. START allows a permission prompt and BACK denies
// it; for a question the wheel picks an option by number and START takes it. Opened by hand
// instead, it shows the usage numbers and the last few notifications until BACK.
//
// Nothing here animates. The mascot, the fuse under a prompt and the closing wipe are .anim files
// the bar plays by itself, so a notification is one draw request and one sound request, and the
// only thing the app waits on is a single timer: the moment the item on screen is over.

import { audioPlay, displayClear, displayDraw, type DisplayElement } from "@shared/device";
import manifest from "./appmeta/manifest.json";
import { kindOf, showsUsage } from "./kinds.ts";
import { alreadySeen, answer, markSeen, readInbox, type Answer, type Inbox, type Item, type Usage } from "./mailbox.ts";
import { bodyLine, idsOf, notice, optionText, prompt, usage, verdict, wipe } from "./screens.ts";
import { DEFAULTS, loadSettings, type Settings } from "./settings.ts";

/** The name the app draws under; the device clears elements by this id. */
const APP = manifest.id;

/** How long a notice stays up when others are waiting behind it. */
const CROWDED_MS = 4000;

/** The share of a finished turn's time on screen spent on its text; the usage numbers get the rest. */
const USAGE_AFTER = 0.45;

/** Usage numbers older than this are not shown at all: a stale "42%" is worse than no number. */
const USAGE_MAX_AGE_S = 15 * 60;

/** How long "allowed" or "denied" stays up after a button. */
const VERDICT_MS = 900;

/** The closing wipe is four frames at twelve a second. */
const WIPE_MS = 400;

/** An item with the moment it stops being worth showing, on the bar's clock. */
interface Live {
  item: Item;
  until: number;
}

type Mode =
  /** Settings are being read; nothing is on screen yet. */
  | "starting"
  /** An item of the queue is up. */
  | "notify"
  /** A button was pressed; its result is up. */
  | "verdict"
  /** Opened by hand: usage and history, until BACK. */
  | "idle"
  /** Everything is being let go of. */
  | "closing";

let mode: Mode = "starting";
let settings: Settings = DEFAULTS;

/** What the hook left. Undefined when no hook has ever written to this bar. */
let inbox: Inbox | undefined;

/** True when this launch is the hook's doing: there is an inbox nobody has shown yet. */
let summoned = false;

/** What is still to be shown, and which of it is up. */
let queue: Live[] = [];
let cursor = 0;

/** The option the wheel is on, counted from 1, while a question is up. */
let choice = 1;

/** The page of the idle screen: 0 is the usage, 1 and up walk back through the history. */
let page = 0;

/** The ids on screen, so that a new screen clears only what it does not redraw. */
let shown: string[] = [];

/** The one thing the app is waiting for: the end of what is on screen. */
let timer: ReturnType<typeof setTimeout> | undefined;

/** Swaps a finished turn's text for the usage numbers partway through. */
let swapTimer: ReturnType<typeof setTimeout> | undefined;

let unbind: BusyUnbind | undefined;

function report(err: unknown): void {
  console.error(`${APP}: ${err instanceof Error ? err.message : String(err)}`);
}

function clearTimers(): void {
  if (timer !== undefined) clearTimeout(timer);
  if (swapTimer !== undefined) clearTimeout(swapTimer);
  timer = undefined;
  swapTimer = undefined;
}

/** Runs `step`, and lets go of everything if it throws: a broken app must not sit on the screen. */
function guarded(step: () => void): () => void {
  return () => {
    try {
      step();
    } catch (err) {
      report(err);
      quit();
    }
  };
}

/**
 * The usage numbers, if they are recent enough to be worth showing. The inbox can be hours old
 * by the time the app is opened by hand, and the hook that wrote it is long gone; the bar's own
 * clock is what says so.
 */
function recentUsage(): Usage | undefined {
  const numbers = inbox === undefined ? undefined : inbox.usage;
  if (numbers === undefined || Date.now() / 1000 - numbers.at > USAGE_MAX_AGE_S) return undefined;
  return numbers;
}

/** Puts a screen up: one draw, and one clear for whatever the last screen had that this one lacks. */
function show(elements: DisplayElement[], led?: string): void {
  const ids = idsOf(elements);
  const stale = shown.filter((id) => ids.indexOf(id) === -1);
  displayDraw(APP, elements, led);
  if (stale.length > 0) displayClear(APP, stale);
  shown = ids;
}

/** The inbox as a queue: the item this launch is about, then prompts, then the rest, oldest first. */
function lineUp(from: Inbox, now: number): Live[] {
  const rank = (item: Item) => (item.id === from.fresh ? 0 : item.ask ? 1 : 2);
  const lives: Live[] = [];
  for (let i = 0; i < from.items.length; i++) {
    const item = from.items[i]!;
    lives.push({ item, until: now + item.ttl * 1000 });
  }
  // Array.prototype.sort is not stable everywhere; the index breaks ties.
  return lives
    .map((live, index) => ({ live, index }))
    .sort((a, b) => rank(a.live.item) - rank(b.live.item) || a.index - b.index)
    .map((entry) => entry.live);
}

/**
 * Shows the item under the cursor and arms the timer for its end.
 * @param announce - True the first time an item is shown: it gets its tone and the LED.
 */
function present(announce: boolean): void {
  clearTimers();
  mode = "notify";

  const live = queue[cursor]!;
  const item = live.item;
  const kind = kindOf(item.kind);
  const now = Date.now();
  const waiting = queue.length - 1;
  const title = waiting > 0 ? `${item.title} +${waiting}` : item.title;
  const led = announce && settings.led ? kind.color : undefined;

  let end = live.until;
  if (item.ask) {
    // A prompt stays for as long as the hook is still listening, and not a moment longer.
    choice = 1;
    const asksWhich = item.options > 0;
    const body = asksWhich ? optionText(choice, item.options) : item.text;
    show(prompt(kind.section, title, body, (live.until - now) / 1000, item.window), led);
  } else {
    show(notice(kind.section, title, item.text), led);
    end = Math.min(live.until, now + (waiting > 0 ? Math.min(CROWDED_MS, kind.seconds * 1000) : kind.seconds * 1000));

    const numbers = recentUsage();
    if (settings.usage && numbers !== undefined && showsUsage(item.kind)) {
      swapTimer = setTimeout(
        guarded(() => {
          swapTimer = undefined;
          show(usage(kind.section, numbers));
        }),
        Math.max(0, (end - now) * USAGE_AFTER),
      );
    }
  }

  if (announce && settings.sound && kind.tone !== undefined) audioPlay(APP, `sounds/${kind.tone}.snd`);

  timer = setTimeout(guarded(over), Math.max(0, end - now));
}

/** The item on screen is finished with: it timed out, was answered, or was waved away. */
function over(): void {
  queue.splice(cursor, 1);
  advance();
}

/** Moves on to whatever is still worth showing, or finishes. */
function advance(): void {
  clearTimers();
  const now = Date.now();
  queue = queue.filter((live) => live.until > now);
  if (queue.length === 0) {
    finish();
    return;
  }
  if (cursor >= queue.length) cursor = 0;
  present(false);
}

/** The queue is empty. Launched by an event, say so and close; opened by hand, stay. */
function finish(): void {
  if (summoned && inbox !== undefined) {
    // This is what the hook waits for before it puts the bar back the way it was.
    markSeen(inbox);
    summoned = false;
    close();
  } else {
    idle();
  }
}

/** The screen of an app with nothing to announce: the usage numbers, or the history behind them. */
function idle(): void {
  clearTimers();
  mode = "idle";

  const history = inbox === undefined ? [] : inbox.history;
  if (page > history.length) page = 0;

  if (page > 0) {
    const past = history[page - 1]!;
    show(notice(kindOf(past.kind).section, past.title, past.text));
    return;
  }

  const numbers = recentUsage();
  if (numbers !== undefined) show(usage("idle", numbers));
  else show(notice("idle", "Claude Code", inbox === undefined ? "no events yet" : "all quiet"));
}

/** A button on a prompt or a question: record it for the hook, say so, move on. */
function decide(value: Answer): void {
  const live = queue[cursor]!;
  if (inbox !== undefined) answer(inbox, live.item.id, value);
  queue.splice(cursor, 1);

  clearTimers();
  mode = "verdict";
  if (value === "deny") show(verdict("deny", live.item.title, "denied"));
  else show(verdict("allow", live.item.title, value === "allow" ? "allowed" : `option ${value}`));
  timer = setTimeout(guarded(advance), VERDICT_MS);
}

/** BACK on a question: leave it to the terminal, and tell the hook to stop waiting. */
function skip(): void {
  const live = queue[cursor]!;
  if (inbox !== undefined) answer(inbox, live.item.id, "skip");
  over();
}

/** Plays the wipe over whatever is up, then lets go. */
function close(): void {
  clearTimers();
  mode = "closing";
  displayDraw(APP, wipe());
  timer = setTimeout(quit, WIPE_MS);
}

/** Releases everything the app holds. With no timer and no listener left, the bar takes the screen back. */
function quit(): void {
  clearTimers();
  mode = "closing";
  const detach = unbind;
  unbind = undefined;
  // Not from inside the handler itself: detaching the last handle stops the app on the spot.
  if (detach !== undefined) setTimeout(detach, 0);
}

function onEncoder(delta: number): void {
  const live = mode === "notify" ? queue[cursor] : undefined;
  if (live !== undefined && live.item.ask && live.item.options > 0) {
    // The wheel picks an option. Only the line with the number is sent: redrawing the rest
    // would start the fuse over.
    const count = live.item.options;
    choice = ((choice - 1 + delta + count) % count) + 1;
    displayDraw(APP, bodyLine(optionText(choice, count)));
  } else if (live !== undefined && queue.length > 1) {
    cursor = (cursor + delta + queue.length) % queue.length;
    present(false);
  } else if (mode === "idle") {
    const pages = 1 + (inbox === undefined ? 0 : inbox.history.length);
    page = (page + delta + pages) % pages;
    idle();
  }
}

function onInput(event: BusyInputEvent): void {
  if (event.key === "encoder") {
    onEncoder(event.delta);
    return;
  }
  if (event.action !== "short") return;

  if (mode === "idle") {
    if (event.key === "back") close();
    return;
  }
  if (mode !== "notify") return;

  const item = queue[cursor]!.item;
  if (!item.ask) {
    // Nothing to answer: any button waves it away.
    over();
  } else if (item.options > 0) {
    // A question. START takes the option the wheel is on, and so does pressing the wheel.
    if (event.key === "back") skip();
    else decide(String(choice));
  } else if (event.key === "start") {
    decide("allow");
  } else if (event.key === "back") {
    decide("deny");
  }
  // The wheel's button does nothing to a permission prompt: it is too easy to press by accident.
}

/** Settings are in: show what the hook left, or the idle screen when it left nothing new. */
function begin(loaded: Settings): void {
  settings = loaded;
  if (summoned && inbox !== undefined) queue = lineUp(inbox, Date.now());

  // Only the item this launch is about gets its tone and the LED. An inbox with nothing new in
  // it (what is left after another session's item was taken down, or a demo) arrives quietly.
  if (queue.length > 0) present(inbox !== undefined && queue[0]!.item.id === inbox.fresh);
  else finish();
}

/** Entry point, called at the end of the file: the device runs this script from top to bottom, once. */
function main(): void {
  inbox = readInbox();
  summoned = inbox !== undefined && !alreadySeen(inbox);

  unbind = listen("input", (event) => {
    try {
      onInput(event);
    } catch (err) {
      report(err);
      quit();
    }
  });

  // loadSettings falls back to the defaults rather than rejecting; the catch is for begin itself.
  void loadSettings(APP)
    .then(begin)
    .catch((err: unknown) => {
      report(err);
      quit();
    });
}

main();
