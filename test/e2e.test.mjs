// The whole path with no hardware: a Claude Code hook (busyclaude.py), the bar's
// HTTP API and JavaScript runtime (tools/fakebar.mjs), and the built app
// (dist/, so run `npm run build` first; `npm test` does).

import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { after, before, beforeEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { FakeBar } from '../tools/fakebar.mjs'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const HOST = join(ROOT, 'busyclaude.py')
const APP = JSON.parse(readFileSync(join(ROOT, 'src/appmeta/manifest.json'), 'utf8')).id
const PACKAGE = join(ROOT, 'dist', `${APP}.tgz`)
const PYTHON = process.env.PYTHON || 'python3'

let bar
let port
let home
let project

/** Runs busyclaude.py with a home of its own, against the fake bar. */
function host(args, { stdin, env = {}, script = HOST } = {}) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, [script, ...args], {
      env: { ...process.env, HOME: home, BUSYBAR_ADDR: `127.0.0.1:${port}`, BUSYCLAUDE_USAGE: '0', BUSYCLAUDE_FETCH: '0', CLAUDE_PROJECT_DIR: project, ...env },
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => (stdout += d))
    child.stderr.on('data', (d) => (stderr += d))
    child.on('close', (code) => resolve({ code, stdout, stderr }))
    child.stdin.end(stdin === undefined ? '' : JSON.stringify(stdin))
  })
}

const hook = (payload, options) => host(['hook'], { stdin: payload, ...options })

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** Waits until `probe` returns something truthy, and returns it. */
async function until(what, probe, ms = 8000) {
  const deadline = Date.now() + ms
  for (;;) {
    const value = probe()
    if (value) return value
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${what}\n${JSON.stringify(bar.state(), null, 1).slice(0, 3000)}`)
    await sleep(25)
  }
}

/** What the app has on the display, as {id: element}. */
const screen = () => bar.state().screen[APP] || {}
const line = (row) => (screen()[`t${row}`] || {}).text
const showing = (text) => until(`"${text}" on screen`, () => line(1) === text && screen())
/** Requests leave the app one at a time, so the tone lands a moment after the picture. */
const sounds = (count) => until(`${count} sound(s)`, () => bar.state().sounds.length >= count && bar.state().sounds)
const backToNormal = (ms) => until('the bar to be handed back', () => bar.state().launcher === null, ms)
const hostLog = () => readFileSync(join(home, '.cache/busyclaude/events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l))
const mailbox = () => JSON.parse(bar.state().files[`/ext/apps_data/jsrunner/${APP}.localstorage.json`]).data

const permission = (session, tool = 'Bash') => ({ hook_event_name: 'PermissionRequest', session_id: session, tool_name: tool })

const question = (session, questions) => ({ hook_event_name: 'PermissionRequest', session_id: session, tool_name: 'AskUserQuestion', tool_input: { questions } })
const FRAMEWORK = {
  question: 'Which framework?',
  header: 'Framework',
  multiSelect: false,
  options: [{ label: 'React', description: 'Component library' }, { label: 'Vue', description: 'Progressive framework' }, { label: 'Svelte', description: 'Compiler' }],
}

async function install(body = readFileSync(PACKAGE)) {
  const staged = await fetch(`http://127.0.0.1:${port}/api/apps/stage`, { method: 'POST', body }).then((r) => r.json())
  await fetch(`http://127.0.0.1:${port}/api/apps/install?install_key=${staged.install_key}`, { method: 'POST' })
}

/** Another app on the bar, the way a clock would be: it draws something and stays. */
const CLOCK = 'app.test.clock'
async function installClock() {
  const dir = mkdtempSync(join(tmpdir(), 'busyclaude-clock-'))
  mkdirSync(join(dir, CLOCK, 'appmeta'), { recursive: true })
  mkdirSync(join(dir, CLOCK, 'scripts'))
  writeFileSync(join(dir, CLOCK, 'appmeta', 'manifest.json'), JSON.stringify({ format_version: 1, id: CLOCK, name: 'Clock', version: '1.0.0', api_version: '27.9.0' }))
  writeFileSync(
    join(dir, CLOCK, 'scripts', 'main.js'),
    `fetch("http://localhost/api/display/draw", { method: "POST", body: JSON.stringify({ application_name: "${CLOCK}", elements: [{ id: "time", type: "text", text: "12:00", font: "small", x: 0, y: 0 }] }) });
     listen("input", function () {});`,
  )
  await install(execFileSync('tar', ['-czf', '-', '-C', dir, CLOCK]))
  rmSync(dir, { recursive: true, force: true })
}

/** The user picks an app in the bar's Apps menu: it runs, and the menu remembers it. */
async function openFromAppsMenu(id) {
  bar.files.set('/ext/apps_data/apps_menu/settings.json', Buffer.from(JSON.stringify({ version: 2, values: { active_application: id } })))
  await fetch(`http://127.0.0.1:${port}/api/apps/launch?app_id=${id}`, { method: 'POST' })
}

before(() => {
  assert.ok(existsSync(PACKAGE), `${PACKAGE} is missing: run npm run build`)
})

beforeEach(async () => {
  if (bar) await bar.shutdown()
  if (home) rmSync(home, { recursive: true, force: true })
  home = mkdtempSync(join(tmpdir(), 'busyclaude-home-'))
  project = join(home, 'api-server')
  mkdirSync(project)
  bar = new FakeBar()
  port = await bar.listen()
})

after(async () => {
  if (bar) await bar.shutdown()
  if (home) rmSync(home, { recursive: true, force: true })
})

test('setup installs the app, replaces old hooks, says hello and hands the screen back', async () => {
  const settings = join(home, '.claude', 'settings.json')
  mkdirSync(dirname(settings))
  const foreign = { type: 'command', command: 'echo not ours' }
  const legacy = { type: 'command', command: '/usr/bin/python3', args: ['/Users/x/PetProjects/busyclaude/app.py', 'hook', 'wss://example'], timeout: 10 }
  writeFileSync(settings, JSON.stringify({ theme: 'dark', hooks: { Stop: [{ hooks: [foreign, legacy] }], PreToolUse: [{ matcher: 'Bash', hooks: [foreign] }], Notification: [{ hooks: [legacy] }] } }))

  const run = host(['setup', '--package', PACKAGE, '--settings', settings])
  const hello = await showing('press START')
  assert.equal(hello.m.section, 'hello')
  assert.equal(hello.m.path, 'animations/clawd.anim')
  assert.equal(hello.f.path, 'animations/fuse30.anim', 'the confirmation burns half a minute of its own')
  bar.press('start')
  const { code, stdout, stderr } = await run
  assert.equal(code, 0, stderr)
  assert.match(stdout, /installed Claude Code v\d/)
  assert.match(stdout, /START: that is the right bar/, 'the button that came back is what setup reports')

  // The app closed by itself, and the hook gave the screen back.
  assert.equal(bar.state().launcher, null)
  assert.deepEqual(bar.state().sounds, [], 'hello is silent')
  assert.deepEqual(bar.state().errors, [])

  const written = JSON.parse(readFileSync(settings, 'utf8'))
  assert.equal(written.theme, 'dark')
  assert.deepEqual(written.hooks.PreToolUse, [{ matcher: 'Bash', hooks: [foreign] }], "someone else's hook is left alone")
  assert.deepEqual(written.hooks.Stop[0], { hooks: [foreign] })
  assert.equal(JSON.stringify(written).includes('app.py'), false, 'the old generation is gone')
  const events = Object.keys(written.hooks).filter((e) => JSON.stringify(written.hooks[e]).includes('busyclaude.py'))
  assert.equal(events.length, 11)
  assert.equal(written.hooks.PermissionRequest[0].hooks[0].timeout, 620, 'long enough to outlast the longest answer time')
  assert.ok(existsSync(`${settings}.before-busyclaude`))

  // Running it again changes nothing: no duplicates.
  await host(['uninstall', '--settings', settings])
  const cleaned = JSON.parse(readFileSync(settings, 'utf8'))
  assert.deepEqual(Object.keys(cleaned.hooks).sort(), ['PreToolUse', 'Stop'])
})

test('setup --no-confirm waits for nobody', async () => {
  const settings = join(home, '.claude', 'settings.json')
  mkdirSync(dirname(settings))
  const { code, stdout, stderr } = await host(['setup', '--no-confirm', '--package', PACKAGE, '--settings', settings])
  assert.equal(code, 0, stderr)
  assert.match(stdout, /hello: shown/)
  assert.doesNotMatch(stdout, /press START/)
  await backToNormal()
})

test('START on the bar allows a permission prompt', async () => {
  await install()
  const run = hook(permission('s1'))
  const up = await showing('allow Bash?')
  assert.equal(up.t0.text, 'api-server', 'line one is the project, from CLAUDE_PROJECT_DIR')
  assert.equal(up.m.section, 'perm')
  assert.equal(up.f.path, 'animations/fuse60.anim', 'a minute, out of the box')
  assert.match(up.f.section, /^t(5[89]|60)$/, 'the fuse starts with about a minute on it')
  assert.equal(up.f.loop, false)
  assert.deepEqual(await sounds(1), ['sounds/perm.snd'])
  assert.deepEqual(bar.state().leds, ['#FF8800FF'])

  // The wheel's own button is too easy to press by accident to mean yes.
  bar.press('ok')
  await sleep(200)
  assert.equal(line(1), 'allow Bash?')

  bar.press('start')
  await until('the verdict', () => line(1) === 'allowed' && screen().m.section === 'allow')
  const { code, stdout } = await run
  assert.equal(code, 0)
  assert.deepEqual(JSON.parse(stdout), { hookSpecificOutput: { hookEventName: 'PermissionRequest', decision: { behavior: 'allow' } } })

  await backToNormal()
  assert.equal(bar.state().quits, 1, 'nothing was up before, so the app is simply quit')
  assert.deepEqual(bar.state().errors, [])
  assert.equal(hostLog().at(-1).outcome, 'allow')
})

test('BACK denies', async () => {
  await install()
  const run = hook(permission('s1', 'mcp__Claude_Browser__computer'))
  await showing('computer?')
  bar.press('back')
  await until('the verdict', () => line(1) === 'denied' && screen().m.section === 'deny')
  const { stdout } = await run
  const decision = JSON.parse(stdout).hookSpecificOutput.decision
  assert.equal(decision.behavior, 'deny')
  assert.match(decision.message, /BUSY Bar/)
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
})

test('the bar goes back to the app that was open, not to a menu', async () => {
  await install()
  await installClock()
  await openFromAppsMenu(CLOCK)
  await until('the clock', () => (bar.state().screen[CLOCK] || {}).time)

  const run = hook(permission('s1'))
  await showing('allow Bash?')
  // A second event while ours is up must not mistake our own app for what was there before.
  await hook({ hook_event_name: 'Notification', session_id: 's2', notification_type: 'idle_prompt', message: 'Claude is waiting for your input' })
  await showing('your turn')
  bar.press('start')
  await showing('allow Bash?')
  bar.press('start')
  await run

  await until('the clock to be back', () => bar.state().launcher === CLOCK && (bar.state().screen[CLOCK] || {}).time)
  assert.equal(bar.state().quits, 0, 'the clock is started in our place; quitting would land in the Apps menu')
  assert.equal(JSON.parse(bar.state().files['/ext/apps_data/apps_menu/settings.json']).values.active_application, CLOCK, "the menu's memory of it is untouched")
  assert.equal(bar.state().launches.at(-1), CLOCK)
  assert.deepEqual(bar.state().errors, [])
})

test('opened by hand, an event interrupts and then the app is back on its own screen', async () => {
  await install()
  await openFromAppsMenu(APP)
  await until('the first-run screen', () => line(1) === 'no events yet')
  await hook({ hook_event_name: 'Stop', session_id: 's1' })
  await showing('finished')
  bar.press('start')
  await until('the idle screen again', () => line(1) === 'all quiet' && bar.state().running, 6000)
  assert.equal(bar.state().quits, 0)
  assert.deepEqual(bar.state().errors, [])
})

test('a finished turn returns at once, shows, and is taken down when the user types', async () => {
  await install()
  const started = Date.now()
  const { code, stdout } = await hook({ hook_event_name: 'Stop', session_id: 's1' })
  assert.equal(code, 0)
  assert.equal(stdout, '', 'a hook prints nothing')
  assert.ok(Date.now() - started < 1500, 'Claude Code is not kept waiting')

  const up = await showing('finished')
  assert.equal(up.m.section, 'done')
  assert.equal(up.t0.text, 'api-server')
  assert.equal(up.f, undefined, 'nothing to answer, no fuse')
  assert.deepEqual(await sounds(1), ['sounds/done.snd'])

  // PreToolUse and friends never reach the bar.
  const before = bar.state().requests
  await hook({ hook_event_name: 'PreToolUse', session_id: 's1', tool_name: 'Bash' })
  await sleep(300)
  assert.equal(bar.state().requests, before)

  // Another session typing does not take this one's notification down...
  await hook({ hook_event_name: 'UserPromptSubmit', session_id: 'other' })
  await sleep(400)
  assert.equal(line(1), 'finished')
  // ...its own does.
  await hook({ hook_event_name: 'UserPromptSubmit', session_id: 's1' })
  await backToNormal(3000)
  assert.deepEqual(bar.state().errors, [])

  // And with nothing up, a prompt being typed costs the bar no request at all.
  const quiet = bar.state().requests
  await hook({ hook_event_name: 'UserPromptSubmit', session_id: 's1' })
  await sleep(400)
  assert.equal(bar.state().requests, quiet)
})

test('usage follows a completion, as numbers and two bars', async () => {
  await install()
  const cache = join(home, '.cache', 'busyclaude')
  mkdirSync(cache, { recursive: true })
  writeFileSync(join(cache, 'usage.json'), JSON.stringify({ five: 42.4, week: 91, at: Date.now() / 1000 }))
  await hook({ hook_event_name: 'Stop', session_id: 's1' }, { env: { BUSYCLAUDE_USAGE: '1' } })
  await showing('finished')
  const numbers = await until('the usage numbers', () => line(0) === '5h 42%' && screen(), 6000)
  assert.equal(numbers.t1.text, '7d 91%')
  assert.equal(numbers.v0.fill_colors[0], '#00FF66FF', 'under half is green')
  assert.equal(numbers.v1.fill_colors[0], '#FF2244FF', 'past 85% is red')
  assert.equal(numbers.v0.width, 7)
  assert.equal(numbers.m.section, 'done', 'the mascot stays')
  bar.press('back')
  await until('the app to close', () => !bar.state().running)
  assert.deepEqual(bar.state().errors, [])
})

test('a second prompt takes the screen, and the first comes back with its fuse still burning', async () => {
  await install()
  const first = hook(permission('s1', 'Edit'))
  await showing('allow Edit?')
  const second = hook(permission('s2', 'Bash'), { env: { CLAUDE_PROJECT_DIR: join(home, 'missing') } })
  const up = await showing('allow Bash?')
  assert.match(up.t0.text, / \+1$/, 'line one says another is waiting')
  assert.deepEqual(await sounds(2), ['sounds/perm.snd', 'sounds/perm.snd'])

  // Claude Code also announces a prompt as a Notification; that one is not shown twice.
  await hook({ hook_event_name: 'Notification', session_id: 's2', notification_type: 'permission_prompt', message: 'Claude needs your permission to use Bash' })
  // And a subagent finishing does not take a prompt off the screen.
  await hook({ hook_event_name: 'SubagentStop', session_id: 's3' })
  await sleep(500)
  assert.equal(line(1), 'allow Bash?')
  assert.equal(bar.state().sounds.length, 2)

  bar.press('start')
  assert.equal(JSON.parse((await second).stdout).hookSpecificOutput.decision.behavior, 'allow')
  const back = await showing('allow Edit?')
  assert.equal(back.t0.text, 'api-server')
  assert.equal(bar.state().sounds.length, 2, 'a prompt that comes back does not chime again')
  bar.press('back')
  assert.equal(JSON.parse((await first).stdout).hookSpecificOutput.decision.behavior, 'deny')
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
  const outcomes = hostLog().map((l) => l.outcome)
  assert.ok(outcomes.includes('duplicate of the prompt already up'), outcomes.join(' | '))
  assert.ok(outcomes.includes('held back: a prompt is up'), outcomes.join(' | '))
})

test('a question is answered by number: the wheel picks, START takes it', async () => {
  await install()
  await setUp({ wait: '15' })
  const run = hook(question('s1', [FRAMEWORK]))
  const up = await showing('option 1/3')
  assert.equal(up.m.section, 'question')
  assert.equal(up.f.path, 'animations/fuse30.anim', 'with the answer time at 15 s, a question still gets 30 to be read in')
  assert.match(up.f.section, /^t(2[89]|30)$/)

  bar.press('down')
  await showing('option 3/3')
  // Claude Code announces the same question again as a Notification a few seconds in. That
  // must not take the question off the screen, or put the wheel back on option 1.
  await hook({ hook_event_name: 'Notification', session_id: 's1', notification_type: 'permission_prompt', message: 'Claude needs your permission to use AskUserQuestion' })
  await sleep(500)
  assert.equal(line(1), 'option 3/3')
  bar.press('up')
  bar.press('up')
  await showing('option 2/3')
  assert.equal(bar.state().launches.length, 1, 'neither that nor turning the wheel restarts anything')

  bar.press('start')
  await until('the verdict', () => line(1) === 'option 2' && screen().m.section === 'allow')
  const { stdout } = await run
  assert.deepEqual(JSON.parse(stdout).hookSpecificOutput.decision, {
    behavior: 'allow',
    updatedInput: { questions: [FRAMEWORK], answers: { 'Which framework?': 'Vue' } },
  })
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
  assert.equal(hostLog().at(-1).outcome, 'option 2')
})

test('BACK leaves a question to the terminal, and the hook stops waiting', async () => {
  await install()
  const started = Date.now()
  const run = hook(question('s1', [FRAMEWORK]))
  await showing('option 1/3')
  bar.press('back')
  const { code, stdout } = await run
  assert.equal(code, 0)
  assert.equal(stdout, '', 'no decision: the terminal still asks')
  assert.ok(Date.now() - started < 5000)
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
})

test('a question with several parts is shown and left to the terminal', async () => {
  await install()
  // `ask` and `window` are how setup's own Test asks for a button; on a real event they mean nothing.
  const { stdout } = await hook({ ...question('s1', [FRAMEWORK, { ...FRAMEWORK, question: 'And which bundler?' }]), ask: true, window: 600 })
  assert.equal(stdout, '')
  const up = await showing('a question')
  assert.equal(up.m.section, 'question')
  assert.equal(up.f, undefined)
  bar.press('start')
  await backToNormal()
  assert.equal(mailbox().answers, undefined, 'the button only waved it away')
  assert.deepEqual(bar.state().errors, [])
})

test('during a BUSY session nothing is shown and the hook does not wait', async () => {
  await install()
  bar.busy = true
  const started = Date.now()
  const { code, stdout } = await hook(permission('s1'))
  assert.equal(code, 0)
  assert.equal(stdout, '', 'the terminal asks instead')
  assert.ok(Date.now() - started < 3000)
  assert.equal(bar.state().launcher, null)
  assert.match(hostLog().at(-1).outcome, /written, not shown/)

  // Opened by hand afterwards, the app does not replay it as news.
  bar.busy = false
  await fetch(`http://127.0.0.1:${port}/api/apps/launch?app_id=${APP}`, { method: 'POST' })
  await until('the idle screen', () => line(1) === 'all quiet')
  assert.equal(screen().m.section, 'idle')
  assert.deepEqual(bar.state().sounds, [])
})

test('opened by hand it stays: usage first, the wheel walks the history, BACK closes', async () => {
  await install()
  await fetch(`http://127.0.0.1:${port}/api/apps/launch?app_id=${APP}`, { method: 'POST' })
  await until('the first-run screen', () => line(1) === 'no events yet')

  await hook({ hook_event_name: 'StopFailure', session_id: 's1', error: 'rate_limit' })
  const up = await showing('rate limit')
  assert.equal(up.m.section, 'error')
  await hook({ hook_event_name: 'UserPromptSubmit', session_id: 's1' })
  await backToNormal(3000)

  await fetch(`http://127.0.0.1:${port}/api/apps/launch?app_id=${APP}`, { method: 'POST' })
  await until('the idle screen', () => line(1) === 'all quiet')
  await sleep(1200)
  assert.equal(bar.state().running, true, 'it does not close itself when you opened it')
  bar.press('up')
  await showing('rate limit')
  bar.press('up')
  await until('the wheel to come round', () => line(1) === 'all quiet')
  bar.press('back')
  await until('the wipe', () => screen().w)
  await until('the app to close', () => !bar.state().running)
  assert.deepEqual(bar.state().errors, [])
})

test('demo walks through every kind without a sound, and any button skips ahead', async () => {
  await install()
  const run = host(['demo'])
  const seen = []
  for (const text of ['$ npm test', 'a question', 'your turn', 'finished', 'agent done', 'rate limit', 'ready']) {
    const up = await showing(text)
    seen.push(up.m.section)
    assert.equal(up.f, undefined, 'nothing in a demo waits for an answer')
    bar.press('start')
  }
  assert.deepEqual(seen, ['perm', 'question', 'ask', 'done', 'agent', 'error', 'hello'])
  const { code, stdout } = await run
  assert.equal(code, 0)
  assert.match(stdout, /returned to the bar's own screen/)
  assert.deepEqual(bar.state().sounds, [])
  assert.deepEqual(bar.state().leds, [])
  assert.deepEqual(bar.state().errors, [])
})

test('usage handed over by hand is what a finished turn shows', async () => {
  await install()
  const { stdout } = await host(['usage', '37', '5'], { env: { BUSYCLAUDE_USAGE: '1' } })
  assert.match(stdout, /5h 37%, 7d 5%/)
  await hook({ hook_event_name: 'Stop', session_id: 's1' }, { env: { BUSYCLAUDE_USAGE: '1' } })
  await showing('finished')
  const numbers = await until('the usage numbers', () => line(0) === '5h 37%' && screen(), 6000)
  assert.equal(numbers.t1.text, '7d 5%')
  bar.press('back')
  await backToNormal()
})

/** What the bar's Setup screen for the app would save. */
function setUp(values) {
  return fetch(`http://127.0.0.1:${port}/api/apps/settings?app_id=${APP}`, {
    method: 'PUT',
    body: JSON.stringify({ version: 3, values: { sound: true, led: true, usage: true, wait: '60', ...values } }),
  })
}

test('"Answer time" on the Setup screen is how long a prompt waits, with a fuse to match', async () => {
  await install()
  await setUp({ wait: '60', sound: false })
  const run = hook(permission('s1'))
  const up = await showing('allow Bash?')
  assert.equal(up.f.path, 'animations/fuse60.anim')
  assert.match(up.f.section, /^t(5[89]|60)$/)
  assert.equal(mailbox().inbox.includes('"window":60'), true)
  assert.deepEqual(bar.state().sounds, [], 'and Sound off is silent')
  bar.press('start')
  assert.equal(JSON.parse((await run).stdout).hookSpecificOutput.decision.behavior, 'allow')
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
})

test('with "Show usage" off a finished turn keeps its text', async () => {
  await install()
  await setUp({ usage: false })
  await host(['usage', '37', '5'], { env: { BUSYCLAUDE_USAGE: '1' } })
  await hook({ hook_event_name: 'Stop', session_id: 's1' }, { env: { BUSYCLAUDE_USAGE: '1' } })
  await showing('finished')
  await sleep(4500)
  assert.equal(line(0), 'api-server', 'no numbers took its place')
  assert.equal(line(1), 'finished')
  assert.equal(screen().u0, undefined)
  bar.press('back')
  await backToNormal()
})

test('opened by hand hours later, the app does not show usage numbers that have gone stale', async () => {
  await install()
  const leave = (at) => {
    const doc = { v: 1, n: 'n1', fresh: '', items: [], history: [], usage: { five: 41, week: 6, at } }
    bar.files.set(`/ext/apps_data/jsrunner/${APP}.localstorage.json`, Buffer.from(JSON.stringify({ format_version: 1, data: { inbox: JSON.stringify(doc), seen: '{"n":"n1"}' } })))
    return fetch(`http://127.0.0.1:${port}/api/apps/launch?app_id=${APP}`, { method: 'POST' })
  }
  await leave(Date.now() / 1000 - 60)
  await until('recent numbers', () => line(0) === '5h 41%')
  await leave(Date.now() / 1000 - 3600)
  await until('no numbers', () => line(1) === 'all quiet')
  assert.equal(screen().u0, undefined)
  assert.deepEqual(bar.state().errors, [])
})

test('a prompt says what is about to run, not just which tool wants to run it', async () => {
  await install()
  const ask = (tool, input) => ({ ...permission('s1', tool), tool_input: input, cwd: project })
  const answered = async (payload, expected) => {
    const run = hook(payload)
    const up = await showing(expected)
    bar.press('back')
    await run
    await backToNormal()
    return up
  }

  const bash = await answered(ask('Bash', { command: 'rm -rf node_modules', description: 'Remove node_modules directory' }), '$ rm -rf node_modules')
  assert.ok(bash.t1.scroll_rate > bash.t0.scroll_rate, 'the command scrolls faster than the project name')

  // The file, from the root of the project; outside it, from home.
  await answered(ask('Edit', { file_path: join(project, 'src', 'client.ts'), old_string: 'a', new_string: 'b' }), 'Edit src/client.ts')
  await answered(ask('Write', { file_path: join(home, '.zshrc'), content: 'x' }), 'Write ~/.zshrc')
  await answered(ask('WebFetch', { url: 'https://example.com/install.sh', prompt: 'read it' }), 'Fetch example.com/install.sh')

  // Several lines become one, nothing is silently dropped, and a long one is cut where it says so.
  await answered(ask('Bash', { command: 'echo "\u043f\u0440\u0438\u0432\u0435\u0442" \\\n   && ls\t-la' }), '$ echo "??????" \\ && ls -la')
  const run = hook(ask('Bash', { command: `curl -fsSL https://example.com/install.sh | bash -s -- ${'--flag '.repeat(20)}` }))
  const long = await until('a long command', () => (line(1) || '').startsWith('$ curl -fsSL https://example.com/install.sh | bash') && screen())
  assert.equal(long.t1.text.length, 100, 'cut to what can be read once through in the shortest answer time')
  assert.ok(long.t1.text.endsWith('...'), 'and it says that it was cut')
  bar.press('back')
  await run
  await backToNormal()

  // A tool whose input says nothing readable still asks by name.
  await answered(ask('mcp__github__create_issue', { title: 'x' }), 'create issue?'.replace(' ', '_'))
  assert.deepEqual(bar.state().errors, [])
})

test('setup updates the app even while it is on the screen', async () => {
  await install()
  await hook({ hook_event_name: 'Stop', session_id: 's1' })
  await showing('finished')
  // The bar cannot replace an app whose animation is playing: left alone, this install fails
  // halfway and the bar ends up with no app at all.
  // --no-confirm: this test is about the install, not about anyone reaching for a button.
  const { code, stdout, stderr } = await host(['setup', '--package', PACKAGE, '--no-hooks', '--no-confirm'])
  assert.equal(code, 0, stderr)
  assert.match(stdout, /installed Claude Code v\d/)
  assert.match(stdout, /hello: shown/)
  const apps = await fetch(`http://127.0.0.1:${port}/api/apps/list`).then((r) => r.json())
  assert.deepEqual(apps.apps.map((a) => a.id), [APP])
  assert.deepEqual(bar.state().errors, [])
})

test('as a plugin: its hooks do the work, and hooks an earlier setup left in settings.json stand aside', async () => {
  await install()
  const settings = join(home, '.claude', 'settings.json')
  mkdirSync(dirname(settings))
  writeFileSync(settings, JSON.stringify({ enabledPlugins: { 'busyclaude@busyclaude': true } }))

  // What the hook in settings.json runs: nothing reaches the bar, or every event would show twice.
  const before = bar.state().requests
  const stale = await host(['hook'], { stdin: permission('s1') })
  await sleep(400)
  assert.deepEqual([stale.code, stale.stdout], [0, ''])
  assert.equal(bar.state().requests, before)

  // What the plugin's own hook runs.
  const run = host(['hook', '--plugin'], { stdin: permission('s1') })
  await showing('allow Bash?')
  bar.press('start')
  assert.equal(JSON.parse((await run).stdout).hookSpecificOutput.decision.behavior, 'allow')
  await backToNormal()
  assert.deepEqual(bar.state().errors, [])
})

test('as a plugin: setup installs the package it ships, with no build tools, and clears old hooks', async () => {
  // A plugin's copy of this repository: the files git tracks, and no node_modules.
  const copy = mkdtempSync(join(tmpdir(), 'busyclaude-plugin-'))
  for (const name of ['busyclaude.py', 'release', 'hooks', 'skills', '.claude-plugin']) cpSync(join(ROOT, name), join(copy, name), { recursive: true })
  cpSync(join(ROOT, 'src', 'appmeta'), join(copy, 'src', 'appmeta'), { recursive: true })

  const settings = join(home, '.claude', 'settings.json')
  mkdirSync(dirname(settings))
  const foreign = { type: 'command', command: 'echo not ours' }
  const ours = { type: 'command', command: `python3 ${HOST} hook`, timeout: 10 }
  writeFileSync(settings, JSON.stringify({ hooks: { Stop: [{ hooks: [foreign, ours] }], Notification: [{ hooks: [ours] }] } }))

  const { code, stdout, stderr } = await host(['setup', '--plugin', '--no-confirm', '--settings', settings], { script: join(copy, 'busyclaude.py') })
  rmSync(copy, { recursive: true, force: true })
  assert.equal(code, 0, stderr)
  assert.match(stdout, /installed Claude Code v\d/)
  assert.match(stdout, /hooks come with the plugin; the ones an earlier setup left/)
  assert.match(stdout, /hello: shown/)
  assert.deepEqual(JSON.parse(readFileSync(settings, 'utf8')).hooks, { Stop: [{ hooks: [foreign] }] })
  assert.deepEqual(bar.state().errors, [])
})

