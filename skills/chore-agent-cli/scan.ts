// Scans Claude Code transcripts and prints agent-cli improvement signals.
// bun scan.ts [--scope project|user] [--days N] [--min-sessions N] [--min-projects N] [--json]
import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

const { values: opt } = parseArgs({
  strict: true,
  options: {
    scope: { type: 'string' },
    days: { type: 'string', default: '7' },
    'min-sessions': { type: 'string', default: '3' },
    'min-projects': { type: 'string', default: '2' },
    json: { type: 'boolean', default: false },
  },
});
const int = (name: 'days' | 'min-sessions' | 'min-projects') => {
  const n = Number(opt[name]);
  if (!Number.isInteger(n) || n < 1) throw new Error(`--${name} expects a positive integer, got ${opt[name]}`);
  return n;
};
const DAYS = int('days');
const MIN_SESSIONS = int('min-sessions');

const HOME = homedir();
const CONFIG = process.env.CLAUDE_CONFIG_DIR ?? join(HOME, '.claude');
const PROJECTS = join(CONFIG, 'projects');
const git = spawnSync('git', ['rev-parse', '--show-toplevel']);
const ROOT = git.status === 0 ? git.stdout.toString().trim() : process.cwd();

const USER = { cli: join(CONFIG, 'bin/agent-cli'), mod: join(CONFIG, 'skills/agent-cli-first/hooks/register.ts') };
const PROJECT = { cli: join(ROOT, 'bin/agent-cli'), mod: join(ROOT, '.claude/skills/agent-cli-first/hooks/register.ts') };
// A project scan also honours the user layer: what it covers is not a project candidate.
// User scope ranks by spread across projects: a habit of one repo belongs to that repo's CLI.
const SCOPES = {
  project: {
    root: ROOT,
    layers: [USER, PROJECT],
    dirs: () => [ROOT.replace(/[^a-zA-Z0-9]/g, '-')],
    minProjects: 1,
  },
  user: {
    root: CONFIG,
    layers: [USER],
    dirs: () => (existsSync(PROJECTS) ? readdirSync(PROJECTS) : []),
    minProjects: int('min-projects'),
  },
};
// In the config dir's own repo the project CLI is the user CLI, so a project scan would only narrow the user one.
const inConfigRepo = existsSync(CONFIG) && realpathSync(ROOT) === realpathSync(CONFIG);
const scope = opt.scope ?? (git.status === 0 && !inConfigRepo ? 'project' : 'user');
const S = SCOPES[scope as keyof typeof SCOPES];
if (!S) throw new Error(`--scope expects ${Object.keys(SCOPES).join('|')}, got ${scope}`);

const RAW = 'AGENT_CLI_RAW=1';
const DENY = /agent-cli-first: use `(?:\S*\/)?agent-cli ([a-z0-9-]+)/;
const CLI_CALL = /(?:^|[\s;&|(])((?:\S*\/)?agent-cli)(?:\s+(\S+))?/;
// Text utilities count only with a literal argument: `cat package.json` is a lookup, `grep -n` is noise.
const UTILS = new Set('grep rg head tail wc sed awk cut sort uniq tr xargs cat ls find diff jq'.split(' '));
const NOISE = new Set(
  'command cd echo printf for do done if then else fi while sleep true false export set local test wait exit return'.split(' ')
);
// Tools whose second word is a subcommand: the family keeps one more token.
const DEEP = new Set(
  'bun bunx npm npx pnpm yarn deno git gh glab jira make docker kubectl cargo go uv poetry pip python python3 composer php'.split(' ')
);

type Hook = (...a: unknown[]) => Promise<{ deny?: string }>;
const hooks: Hook[] = [];
const fake$ = { plugin: { name: 'scan' }, ui: { status() {} }, clock: { after: () => ({ cancel() {} }) } };
const SUBCOMMANDS = new Set<string>();
for (const { cli, mod } of S.layers) {
  if (existsSync(mod)) {
    const { register } = await import(mod);
    register((ev: string, m: { tool?: string }, hook: Hook) => {
      if (ev === 'tool.call' && m.tool === 'Bash') hooks.push(hook);
      return { catch() {} };
    }, {});
  }
  const help = existsSync(cli) ? spawnSync(cli, ['help', '--json']) : null;
  if (help?.status === 0) for (const name of Object.keys(JSON.parse(help.stdout.toString()))) SUBCOMMANDS.add(name);
}
// A call to another layer's CLI (`./bin/agent-cli` in a user scan) is not this scope's use or error,
// but no agent-cli call is a raw command either.
const LAYER_CLIS = new Set(S.layers.map((l) => l.cli));
// A relative path with no known cwd is not resolved: the scope root stands in for cwd and would be a guess.
const isLayerCli = (token: string, cwd: string | undefined) => {
  if (token.startsWith('~/')) return LAYER_CLIS.has(join(HOME, token.slice(2)));
  if (token.startsWith('/')) return LAYER_CLIS.has(token);
  return token.includes('/') && cwd !== undefined && LAYER_CLIS.has(resolve(cwd, token));
};
// No subcommand, a flag (`--help`) or a redirect (`2>&1`) prints the top help.
const subcommandOf = (arg = '') => {
  if (arg === 'help' || !/^[a-z]/.test(arg)) return 'help';
  return SUBCOMMANDS.has(arg) ? arg : `unknown:${arg}`;
};

const covered = new Map<string, boolean>();
const isCovered = async (command: string) => {
  if (!covered.has(command)) {
    let deny = false;
    for (const h of hooks) if ((deny = !!(await h(fake$, { command }, async () => ({}))).deny)) break;
    covered.set(command, deny);
  }
  return covered.get(command)!;
};

const rel = (s: string, cwd: string) => s.replaceAll(`${cwd}/`, '').replaceAll(HOME, '~');
const normalize = (s: string, cwd: string) =>
  rel(s, cwd)
    .replace(/'[^']*'|"[^"]*"/g, '<s>')
    // Path arguments vary per task; the first token keeps its path so `./bin/x` stays a family.
    .replace(/(\s)(?:~|\.{1,2})?\/?[\w.@+-]+(?:\/[\w.@+*-]*)+/g, '$1<path>')
    .replace(/\b[A-Z]+-\d+\b/g, '<ticket>')
    .replace(/\b\d{2,}\b/g, '<n>')
    .replace(/\s*(?:2>&1|2>\/dev\/null)/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 140);
const familyOf = (seg: string) => {
  const t = seg.split(' ').filter((x) => !/^[A-Z_]+=/.test(x));
  if (!t[0] || NOISE.has(t[0]) || !/^[a-z./~]/.test(t[0])) return null;
  if (UTILS.has(t[0])) {
    const literal = t.slice(1).find((x) => !x.startsWith('-') && !x.includes('<'));
    return literal ? `${t[0]} ${literal}` : null;
  }
  return t.slice(0, DEEP.has(t[0]) ? 3 : 2).join(' ');
};
// Heredoc bodies and quoted scripts (`python3 -c "…"`) are data, not commands, but the rest of the heredoc's opening line and the lines after it are commands. A pipeline counts by its first stage.
const HEREDOC = /(?<!<)<<-?\s*(['"]?)([\w.-]+)\1([^\n]*)\n(?:[\s\S]*?\n)?[\t ]*\2[\t ]*(?=\n|$)/g;
const segments = (command: string, cwd: string) =>
  command
    .replace(HEREDOC, '$3')
    .replace(/'[^']*'|"[^"]*"/g, '<s>')
    .split(/\s*(?:&&|\|\||;|\n)\s*/)
    .map((s) => normalize(s.split(/\s\|\s/)[0], cwd))
    .filter(Boolean);

type Bucket = { sessions: Set<string>; projects: Set<string>; calls: number; samples: Set<string> };
const bucket = () => new Map<string, Bucket>();
const add = (m: Map<string, Bucket>, key: string, sid: string, project: string, sample: string) => {
  const b = m.get(key) ?? { sessions: new Set(), projects: new Set(), calls: 0, samples: new Set() };
  b.sessions.add(sid);
  b.projects.add(project);
  b.calls++;
  if (b.samples.size < 3 && sample) b.samples.add(sample.slice(0, 160));
  m.set(key, b);
};

const raw = bucket(),
  rawBypass = bucket(),
  denied = bucket(),
  cliUse = bucket(),
  cliErrors = bucket(),
  reads = bucket();
const cut = Date.now() - DAYS * 864e5;
const dirs = S.dirs()
  .map((d) => join(PROJECTS, d))
  .filter((d) => existsSync(d) && statSync(d).isDirectory());
if (!dirs.length) throw new Error(`no transcripts for scope ${scope} under ${PROJECTS}`);
const sessions = new Set<string>();
let subagents = 0,
  bashCalls = 0;
const projectsSeen = new Set<string>();

// A subagent transcript `<session>/subagents/agent-*.jsonl` counts toward its parent session.
const transcripts = (dir: string) =>
  readdirSync(dir).flatMap((f) => {
    if (f.endsWith('.jsonl')) return [{ path: join(dir, f), sid: `${dir}/${f.slice(0, -6)}`, sub: false }];
    const sub = join(dir, f, 'subagents');
    return existsSync(sub)
      ? readdirSync(sub)
          .filter((a) => a.endsWith('.jsonl'))
          .map((a) => ({ path: join(sub, a), sid: `${dir}/${f}`, sub: true }))
      : [];
  });

for (const dir of dirs) {
  for (const { path, sid, sub } of transcripts(dir)) {
    if (statSync(path).mtimeMs < cut) continue;
    sessions.add(sid);
    if (sub) subagents++;
    projectsSeen.add(dir);
    const pending = new Map<string, { command: string; cwd: string; isOwn: boolean }>();
    // Some lines carry no cwd: the last one the transcript named still holds.
    let lastCwd: string | undefined;
    for (const line of readFileSync(path, 'utf8').split('\n')) {
      if (!line.includes('"tool_use"') && !line.includes('"tool_result"')) continue;
      let o;
      try {
        o = JSON.parse(line);
      } catch {
        continue;
      }
      lastCwd = o.cwd ?? lastCwd;
      const cwd: string = lastCwd ?? ROOT;
      for (const b of o.message?.content ?? []) {
        if (b?.type === 'tool_use' && b.name === 'Read') {
          add(reads, rel(String(b.input?.file_path ?? ''), cwd), sid, dir, '');
        } else if (b?.type === 'tool_use' && b.name === 'Bash') {
          const command: string = b.input?.command ?? '';
          const [, bin, arg] = command.match(CLI_CALL) ?? [];
          const isOwn = bin !== undefined && isLayerCli(bin, lastCwd);
          bashCalls++;
          pending.set(b.id, { command, cwd, isOwn });
          if (isOwn) add(cliUse, subcommandOf(arg), sid, dir, normalize(command, cwd));
          if (command.includes(RAW)) {
            const sample = normalize(command, cwd);
            const seg = segments(command, cwd).find((s) => s.includes(RAW)) ?? sample;
            add(rawBypass, familyOf(seg.replace(RAW, '').trim()) ?? seg, sid, dir, sample);
            continue;
          }
          if (bin || (await isCovered(command))) continue;
          for (const seg of segments(command, cwd)) {
            const fam = familyOf(seg);
            if (fam) add(raw, fam, sid, dir, seg);
          }
        } else if (b?.type === 'tool_result' && pending.has(b.tool_use_id)) {
          const call = pending.get(b.tool_use_id)!;
          const text = [b.content ?? []]
            .flat()
            .map((c: string | { text?: string }) => (typeof c === 'string' ? c : (c?.text ?? '')))
            .join('');
          const sub = text.match(DENY);
          if (sub) add(denied, sub[1], sid, dir, normalize(call.command, call.cwd));
          else if (call.isOwn &&/(^|\n)error: /.test(text))
            add(cliErrors, text.match(/error: (.+)/)![1].slice(0, 100), sid, dir, normalize(call.command, call.cwd));
        }
      }
    }
  }
}

const top = (m: Map<string, Bucket>, repeated = false) =>
  [...m]
    .filter(([, b]) => !repeated || (b.sessions.size >= MIN_SESSIONS && b.projects.size >= S.minProjects))
    .sort(
      (a, b) =>
        b[1].projects.size - a[1].projects.size || b[1].sessions.size - a[1].sessions.size || b[1].calls - a[1].calls
    )
    .slice(0, 25)
    .map(([key, b]) => ({ key, projects: b.projects.size, sessions: b.sessions.size, calls: b.calls, samples: [...b.samples] }));

const window = {
  scope,
  root: S.root,
  days: DAYS,
  projects: projectsSeen.size,
  sessions: sessions.size,
  subagents,
  bashCalls,
  minSessions: MIN_SESSIONS,
  minProjects: S.minProjects,
};
const layers = S.layers.map(({ cli, mod }) => ({ cli: existsSync(cli) ? cli : null, mod: existsSync(mod) ? mod : null }));
const sections = {
  rawBypass: top(rawBypass),
  cliErrors: top(cliErrors),
  repeatedRaw: top(raw, true),
  repeatedReads: top(reads, true),
  denied: top(denied),
  cliUse: top(cliUse),
};

if (opt.json) console.log(JSON.stringify({ window: { ...window, layers }, ...sections }));
else {
  console.log(Object.entries(window).map(([k, v]) => `${k}=${v}`).join(' '));
  for (const l of layers) console.log(`layer cli=${l.cli ?? '-'} mod=${l.mod ?? '-'}`);
  for (const [name, rows] of Object.entries(sections)) {
    console.log(`## ${name} (${rows.length})`);
    for (const r of rows) {
      const p = S.minProjects > 1 ? `${String(r.projects).padStart(3)}p ` : '';
      console.log(`${p}${String(r.sessions).padStart(3)}s ${String(r.calls).padStart(4)}c  ${r.key}`);
      for (const s of r.samples) console.log(`          · ${s}`);
    }
  }
}
