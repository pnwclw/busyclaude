// The mailbox: how a Claude Code hook on the computer and this app on the bar talk.
//
// An app on the bar cannot be sent anything, and on firmware 1.3 it cannot read files either. What
// it does have is `localStorage`, which the firmware keeps as one small JSON file on the bar's
// storage; the hook writes that file through the bar's storage API and then launches the app, and
// reads it back for the answer. So everything here is three keys:
//
//   inbox    written by the hook: what to show, as one JSON document
//   answers  written by the app: {"<item id>": "allow" | "deny" | "<option number>" | "skip"}
//   seen     written by the app: the inbox it has finished showing
//
// The store is read once per launch (the firmware caches it), which is why every event relaunches
// the app instead of being noticed by a running one. Every `setItem` rewrites the whole file to
// flash, so the app writes when a button is pressed and when it is done, and never otherwise.

export interface Item {
  id: string;
  /** A name from kinds.ts: perm, question, ask, done, agent, error, hello. */
  kind: string;
  /** Line one: the project. */
  title: string;
  /** Line two: what happened. */
  text: string;
  /** Seconds it is still worth showing, counted from when the hook wrote the inbox. */
  ttl: number;
  /** True when a hook is waiting for a button: a permission prompt, or a question with options. */
  ask: boolean;
  /** How many options a question has to choose from; 0 for everything else. */
  options: number;
  /** Seconds a prompt or question had to be answered in when it was new: which fuse to burn. */
  window: number;
}

export interface Past {
  kind: string;
  title: string;
  text: string;
}

export interface Usage {
  /** Percent of the 5-hour limit used. */
  five: number;
  /** Percent of the weekly limit used. */
  week: number;
  /** When the numbers were true, in Unix seconds; 0 when the hook did not say. */
  at: number;
}

export interface Inbox {
  /** Changes on every write; `seen` echoes it back. */
  n: string;
  /** The item this launch is about: the only one that makes a sound. */
  fresh: string;
  items: Item[];
  history: Past[];
  usage?: Usage;
}

/** What a button said: yes or no to a prompt, an option's number, or "skip" to leave it to the terminal. */
export type Answer = string;

/** The most options a question may have and still be answered by number. */
const OPTIONS_MAX = 9;

const INBOX_KEY = "inbox";
const ANSWERS_KEY = "answers";
const SEEN_KEY = "seen";

/** The document format this build reads. */
const VERSION = 1;

/** The most a line of text may hold: a command in a prompt is cut at 100 by the hook, and nothing else comes close. */
const TEXT_MAX = 128;

function read(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? undefined : (JSON.parse(raw) as unknown);
  } catch {
    return undefined;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    console.error(`mailbox: ${key} not saved: ${err instanceof Error ? err.message : String(err)}`);
  }
}

/** Printable ASCII only: it is all the bar's fonts hold, and the draw API rejects the rest. */
function line(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  let out = "";
  for (let i = 0; i < value.length && out.length < TEXT_MAX; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0x20 && code <= 0x7e) out += value[i];
  }
  out = out.trim();
  return out === "" ? fallback : out;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readItems(value: unknown): Item[] {
  const items: Item[] = [];
  if (!Array.isArray(value)) return items;
  for (let i = 0; i < value.length; i++) {
    const raw: unknown = value[i];
    if (!isRecord(raw)) continue;
    const id = raw["id"];
    const ttl = raw["ttl"];
    if (typeof id !== "string" || id === "" || typeof ttl !== "number" || !(ttl > 0)) continue;
    const options = raw["options"];
    const window = raw["window"];
    items.push({
      id,
      kind: line(raw["kind"], "ask"),
      title: line(raw["title"], "claude"),
      text: line(raw["text"], "your turn"),
      ttl,
      ask: raw["ask"] === true,
      options: typeof options === "number" && options >= 2 && options <= OPTIONS_MAX ? Math.floor(options) : 0,
      window: typeof window === "number" && window > 0 ? window : ttl,
    });
  }
  return items;
}

function readHistory(value: unknown): Past[] {
  const history: Past[] = [];
  if (!Array.isArray(value)) return history;
  for (let i = 0; i < value.length; i++) {
    const raw: unknown = value[i];
    if (!isRecord(raw)) continue;
    history.push({
      kind: line(raw["kind"], "ask"),
      title: line(raw["title"], "claude"),
      text: line(raw["text"], "event"),
    });
  }
  return history;
}

function readUsage(value: unknown): Usage | undefined {
  if (!isRecord(value)) return undefined;
  const five = value["five"];
  const week = value["week"];
  if (typeof five !== "number" || typeof week !== "number") return undefined;
  const at = value["at"];
  return { five, week, at: typeof at === "number" ? at : 0 };
}

/** What the hook left, or undefined when it has never written anything this build understands. */
export function readInbox(): Inbox | undefined {
  const raw = read(INBOX_KEY);
  if (!isRecord(raw) || raw["v"] !== VERSION || typeof raw["n"] !== "string") return undefined;
  return {
    n: raw["n"],
    fresh: typeof raw["fresh"] === "string" ? raw["fresh"] : "",
    items: readItems(raw["items"]),
    history: readHistory(raw["history"]),
    usage: readUsage(raw["usage"]),
  };
}

/** True when this inbox has already been shown: the app was opened by hand, not by an event. */
export function alreadySeen(inbox: Inbox): boolean {
  const seen = read(SEEN_KEY);
  return isRecord(seen) && seen["n"] === inbox.n;
}

/** Records that the inbox has been shown. This is what tells the hook to put the bar back as it was. */
export function markSeen(inbox: Inbox): void {
  write(SEEN_KEY, { n: inbox.n });
}

/** Records a button press for the hook to pick up. Answers to items no longer in the inbox are dropped. */
export function answer(inbox: Inbox, id: string, value: Answer): void {
  const kept: Record<string, Answer> = {};
  const old = read(ANSWERS_KEY);
  if (isRecord(old)) {
    for (let i = 0; i < inbox.items.length; i++) {
      const known = inbox.items[i]!.id;
      const earlier = old[known];
      if (typeof earlier === "string") kept[known] = earlier;
    }
  }
  kept[id] = value;
  write(ANSWERS_KEY, kept);
}
