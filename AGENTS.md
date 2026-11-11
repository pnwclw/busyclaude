# AGENTS.md

Instructions for AI coding agents working on `BusyClaude` (`app.pnwclw.busyclaude`): Claude Code notifications on the BUSY Bar. See [README.md](./README.md) for what it does and how it is set up.

## The two halves

| | where it runs | what it is |
| --- | --- | --- |
| `src/`, `shared/` | on the bar, in the firmware's embedded [JerryScript](https://compat-table.github.io/compat-table/es2016plus/#jerryscript3_0_0) runtime — not Node.js, not a browser | the app: TypeScript, built by `busy-cli` into `dist/<id>.tgz` |
| `busyclaude.py` | on the computer, once per Claude Code hook event | the hook, and `setup` / `test` / `status`: Python 3.9+, standard library only |
| `.claude-plugin/`, `hooks/`, `skills/`, `release/` | in Claude Code, when installed as a plugin | this repository is also a plugin and the marketplace that lists it: the same `busyclaude.py` run from the plugin's copy, and the built app it installs |
| `tools/assets.py` | at development time | generates every `.anim`, `.snd` and icon under `src/` |
| `tools/fakebar.mjs`, `test/` | at development time | a stand-in bar that runs the built app, and the tests that drive the hook against it |
| `tools/shots.py`, `docs/` | at development time | the README's pictures: captures read back from a real bar's display, and what is drawn from them |

There is no third half. Do not add a server, a daemon, a long-running process or a dependency on anything but the bar's HTTP API: a hook starts, talks to the bar, and ends.

### The mailbox

An app on the bar cannot be sent anything. The two halves meet in the app's `localStorage`, which the firmware keeps as one JSON file on the bar (`/ext/apps_data/jsrunner/<id>.localstorage.json`); the hook reads and writes that file through `/api/storage`, then launches the app with `/api/apps/launch`. `src/mailbox.ts` documents the three keys (`inbox`, `answers`, `seen`); `deliver()` and `hand_back()` in `busyclaude.py` are the other side.

When the app has shown an inbox it writes `seen` and closes, and the hook puts the bar back as it was (`Bar.origin()` before the launch, `Bar.restore()` after): the app that was open is launched again in ours' place, anything else is brought back by quitting ours. Do not add a "stay open" mode: an app the user opened by hand comes back by the same route.

- The firmware reads `localStorage` once per launch. A running app never sees a later write, so **every event relaunches the app**. Do not add polling to work around this.
- From firmware 1.4 an app may also use `/api/storage` under `/ext/data`. Until 1.4 is the oldest firmware worth supporting, the app must not: `tools/fakebar.mjs` enforces the 1.3.1 allowlist and the tests fail if it tries.
- A change to the inbox document is a change to both halves and to `INBOX_VERSION` / `VERSION`. `test/tables.test.mjs` holds the names and numbers that must agree across the cable; extend it when you add one.

## Firmware API boundary

The BUSY Bar firmware is an external dependency of this app.

- Never modify or patch BUSY Bar firmware source code as part of work on this project. Treat the firmware repository as read-only reference material.
- If the required capability is not available in the target firmware, use a supported fallback or degrade gracefully. If neither is possible, report the missing capability and required API version; do not implement a firmware change.
- Before treating a capability as missing, check the target device at `http://<device-host>/docs` and `GET /api/version`, or inspect the matching firmware OpenAPI definitions.
- If the target firmware lacks a required capability, use a supported fallback, degrade gracefully, or report the required API version. Do not invent an endpoint or silently depend on unreleased firmware behavior.

## The app: entry point and lifecycle

`src/main.ts` or `src/main.js` is the entry point. The build emits it as `scripts/main.js`, the file the firmware starts. It is a script, not a module: the firmware runs it from top to bottom once, it must not export anything, and it starts the app by calling `main()` at the end.

**`main()` is not run in an infinite loop.** It is a one-time launch. The firmware does not keep the app alive because `main()` was called, and it does not restart it when `main()` returns. The app stays alive only while something the script started is still pending:

- an input listener registered with `listen("input", ...)`;
- a pending `setTimeout` or a running `setInterval`;
- a `fetch` request that has not settled yet.

When `main()` returns, the app keeps running while at least one of those remains. Once the last listener is unbound, every interval is cleared, every timeout is cleared or has fired, and every outstanding `fetch` has settled, the JavaScript process exits and the app closes. The bar takes the screen back.

That makes cleanup part of the app design, not an afterthought:

- Keep the reason the app stays alive explicit: one listener or timer per thing the app is doing.
- To quit, release everything: clear every timer, unbind every listener, and let outstanding fetches settle. There is no exit function — releasing the last handle is the exit.
- Do the same cleanup on every path that ends a mode or the app, not only in the `back` handler.
- Do not unbind the last handler from inside that handler's own callback: the process would stop while the callback is still running. Schedule it instead, as the template does with `setTimeout(unbind, 0)`.
- `listen()` accepts one handler at a time and throws if a handler is already attached. Unbind the old one before attaching another.
- Keep `main()` synchronous and start asynchronous work from it explicitly. Give every promise a rejection handler (`void work().catch(report)` or `try`/`catch`); an unhandled rejection must not be the only error path.
- Do not keep the app open with a dummy long-running timer "just in case", and never busy-wait to stay alive.

The shape of a minimal app:

```ts
function main(): void {
  const timer = setInterval(() => {
    // redraw
  }, 1000);

  const unbind = listen("input", (event) => {
    if (event.key !== "back" || event.action !== "short") return;

    clearInterval(timer);
    // The unbind is the last handle: schedule it so this callback returns first.
    setTimeout(unbind, 0);
  });
}

main();
```

## Conserve device resources

The BUSY Bar is a small embedded device. The JavaScript runtime, the web server that serves the bar's HTTP API, and the rest of the firmware share the same CPU and memory. An app that is greedy slows down the whole bar, not just itself.

### Memory

- `heap_size_kib` in `src/appmeta/manifest.json` is the entire heap for the app: 1–512 KiB, 128 KiB by default. Keep the default unless measurements show the app needs more; a larger heap takes memory from other subsystems. Prefer reducing allocations over raising the heap.
- Avoid large buffers, long-lived caches, and unbounded arrays. Keep only the state the app needs and what is currently on screen.
- Send display elements from the data you already have instead of keeping parallel copies of every frame.
- Parse only the fields you need from responses; do not retain whole payloads.
- Logging costs memory and time. Do not log inside tight loops or per-frame redraws.

### Requests and timers

- Every API call is an HTTP request handled by the device's web server. It costs CPU and memory on the bar and can be slow. Poll on human time scales (seconds or minutes), never per frame and never in a loop.
- Keep at most one display request in flight. When one is still unanswered, skip or coalesce the next update rather than queueing it.
- Do not retry immediately on failure. Use a bounded backoff, and stop retrying while the device reports that it is offline or still connecting.
- Send only what changed. Elements are addressed by `id`, so update an element instead of redrawing the whole screen; clear removed ids once with `DisplayClear` instead of resending an empty frame.
- Run timers only while something needs updating, and clear them when the app is idle. Do not use `setInterval(..., 0)` or a `while` loop as a wait.
- Prefer coarse, aligned timers: redraw on the minute rather than every 100 ms, and refresh remote data every few minutes rather than every few seconds.
- Package images, animations and sounds with the app instead of downloading them at runtime, and keep them small. Do not decode large files into memory.

When in doubt, choose the cheaper option: a slower refresh, fewer requests, less data on screen.

## Display, input and settings

- The screen is 72×16 pixels. `src/screens.ts` builds each screen as a list of elements at fixed coordinates, and `show()` in `src/main.ts` sends it through `displayDraw()` in `shared/device.ts`. Use `manifest.id` as the application name.
- **Nothing is animated from JavaScript.** Motion is an `animation` element naming a `.anim` file and a `section` of it; the bar plays it. If something has to move, add frames to `tools/assets.py` and a section for it. A timer that redraws is a bug, however slow.
- Every screen reuses the same element ids (`IDS` in `src/screens.ts`), so a draw replaces what was there and `show()` clears only the ids the new screen lacks. Give every element an explicit `z_index`: without one the firmware orders by position in the request.
- On firmware 1.3 a short BACK wipes every element the app has drawn, before the app's handler runs (`canvas.c`, `CanvasSrvEventExit`); newer firmware leaves them. So after a button the app always draws the whole next screen, and removing an element that is already gone (HTTP 400) is not an error. `tools/fakebar.mjs` does the same wipe.
- Requests leave one at a time (`shared/device.ts`). A notification is one draw and one sound; keep it that way.
- Text is printable ASCII only (`^[\x20-\x7E]+$`), or the draw is rejected. Both halves strip everything else.
- A permission prompt shows what is about to happen (`prompt_line()` in `busyclaude.py`): the command, the file, the address, never just the name of the tool, because START runs it. It is cut at `PROMPT_TEXT_MAX` with a visible `...`, a character that cannot be shown becomes `?` rather than vanishing, and it scrolls at the faster rate (`HURRY` in `src/screens.ts`) so the whole line passes inside the shortest answer time.
- The bar cannot replace an app while one of its animations is on screen: the file is open, the install fails halfway and leaves no app. `install_app()` takes the app off the display first; keep that.
- Colors are `#RRGGBBAA`. `priority` stays at 50.
- `listen("input", handler)` delivers `ok`, `start` and `back` with a `press`, `release`, `short`, `long` or `repeat` action, and `encoder` with a `delta` of `1` or `-1`. Act on `short`. **START allows a permission prompt and BACK denies it; `ok`, the wheel's own button, does nothing to a prompt.** For a question the wheel moves through the options, shown by number only, and START (or `ok`) takes the one it is on; BACK leaves it to the terminal. Any button waves away a notice that asks nothing. Turning the wheel redraws line two alone (`bodyLine()`), because redrawing the fuse would restart it.
- Settings are described in `src/appmeta/settings.json` and read once at startup by `src/settings.ts` (`GET /api/apps/settings`); an app can read them, not write them. `DEFAULTS` there must mirror the file, and a change to the fields is a new `version` in both. The one setting the app does not read is `wait`, the answer time: the hook reads it (`answer_window()`), because it is the hook that waits, and sends it with each prompt as `window`, which picks the fuse. A `wait` of minutes rather than seconds holds the whole session still that long, so `HOOK_TIMEOUT["PermissionRequest"]` is computed from the longest one and never written by hand, and the poll for an answer backs off after the first minute instead of asking the bar every 0.4 s for ten minutes. The long fuses carry a section every few seconds rather than every second -- 47 pixels over ten minutes is a pixel every thirteen -- and the app rounds the time left up to the nearest section the file has. Do not put secrets in settings.

## Code rules

- The runtime is not Node.js and not a browser: no `fs`, `path`, `process`, `document` or `window`. `console`, `fetch`, timers, `localStorage`, `Date`, `JSON` and standard ECMAScript are available. The build targets ES2020 and does not polyfill; stay with what the official apps use.
- Every `localStorage.setItem` rewrites the whole store to flash. The app writes when a button is pressed and when it is done with an inbox, and at no other time.
- `src/` is the app and the only folder the build scans. `shared/` holds helpers imported as `@shared/*`. The entry point must stay `src/main.ts`, must not export anything, and ends with `main()`.
- TypeScript is strict; run `npm run typecheck`.
- No runtime dependencies. The package is one bundled `scripts/main.js` (`busy-cli build --bundle`), because it is loaded on every event.
- `release/<id>.tgz` is the app as everyone without the build tools installs it, a plugin's copy included. After any change under `src/` or `shared/`, run `npm run package` and commit the result; a test fails while it differs from what the sources build. `npm run build` sets `COPYFILE_DISABLE=1`: without it the tar on a Mac adds an AppleDouble `._` file for every file with extended attributes, and the bar unpacks those as files. `hooks/hooks.json` and `claude/settings.json` are written from `hooks_block()`, and the plugin's version is the manifest's: the tests hold them together.
- The plugin's hooks run `hook --plugin`. Hooks written into `settings.json` by `setup` run `hook`, and stand aside when the plugin is enabled, so nothing is ever shown twice. `setup --plugin` installs the app and writes no hooks.
- Never edit `dist/`, and never edit a file under `src/animations`, `src/sounds` or the icons by hand: change `tools/assets.py` and run `npm run assets`. A test fails when the files on disk are not what the script makes.
- Report failures with `console.error` and the app id as a prefix. A callback that throws must release everything (`guarded()` and `quit()` in `src/main.ts`): a broken app must not sit on the screen.
- `busyclaude.py`: standard library only, Python 3.9 syntax. `hook` prints nothing and exits 0 whatever happens, with one exception, the decision for a `PermissionRequest`; anything else on stdout is read by Claude Code. Everything slow happens in the detached child (`spawn()`), never in the hook itself. Outcomes go to `~/.cache/busyclaude/events.jsonl`.
- Sounds are quiet on purpose (`SOUND_PEAK` in `tools/assets.py`). Do not raise it.
- The mascot is drawn from soft-edged shapes with a brightness per pixel (`mascot_light()` in `tools/assets.py`), not from on/off pixels. At rest its straight edges sit on pixel boundaries, and its feet only ever move by whole pixels; keep it so, or it blurs. Each kind has its own motion and eyes (`pose()`); `python3 busyclaude.py demo` shows them all on the bar.
- Only the item an inbox names as `fresh` gets a tone and the LED. A relaunch that brings nothing new is silent.
- Usage numbers reach the hook through `~/.cache/busyclaude/usage.json` only (`usage_current()`); the status line command, `usage <5h> <7d>` and the fetch all write that file. Never read another program's credentials to get them, and never show numbers older than `USAGE_MAX_AGE`. A fetch never replaces numbers handed over that are still fresh, and a token that disagrees with a hand-over (`usage_crosscheck()`) is not asked again until one agrees: it may speak for another subscription than the one in use. Tests set `BUSYCLAUDE_FETCH=0`, so nothing reaches the network.
- `setup` ends by asking for a button on the bar (`CONFIRM_WAIT`), because every other step would look exactly the same against a bar in another room. Keep `--no-confirm` for anything that runs unattended.
- The app's display name is "Claude Code" (`name` in the manifest: what the bar's menus show). Its id, the repository and everything in code stay `busyclaude`.
- Keep this file and `README.md` accurate when conventions change.

## Before finishing

Run `npm run typecheck` and `npm test` (it builds first), then check:

- [ ] The app quits cleanly on every path: answered, timed out, waved away, an exception. No timer or listener outlives it.
- [ ] Nothing redraws on a timer; a new motion is a new section in a `.anim` file.
- [ ] The number of requests per notification did not grow. A finished turn is twelve, hook and app together.
- [ ] Button activation uses `short`.
- [ ] The app writes to `localStorage` no more often than before.
- [ ] A hook still returns in well under a second for everything but a prompt, and at once when the bar is unplugged.
- [ ] Anything shared by the two halves is covered in `test/tables.test.mjs`.
- [ ] `src/appmeta/manifest.json`, `package.json` and `.claude-plugin/plugin.json` carry the same version, and `api_version` is no higher than needed.
- [ ] `release/` was rebuilt with `npm run package` if the app changed, and `claude plugin validate .` passes.
- [ ] What you could only check on the stand-in bar, say so: it proves the logic, not how it looks or sounds on the device. On a real bar, `GET /api/screen?display=0` returns the front display (base64 of 72x16 pixels, blue-green-red) and `POST /api/log_dump` writes the firmware log, where the app's `console.error` lines land.
