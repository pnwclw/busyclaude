// A stand-in BUSY Bar: enough of the HTTP API, and of the JavaScript runtime
// behind it, to run the built app and the hooks against each other with no
// hardware on the desk.
//
//   node tools/fakebar.mjs [--port 8080] [--busy]
//   BUSYBAR_ADDR=127.0.0.1:8080 python3 busyclaude.py setup --no-hooks
//
// It is deliberately strict where the firmware is: an app may only reach the
// endpoints firmware 1.3.1 allows it (no storage), a draw is checked against
// the OpenAPI schema, an animation's file and section must exist, localStorage
// is read once per launch and saved to the same file the firmware uses, and a
// short BACK wipes the display before the app hears of it. What it cannot tell you is how any of this looks or sounds.
//
// Extra, for tests and for poking at it by hand:
//   GET  /_fake/state         what is on screen, what is running, what went wrong
//   POST /api/input?key=ok    press a button: ok, back, start, up, down

import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import vm from 'node:vm'

const API_VERSION = '27.14.0'
const FIRMWARE = '1.3.1'

/** What an app may call on firmware 1.3.1, from the firmware's own documentation. */
const APP_ALLOWED = [
  ['apps/settings', ['GET']],
  ['display/brightness', ['GET', 'POST']],
  ['display/draw', ['POST', 'DELETE']],
  ['audio/play', ['POST', 'DELETE']],
  ['audio/volume', ['GET', 'POST']],
  ['busy', ['GET', 'PUT']],
  ['name', ['GET']],
  ['time', ['GET']],
  ['wifi/status', ['GET']],
  ['version', ['GET']],
  ['access', ['GET']],
  ['transport', ['GET']],
  ['status', ['GET']],
]

const ID = /^[a-zA-Z0-9._-]+$/
const COLOR = /^#[a-fA-F0-9]{8}$/
const ASCII = /^[\x20-\x7E]+$/
const STORAGE_PATH = /^\/ext(\/[a-zA-Z0-9._-]*)*$/
const FONTS = ['tiny', 'small', 'normal', 'condensed', 'bold', 'large', 'extra_large', 'global', 'superscript']

/** The section names of a .anim file (lib/anim_file/anim_file_format.h). */
function animSections(blob) {
  if (blob.subarray(0, 8).toString() !== 'bicycle1') throw new Error('not a .anim file')
  const count = blob.readUInt32LE(26)
  const names = []
  let at = 34
  for (let i = 0; i < count; i++) {
    const end = blob.indexOf(0, at + 12)
    names.push(blob.subarray(at + 12, end).toString())
    at = end + 1
  }
  return names
}

export class FakeBar {
  constructor({ busy = false } = {}) {
    /** True refuses to launch apps, as the bar does during a BUSY session. */
    this.busy = busy
    /** Storage: path -> Buffer. */
    this.files = new Map()
    /** Installed apps: id -> { dir, manifest, settings }. */
    this.apps = new Map()
    this.staged = new Map()
    /** The app launcher: null when the bar is showing its own screens. */
    this.launcher = null
    /** Elements on the display: application_name -> Map(id -> element). */
    this.display = new Map()
    this.sounds = []
    this.leds = []
    /** Every app launch, in order, and how many times an app was quit through the API. */
    this.launches = []
    this.quits = 0
    /** Everything that should not have happened: refused requests, app exceptions. */
    this.errors = []
    this.log = []
    this.requests = 0
    this.tmp = mkdtempSync(join(tmpdir(), 'fakebar-'))
  }

  close() {
    this.stop()
    rmSync(this.tmp, { recursive: true, force: true })
  }

  // --- the JavaScript runtime ------------------------------------------------
  launch(id) {
    const app = this.apps.get(id)
    this.stop()
    const run = { id, timers: new Set(), fetches: 0, handler: null, finished: false, store: null, dead: false }
    this.launcher = { id, run }
    this.launches.push(id)

    const settle = () => {
      if (run.dead || run.finished) return
      if (run.timers.size === 0 && run.fetches === 0 && run.handler === null) {
        // Nothing keeps it alive: the script is over and the launcher shows the app's Start menu.
        run.finished = true
        this.display.delete(id)
        this.log.push(`${id} finished`)
      }
    }
    const guard = (fn) => (...args) => {
      if (run.dead) return
      try {
        fn(...args)
      } catch (err) {
        this.errors.push(`app threw: ${err && err.stack ? err.stack : err}`)
      }
      settle()
    }
    const timer = (set, clear) => (fn, ms, ...args) => {
      const handle = set(
        guard(() => {
          if (set === setTimeout) run.timers.delete(handle)
          fn(...args)
        }),
        ms,
      )
      run.timers.add(handle)
      return handle
    }
    const untimer = (clear) => (handle) => {
      clear(handle)
      run.timers.delete(handle)
      setImmediate(settle)
    }

    const storePath = `/ext/apps_data/jsrunner/${id}.localstorage.json`
    const store = () => {
      // Read once per launch, as the firmware does; a later write from outside is not seen.
      if (run.store) return run.store
      run.store = {}
      try {
        const doc = JSON.parse(this.files.get(storePath).toString())
        if (doc.format_version === 1) {
          for (const [key, value] of Object.entries(doc.data)) if (typeof value === 'string') run.store[key] = value
        }
      } catch {}
      return run.store
    }
    const save = () => this.files.set(storePath, Buffer.from(JSON.stringify({ format_version: 1, data: store() })))

    const sandbox = {
      console: {
        log: (...a) => this.log.push(`app: ${a.join(' ')}`),
        info: (...a) => this.log.push(`app: ${a.join(' ')}`),
        warn: (...a) => this.log.push(`app warn: ${a.join(' ')}`),
        error: (...a) => this.errors.push(`app error: ${a.join(' ')}`),
      },
      setTimeout: timer(setTimeout, clearTimeout),
      clearTimeout: untimer(clearTimeout),
      setInterval: timer(setInterval, clearInterval),
      clearInterval: untimer(clearInterval),
      localStorage: {
        getItem: (key) => (key in store() ? store()[key] : null),
        setItem: (key, value) => {
          store()[key] = String(value)
          save()
        },
        removeItem: (key) => {
          delete store()[key]
          save()
        },
      },
      listen: (type, handler) => {
        if (type !== 'input') throw new TypeError(`listen: unknown type ${type}`)
        if (run.handler) throw new TypeError('listen: a handler is already attached')
        run.handler = guard(handler)
        return () => {
          run.handler = null
          setImmediate(settle)
        }
      },
      fetch: (url, init = {}) => {
        run.fetches++
        return new Promise((resolve, reject) => {
          setTimeout(() => {
            let result
            if (!run.dead) {
              const target = new URL(url)
              if (target.hostname !== 'localhost') {
                result = new Error(`fetch: ${target.hostname} is not this bar`)
              } else {
                const reply = this.handle(init.method || 'GET', target, init.body ? Buffer.from(String(init.body)) : Buffer.alloc(0), id)
                const text = typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body)
                result = { ok: reply.status >= 200 && reply.status < 300, status: reply.status, text: async () => text, json: async () => JSON.parse(text) }
              }
            }
            run.fetches--
            if (run.dead) return
            if (result instanceof Error) reject(result)
            else resolve(result)
            // Let the promise's own callbacks run before deciding nothing is left.
            setTimeout(settle, 5)
          }, 15)
        })
      },
    }

    const source = readFileSync(join(app.dir, 'scripts', 'main.js'), 'utf8')
    this.log.push(`${id} launched`)
    try {
      vm.runInNewContext(source, sandbox, { filename: 'scripts/main.js' })
    } catch (err) {
      this.errors.push(`app threw at startup: ${err && err.stack ? err.stack : err}`)
    }
    setImmediate(settle)
  }

  /** Ends the running app and the launcher around it. */
  stop() {
    if (!this.launcher) return false
    const { run, id } = this.launcher
    run.dead = true
    for (const handle of run.timers) {
      clearTimeout(handle)
      clearInterval(handle)
    }
    this.display.delete(id)
    this.launcher = null
    return true
  }

  press(key) {
    const run = this.launcher && this.launcher.run
    if (!run || run.finished || !run.handler) return false
    if (key === 'up' || key === 'down') {
      const delta = key === 'up' ? 1 : -1
      run.handler({ key: 'encoder', action: delta > 0 ? 'clockwise' : 'counterclockwise', delta })
    } else {
      for (const action of ['press', 'release', 'short']) {
        // Firmware 1.3: a short BACK wipes everything drawn through the API (canvas.c,
        // CanvasSrvEventExit), and the app hears about the button after that.
        if (key === 'back' && action === 'short') this.display.delete(run.id)
        if (run.handler) run.handler({ key, action })
      }
    }
    return true
  }

  // --- the HTTP API ------------------------------------------------------------
  /** @param fromApp - The id of the app asking, when the request comes from the runtime. */
  handle(method, url, body, fromApp) {
    this.requests++
    const path = url.pathname.replace(/^\/api\//, '')
    const query = url.searchParams
    const fail = (status, error) => {
      this.errors.push(`${method} ${url.pathname}${url.search} -> ${status} ${error}`)
      return { status, body: { error } }
    }
    const ok = (extra = {}) => ({ status: 200, body: { result: 'OK', ...extra } })
    const json = () => JSON.parse(body.toString() || '{}')

    if (fromApp) {
      const rule = APP_ALLOWED.find(([prefix]) => path === prefix || path.startsWith(`${prefix}/`))
      if (!rule || !rule[1].includes(method)) return fail(403, 'not available to applications')
    }

    switch (`${method} ${path}`) {
      case 'GET version':
        return { status: 200, body: { api_semver: API_VERSION } }
      case 'GET status/firmware':
        return { status: 200, body: { version: FIRMWARE } }

      case 'POST storage/write':
      case 'GET storage/read':
      case 'POST storage/mkdir':
      case 'DELETE storage/remove': {
        const file = query.get('path') || ''
        if (!STORAGE_PATH.test(file)) return fail(400, 'Bad Request')
        // An app's folder cannot go while one of its files is on screen.
        if (path === 'storage/remove' && file.startsWith('/ext/user_assets/')) {
          const drawn = this.display.get(file.split('/')[3])
          if (drawn && drawn.size > 0) return { status: 400, body: { error: 'Bad Request' } }
          return ok()
        }
        if (method === 'GET') {
          // A missing file is how a hook learns the mailbox is new: not an error.
          return this.files.has(file) ? { status: 200, body: this.files.get(file).toString() } : { status: 400, body: { error: 'Bad Request' } }
        }
        if (path === 'storage/write') this.files.set(file, body)
        if (path === 'storage/remove') this.files.delete(file)
        return ok()
      }

      case 'POST apps/stage': {
        const dir = mkdtempSync(join(this.tmp, 'pkg-'))
        const archive = join(dir, 'app.tgz')
        try {
          execFileSync('tar', ['-xzf', '-', '-C', dir], { input: body })
          const [root] = execFileSync('ls', [dir]).toString().trim().split('\n')
          const manifest = JSON.parse(readFileSync(join(dir, root, 'appmeta', 'manifest.json'), 'utf8'))
          if (!existsSync(join(dir, root, 'scripts', 'main.js'))) throw new Error('no scripts/main.js')
          const key = this.staged.size + 1000
          this.staged.set(key, { dir: join(dir, root), manifest })
          const info = { id: manifest.id, name: manifest.name, version: manifest.version }
          void archive
          return ok({ install_key: key, staged: info })
        } catch (err) {
          return fail(400, `unpack_error: ${err.message}`)
        }
      }
      case 'POST apps/install': {
        const staged = this.staged.get(Number(query.get('install_key')))
        if (!staged) return fail(400, 'Bad Request')
        let settings = null
        const schema = join(staged.dir, 'appmeta', 'settings.json')
        if (existsSync(schema)) {
          const { version, fields } = JSON.parse(readFileSync(schema, 'utf8'))
          const kept = this.apps.get(staged.manifest.id)
          settings = kept && kept.settings ? kept.settings : { version, values: Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.default])) }
        }
        // An animation on screen is a file held open, and the firmware cannot delete an app with a
        // file open: the install fails and what it had managed to delete stays deleted.
        const drawn = this.display.get(staged.manifest.id)
        if (drawn && drawn.size > 0) {
          if (this.launcher && this.launcher.id === staged.manifest.id) this.stop()
          this.apps.delete(staged.manifest.id)
          this.display.set(staged.manifest.id, drawn)
          return { status: 400, body: { error: 'Bad Request' } }
        }
        if (this.launcher && this.launcher.id === staged.manifest.id) this.stop()
        this.apps.set(staged.manifest.id, { ...staged, settings })
        return ok()
      }
      case 'GET apps/list':
        return { status: 200, body: { apps: [...this.apps.values()].map(({ manifest: m }) => ({ id: m.id, name: m.name, version: m.version, api_version: m.api_version })) } }
      case 'DELETE apps': {
        const id = query.get('app_id')
        if (!this.apps.has(id)) return fail(404, 'Not Found')
        if (this.launcher && this.launcher.id === id) this.stop()
        this.apps.delete(id)
        return ok()
      }
      case 'POST apps/launch': {
        const id = query.get('app_id')
        if (!this.apps.has(id)) return fail(404, 'Not Found')
        // During a BUSY session the bar turns an app away; a hook is expected to cope, so this is not logged as an error.
        if (this.busy) return { status: 409, body: { error: 'not started due to low priority' } }
        this.launch(id)
        return ok()
      }
      case 'POST apps/quit':
        if (!this.stop()) return { status: 409, body: { error: 'application is not running' } }
        this.quits++
        return ok()
      case 'GET apps/settings':
      case 'PUT apps/settings': {
        const app = this.apps.get(query.get('app_id'))
        if (!app || !app.settings) return fail(404, 'Not Found')
        if (fromApp && fromApp !== query.get('app_id')) return fail(403, 'Forbidden')
        if (method === 'PUT') app.settings = json()
        return method === 'GET' ? { status: 200, body: app.settings } : ok()
      }

      case 'POST display/draw': {
        const request = json()
        const problem = this.checkDraw(request)
        if (problem) return fail(400, problem)
        const name = request.application_name
        // One name owns the display at a time: a draw under another takes it over.
        for (const other of [...this.display.keys()]) if (other !== name) this.display.delete(other)
        if (!this.display.has(name)) this.display.set(name, new Map())
        for (const element of request.elements) this.display.get(name).set(element.id, element)
        if (request.led_notification_color) this.leds.push(request.led_notification_color)
        return ok()
      }
      case 'DELETE display/draw': {
        const request = json()
        const name = request.application_name || query.get('application_name')
        const elements = this.display.get(name)
        if (request.element_ids) {
          // Someone else has the display: refused, and in different words from a missing
          // element. That difference is how a hook finds out what is on screen.
          const owner = [...this.display].find(([, drawn]) => drawn.size > 0)
          if (owner && owner[0] !== name) {
            return { status: 400, body: { error: 'this application_name is currently not displaying anything' } }
          }
          // All or nothing, as canvas_element_destroy_multi does it. Not logged as an error: after
          // a BACK this is what an app that clears its own leftovers gets, and it has to cope.
          if (!elements || request.element_ids.some((id) => !elements.has(id))) {
            return { status: 400, body: { error: 'one of element_ids is non-existent' } }
          }
          for (const id of request.element_ids) elements.delete(id)
        } else if (elements) {
          elements.clear()
        }
        return ok()
      }
      case 'POST audio/play': {
        const request = json()
        const app = this.apps.get(request.application_name)
        if (!app || !request.path || !existsSync(join(app.dir, request.path))) return fail(404, 'Audio file not found')
        const size = readFileSync(join(app.dir, request.path)).length
        if (size === 0 || size % 2) return fail(404, 'not 16-bit PCM')
        this.sounds.push(request.path)
        return ok()
      }
      case 'POST input':
        return this.press(query.get('key')) ? ok() : { status: 200, body: { result: 'OK', note: 'no app took it' } }

      case 'GET /_fake/state':
        this.requests--
        return { status: 200, body: this.state() }
      default:
        return fail(404, 'Not Found')
    }
  }

  /** The draw schema from the firmware's OpenAPI, for the element types the app uses. */
  checkDraw(request) {
    if (typeof request.application_name !== 'string' || !ID.test(request.application_name)) return 'application_name'
    if (request.priority !== undefined && !(Number.isInteger(request.priority) && request.priority >= 1 && request.priority <= 100)) return 'priority'
    if (request.led_notification_color !== undefined && !COLOR.test(request.led_notification_color)) return 'led_notification_color'
    if (!Array.isArray(request.elements) || request.elements.length === 0) return 'elements'
    const app = this.apps.get(request.application_name)
    for (const e of request.elements) {
      const where = `element ${e.id}`
      if (typeof e.id !== 'string' || !ID.test(e.id)) return `${where}: id`
      for (const key of ['x', 'y', 'z_index']) if (e[key] !== undefined && !Number.isInteger(e[key])) return `${where}: ${key}`
      if (e.type === 'text') {
        if (typeof e.text !== 'string' || !ASCII.test(e.text)) return `${where}: text must be printable ASCII, got ${JSON.stringify(e.text)}`
        if (!FONTS.includes(e.font)) return `${where}: font`
        if (e.color !== undefined && !COLOR.test(e.color)) return `${where}: color`
        if (e.width !== undefined && !(Number.isInteger(e.width) && e.width >= 1)) return `${where}: width`
      } else if (e.type === 'animation') {
        const file = app && typeof e.path === 'string' ? join(app.dir, e.path) : null
        if (!file || !existsSync(file)) return `${where}: no such animation ${e.path}`
        if (e.section !== undefined && !animSections(readFileSync(file)).includes(e.section)) return `${where}: ${e.path} has no section ${e.section}`
      } else if (e.type === 'rectangle') {
        for (const key of ['width', 'height']) if (!(Number.isInteger(e[key]) && e[key] >= 1)) return `${where}: ${key}`
        for (const color of e.fill_colors || []) if (!COLOR.test(color)) return `${where}: fill_colors`
      } else {
        return `${where}: type ${e.type}`
      }
    }
    return null
  }

  state() {
    const run = this.launcher && this.launcher.run
    const screen = {}
    for (const [name, elements] of this.display) screen[name] = Object.fromEntries(elements)
    return {
      /** null: the bar's own screens. Otherwise the app the launcher is holding. */
      launcher: this.launcher ? this.launcher.id : null,
      /** True while the app's script is alive; false once it has finished and its Start menu is up. */
      running: Boolean(run && !run.finished),
      screen,
      sounds: this.sounds,
      leds: this.leds,
      launches: this.launches,
      quits: this.quits,
      errors: this.errors,
      log: this.log,
      requests: this.requests,
      files: Object.fromEntries([...this.files].map(([k, v]) => [k, v.toString()])),
    }
  }

  listen(port = 0) {
    this.server = createServer((req, res) => {
      const chunks = []
      req.on('data', (chunk) => chunks.push(chunk))
      req.on('end', () => {
        const url = new URL(req.url, 'http://bar')
        const fake = url.pathname === '/_fake/state'
        const reply = fake ? { status: 200, body: this.state() } : this.handle(req.method, url, Buffer.concat(chunks), null)
        const text = typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body)
        res.writeHead(reply.status, { 'Content-Type': 'application/json' })
        res.end(text)
      })
    })
    return new Promise((resolve) => this.server.listen(port, '127.0.0.1', () => resolve(this.server.address().port)))
  }

  shutdown() {
    this.close()
    return new Promise((resolve) => this.server.close(resolve))
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const arg = (name) => {
    const at = process.argv.indexOf(name)
    return at === -1 ? undefined : process.argv[at + 1]
  }
  const bar = new FakeBar({ busy: process.argv.includes('--busy') })
  const port = await bar.listen(Number(arg('--port') || 8080))
  console.log(`fake BUSY Bar (firmware ${FIRMWARE}, API ${API_VERSION}) on 127.0.0.1:${port}`)
  console.log(`  BUSYBAR_ADDR=127.0.0.1:${port} python3 busyclaude.py setup --no-hooks`)
  process.on('SIGINT', () => bar.shutdown().then(() => process.exit(0)))
}
