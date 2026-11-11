// The same few names and numbers live on both sides of the USB cable. These
// tests are what keeps them from drifting apart.

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const read = (path) => readFileSync(join(ROOT, path), 'utf8')
const PYTHON = process.env.PYTHON || 'python3'
const py = (code) => JSON.parse(execFileSync(PYTHON, ['-c', `import sys, json; sys.path.insert(0, ${JSON.stringify(ROOT)}); sys.path.insert(0, ${JSON.stringify(join(ROOT, 'tools'))})\n${code}`]).toString())

const manifest = JSON.parse(read('src/appmeta/manifest.json'))

/** The .anim sections, read straight from the file the bar will play. */
function sections(path) {
  const blob = readFileSync(join(ROOT, path))
  assert.equal(blob.subarray(0, 8).toString(), 'bicycle1')
  const names = []
  let at = 34
  for (let i = 0; i < blob.readUInt32LE(26); i++) {
    const end = blob.indexOf(0, at + 12)
    names.push(blob.subarray(at + 12, end).toString())
    at = end + 1
  }
  return names
}

test('the hook and the app agree on the app id', () => {
  assert.equal(py('import busyclaude; print(json.dumps(busyclaude.APP_ID))'), manifest.id)
  assert.equal(JSON.parse(read('package.json')).version, manifest.version, 'package.json and the manifest carry one version')
})

test('the generated assets on disk are what tools/assets.py makes', () => {
  execFileSync(PYTHON, [join(ROOT, 'tools/assets.py'), '--check'], { stdio: 'pipe' })
})

test('every kind has a section in the mascot, a tone on disk, and the same seconds on both sides', () => {
  const kinds = read('src/kinds.ts')
  const mascot = sections('src/animations/clawd.anim')
  const host = py('import busyclaude; print(json.dumps(busyclaude.SHOW_SECONDS))')
  const found = [...kinds.matchAll(/^\s+(\w+): \{ color: "#[0-9A-F]{8}", section: "(\w+)",(?: tone: "(\w+)",)? seconds: (\d+) \}/gm)]
  assert.equal(found.length, Object.keys(host).length)
  for (const [, name, section, tone, seconds] of found) {
    assert.ok(mascot.includes(section), `clawd.anim has no section ${section}`)
    assert.equal(host[name], Number(seconds), `${name}: seconds differ between busyclaude.py and kinds.ts`)
    if (tone) assert.ok(readFileSync(join(ROOT, `src/sounds/${tone}.snd`)).length > 0)
  }
  for (const section of ['idle', 'allow', 'deny']) assert.ok(mascot.includes(section))
})

test('there is a fuse for every answer time the Setup screen offers', () => {
  const host = py('import busyclaude as b; print(json.dumps({"choices": list(b.WAIT_CHOICES), "default": b.DEFAULT_WAIT, "question": b.QUESTION_MIN_WAIT}))')
  const field = JSON.parse(read('src/appmeta/settings.json')).fields.wait
  assert.deepEqual(field.options.map((o) => Number(o.value)), host.choices)
  assert.equal(Number(field.default), host.default)
  assert.ok(host.choices.includes(host.question), 'a question waits one of the times there is a fuse for')
  // The app's table and the generator's have to agree on the step as well as the seconds: the app
  // rounds the time left up to a section, and asks for one the file does not have if they differ.
  const made = py('import assets; print(json.dumps({name: list(spec) for name, spec in assets.FUSES.items()}))')
  const app = [...read('src/screens.ts').matchAll(/\{ seconds: (\d+), step: (\d+) \}/g)].map((m) => [Number(m[1]), Number(m[2])])
  assert.deepEqual(app.map(([seconds]) => seconds), host.choices)
  for (const [seconds, step] of app) {
    const spec = made[`fuse${seconds}`]
    assert.ok(spec, `tools/assets.py makes no fuse${seconds}`)
    assert.equal(spec[2], step, `fuse${seconds}: the app and tools/assets.py disagree on the step`)
    const names = sections(`src/animations/fuse${seconds}.anim`)
    for (let n = step; n <= seconds; n += step) assert.ok(names.includes(`t${n}`), `fuse${seconds}.anim has no section t${n}`)
    assert.ok(names.includes(`t${seconds}`), `fuse${seconds}.anim has no section to start full at`)
  }
})

test('a permission hook is allowed to outlast the longest answer time', () => {
  const host = py('import busyclaude as b; print(json.dumps({"timeout": b.hooks_block("x")["PermissionRequest"][0]["hooks"][0]["timeout"], "longest": max(b.WAIT_CHOICES), "grace": b.ANSWER_GRACE, "confirm": b.CONFIRM_WAIT, "choices": list(b.WAIT_CHOICES)}))')
  assert.ok(host.timeout > host.longest + host.grace, 'Claude Code would cut the hook off mid-prompt')
  assert.ok(host.choices.includes(host.confirm), "setup's confirmation waits a time there is a fuse for")
})

test('both halves agree on the inbox document version', () => {
  const host = py('import busyclaude as b; print(json.dumps(b.INBOX_VERSION))')
  assert.equal(Number(read('src/mailbox.ts').match(/const VERSION = (\d+);/)[1]), host)
})

test('a fetch never overrides numbers handed over, and a token that disagrees with them is left alone', () => {
  const out = py(`import os, subprocess, sys, tempfile
home = tempfile.mkdtemp()
script = os.path.join(home, "check.py")
open(script, "w").write(r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import busyclaude as b
token = {"accessToken": "t", "subscriptionType": "team", "rateLimitTier": "x"}
says = {"five": 0.0, "week": 0.0}
asked = []
b.credentials = lambda: dict(token)
b.usage_ask = lambda oauth: asked.append(1) or dict(says)
def age(seconds):
    cached = json.load(open(b.USAGE_CACHE)); cached["at"] = time.time() - seconds
    json.dump(cached, open(b.USAGE_CACHE, "w"))
out = {}
handed = b.usage_store(70, 10, "by hand")
age(300)
out["handed_stands"] = [b.usage_current()["five"], len(asked)]
out["disagrees"] = b.usage_crosscheck(handed) is not None
age(3600)
out["left_alone"] = [b.usage_current(), len(asked), "another subscription" in b.usage_diagnose()]
token["subscriptionType"] = "max"
out["new_login"] = b.usage_current()["five"]
token["subscriptionType"] = "team"; says.update(five=70.0, week=10.0)
out["agrees"] = [b.usage_crosscheck(handed), b.usage_fetch()["five"]]
print(json.dumps(out))
""")
env = dict(os.environ, HOME=home); env.pop("BUSYCLAUDE_FETCH", None); env.pop("BUSYCLAUDE_USAGE", None)
print(subprocess.run([sys.executable, script, sys.path[1]], env=env, capture_output=True, check=True).stdout.decode())`)
  assert.deepEqual(out.handed_stands, [70, 0], 'five minutes old, numbers handed over stand and nothing is fetched')
  assert.equal(out.disagrees, true, '0% from the token against 70% handed over')
  assert.deepEqual(out.left_alone, [null, 1, true], 'once they are stale, nothing is shown rather than the other subscription')
  assert.equal(out.new_login, 0, '`claude` logged in elsewhere: that token is asked again')
  assert.deepEqual(out.agrees, [null, 70], 'a hand-over that agrees trusts the token again')
})

test('both halves agree on how old a usage number may be', () => {
  const host = py('import busyclaude as b; print(json.dumps(b.USAGE_MAX_AGE))')
  const app = read('src/main.ts').match(/USAGE_MAX_AGE_S = (\d+) \* (\d+)/)
  assert.equal(Number(app[1]) * Number(app[2]), host)
})

test('usage numbers that have gone stale are not passed on to the bar', () => {
  const kept = py(`import busyclaude as b, time
now = time.time()
fresh = b.pack({"usage": {"five": 1, "week": 2, "at": now - 60}}, [], [], None, "", now)
stale = b.pack({"usage": {"five": 1, "week": 2, "at": now - 3600}}, [], [], None, "", now)
undated = b.pack({"usage": {"five": 1, "week": 2}}, [], [], None, "", now)
print(json.dumps(["usage" in fresh, "usage" in stale, "usage" in undated]))`)
  assert.deepEqual(kept, [true, false, false])
})

test('the settings the app reads are the ones the manifest describes', () => {
  const fields = JSON.parse(read('src/appmeta/settings.json')).fields
  const source = read('src/settings.ts')
  assert.equal(Number(source.match(/const VERSION = (\d+)/)[1]), JSON.parse(read('src/appmeta/settings.json')).version)
  const defaults = source.match(/DEFAULTS: Settings = \{([^}]+)\}/)[1]
  for (const [name, field] of Object.entries(fields)) {
    // `wait` is the hook's to read: it is the hook that waits.
    if (name === 'wait') continue
    assert.match(defaults, new RegExp(`${name}: ${field.default}`))
  }
})

test('claude/settings.json registers the events the hook handles', () => {
  const sample = JSON.parse(read('claude/settings.json')).hooks
  const host = py('import busyclaude; print(json.dumps(busyclaude.hooks_block("x")))')
  assert.deepEqual(Object.keys(sample).sort(), Object.keys(host).sort())
  for (const [event, groups] of Object.entries(sample)) {
    assert.equal(groups[0].hooks[0].timeout, host[event][0].hooks[0].timeout, event)
    assert.match(groups[0].hooks[0].command, /busyclaude\.py hook$/)
  }
})

test('the status line command notes the numbers it is handed and keeps the old status line', () => {
  const out = py(`import busyclaude as b, os, subprocess, sys, tempfile
home = tempfile.mkdtemp()
env = dict(os.environ, HOME=home)
handed = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 23.5}, "seven_day": {"used_percentage": 41.2}}, "model": {"display_name": "Opus"}})
line = subprocess.run([sys.executable, b.__file__, "statusline", "--then", "python3 -c 'import sys, json; print(json.load(sys.stdin)[\\"model\\"][\\"display_name\\"])'"], input=handed.encode(), env=env, capture_output=True)
none = subprocess.run([sys.executable, b.__file__, "statusline"], input=b"{}", env=env, capture_output=True)
noted = json.load(open(os.path.join(home, ".cache/busyclaude/usage.json")))
theirs = "jq -r '.model.display_name' | tr a-z A-Z"
wrapped = b.merge_statusline({"statusLine": {"type": "command", "command": theirs, "padding": 0}}, True)
print(json.dumps({
  "line": line.stdout.decode().strip(), "noted": [noted["five"], noted["week"], noted["source"]],
  "quiet": [none.returncode, none.stdout.decode()],
  "twice": b.merge_statusline(json.loads(json.dumps(wrapped)), True) == wrapped,
  "back": b.merge_statusline(wrapped, False)["statusLine"],
  "gone": "statusLine" in b.merge_statusline(b.merge_statusline({}, True), False),
}))`)
  assert.equal(out.line, 'Opus', 'the status line that was there still prints')
  assert.deepEqual(out.noted, [23.5, 41.2, 'status line'])
  assert.deepEqual(out.quiet, [0, ''], 'a session with no limits changes nothing and prints nothing')
  assert.equal(out.twice, true, 'installing twice does not wrap twice')
  assert.deepEqual(out.back, { type: 'command', command: "jq -r '.model.display_name' | tr a-z A-Z", padding: 0 })
  assert.equal(out.gone, false)
})

test('the plugin says what the app and the hook say', () => {
  const plugin = JSON.parse(read('.claude-plugin/plugin.json'))
  const market = JSON.parse(read('.claude-plugin/marketplace.json'))
  const host = py('import busyclaude as b; print(json.dumps({"name": b.PLUGIN_NAME, "hooks": b.hooks_block(b.PLUGIN_HOOK_COMMAND)}))')
  assert.equal(plugin.version, manifest.version, 'one version: the manifest, package.json and the plugin')
  assert.equal(plugin.name, host.name)
  assert.deepEqual(market.plugins.map((p) => [p.name, p.source]), [[host.name, './']], 'the marketplace lists this repository as the plugin')
  assert.deepEqual(JSON.parse(read('hooks/hooks.json')).hooks, host.hooks)
  assert.match(read('skills/setup/SKILL.md'), /busyclaude\.py" setup --plugin/)
})

test('the package the plugin ships is what this source builds', () => {
  // Everyone without the build tools, a plugin's copy included, installs release/<id>.tgz.
  // `npm run package` refreshes it; this fails when someone changed src/ and did not.
  const unpack = (archive) => {
    const dir = mkdtempSync(join(tmpdir(), 'busyclaude-pkg-'))
    execFileSync('tar', ['-xzf', join(ROOT, archive), '-C', dir])
    const files = {}
    const walk = (at) => {
      for (const name of readdirSync(at)) {
        const path = join(at, name)
        if (statSync(path).isDirectory()) walk(path)
        // macOS tar adds PaxHeader entries for extended attributes; the bar skips them, and so does this.
        else if (!path.includes('PaxHeader')) files[path.slice(dir.length + 1)] = readFileSync(path).toString('base64')
      }
    }
    walk(dir)
    rmSync(dir, { recursive: true, force: true })
    return files
  }
  const built = `dist/${manifest.id}.tgz`
  assert.ok(existsSync(join(ROOT, built)), `${built} is missing: npm test builds it first`)
  // macOS tar stores extended attributes as AppleDouble files (._name) beside every file unless
  // COPYFILE_DISABLE is set, and the bar unpacks them as files. The tar on a Mac hides them again
  // when it unpacks, so the archive's own list of entries is what is checked.
  const appleDouble = py(`import tarfile; print(json.dumps([n for n in tarfile.open(${JSON.stringify(join(ROOT, 'release', `${manifest.id}.tgz`))}).getnames() if n.split("/")[-1].startswith("._")]))`)
  assert.deepEqual(appleDouble, [], 'release/ holds macOS AppleDouble files: npm run package builds without them')
  const shipped = unpack(`release/${manifest.id}.tgz`)
  const fresh = unpack(built)
  assert.deepEqual(Object.keys(shipped).sort(), Object.keys(fresh).sort())
  for (const name of Object.keys(fresh)) assert.ok(shipped[name] === fresh[name], `${name} in release/ is out of date: run npm run package`)
})

