# ccdash

A terminal dashboard for your coding agents. **Claude Code**, **Codex CLI** and **opencode**
sessions show up side by side: plan limits and whether you'll hit them, what today cost, and a
card per git repo — open one for a list of its worktrees and a live row per session saying which
ones are working and which are waiting on you.

![ccdash --demo](https://raw.githubusercontent.com/bluelf45/ccdash/main/docs/demo.png)

- **Limits:** Claude's 5-hour and weekly limits, and Codex's own, each with a pace marker and
  the time it runs out if you keep going like this. When one agent is about to run dry and
  another has room, it says so: `→ Claude 5h full by 15:12 · Codex 78% left`.
- **Today:** cost at API prices, burn rate against a usual day, tokens, cache hit rate, a
  per-model split, an hourly chart, and the last 14 days (`history_days`).
- **Repos:** one card per git repo under `root` (your home by default, 3 levels deep):
  its branch, its other worktrees, and how many of its sessions are working, waiting or idle.
  Sessions in no repo there go on an `other` card.
- **Worktrees and sessions:** ⏎ on a repo lists each worktree followed by a row per session —
  working, waiting on you, error or idle; what you asked, what the agent said or which tool
  it's in, cost and context used. `c` adds a worktree beside the repo (`repo.branch`), `D`
  removes one (git keeps it if it has changes; the branch stays), ⏎ on a worktree starts a new
  claude in it. It rings the bell when a session stops to wait on you.
- **Jump in:** the dashboard runs in a tmux server of its own per run; pick a session and press ⏎
  to reopen that session in its own window there, with a column of limits and stats beside it.
  `alt+0` comes back, `alt+1`–`9` switch sessions. Quitting asks first, then closes that server,
  agent windows included. Inside your own tmux, ⏎ opens the session as a window there too, but
  the `alt+` keys aren't bound.
- One Python file, standard library only. Pillow is optional, for your own animated art (and
  real pixels on sixel terminals). Everything is a setting in one JSON file, and `p` shows
  what the dashboard itself costs to run.

## Install

```sh
pipx install 'ccdash[art]'          # or: uv tool install 'ccdash[art]'
```

From GitHub, before a release lands on PyPI:

```sh
pipx install 'ccdash[art] @ git+https://github.com/bluelf45/ccdash'
```

Or grab the file — it's all there is:

```sh
curl -o ~/.local/bin/ccdash https://raw.githubusercontent.com/bluelf45/ccdash/main/ccdash.py
chmod +x ~/.local/bin/ccdash
```

Needs Python 3.8+ on Linux or macOS (Windows through WSL). tmux is optional: without it, ⏎
runs the agent in place and quitting it brings the dashboard back.

Then run `ccdash`. `ccdash --demo` shows it with made-up sessions, and `ccdash --doctor` tells
you what it can and can't see.

## Keys

| key | |
|---|---|
| `h j k l` / arrows | pick a card, or a row in an opened repo |
| `⏎` | open the picked repo; on a worktree, a new claude there; on a session, resume it |
| `esc` | back to the repos; there, drop the pick and filter |
| `c` | in a repo: add a worktree for a branch (new, or one that exists) |
| `D` | remove the picked worktree — asks first |
| `/` | filter by name, branch, title or prompt |
| `g` | hourly chart: $ / output tokens / all tokens |
| `t` | next theme (remembered) |
| `r` | refresh now |
| `p` | what ccdash itself costs: render, paint, scan, CPU, memory |
| `J K` / PgUp PgDn | scroll the cards, or the list in an opened repo |
| `wheel` | the same — the clock, limits and stats never move |
| `ctrl+z` | suspend — `fg` comes back repainted |
| `?` | help |
| `q` | quit — asks first when agent windows are open (`ctrl+c` too; `ctrl+d` quits without asking) |

## Extras

**Exact "waiting" cards.** A transcript can't show that Claude is stuck on a permission prompt,
so by default a tool call that goes quiet for 60 seconds (`wait_after`) counts as waiting. A
Notification hook makes it exact. ccdash looks for it in `~/.claude/settings.json` (or
`$CLAUDE_CONFIG_DIR/settings.json`) and falls back to the timed guess when it isn't there.
Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "permission_prompt",
        "hooks": [{ "type": "command", "command": "ccdash --hook" }]
      }
    ]
  }
}
```

**Claude Code status line.** `ccdash --statusline` prints one line such as
`5h 53% · week 47% · codex 5h 22% · $49.6 today`. Claude's limits are the ones Claude Code
passes it, so it makes no request of its own. Codex's limits and today's cost are as of the
last time the dashboard looked, so leave one running.

```json
{ "statusLine": { "type": "command", "command": "ccdash --statusline" } }
```

## Configuration

Settings live in `~/.config/ccdash/config.json`, hand-edited. `ccdash --config` prints what
this run goes by, so writing a starter file is one command:

```sh
ccdash --config > ~/.config/ccdash/config.json
```

A setting the file leaves out keeps its default; a key it doesn't know, or a value of the
wrong shape, is a line in `ccdash --doctor` and nothing more — a broken config never stops
the dashboard. `--doctor` also says where each setting came from.

| key | default | |
|---|---|---|
| `root` | `"~"` | where your repos live |
| `depth` | `3` | how deep under `root` to look; hidden folders are skipped (1–8) |
| `theme` | `""` | theme name; `t` cycles and writes it back here. Empty means `ccdash` |
| `art` | `""` | path to your art; empty means `~/.config/ccdash/art.gif` |
| `sixel` | `true` | `false` forces half blocks even on a sixel terminal |
| `frame` | `0.12` | seconds per art frame; `0` stops on the first one (0–5) |
| `refresh` | `5.0` | seconds between transcript rescans (1–300) |
| `history_days` | `14` | days in the history chart, and how far back costs are summed (1–90) |
| `max_cards` | `24` | sessions the grid keeps on cards (4–200) |
| `wait_after` | `60` | seconds a quiet tool call takes to read as "waiting on you" (5–3600) |
| `idle_after` | `1800` | seconds without a turn before a session reads as idle (60–86400) |
| `bell` | `true` | `false` mutes the bell a session rings when it stops to wait on you |
| `agents` | `["claude", "codex", "opencode"]` | which agents to read at all |
| `claude_dir` | `""` | Claude's folder, if you moved it (else `~/.claude`) |
| `codex_home` | `""` | Codex's folder, if you moved it (else `~/.codex`) |
| `opencode_db` | `""` | opencode's database file, if you moved it |
| `prices` | `{}` | your own `$` per 1M tokens: `{"gpt-5.6-terra": [2.0, 12.0]}` — `[input, output]`, merged over the built-in table |

An environment variable beats the file, for one run: `CCDASH_ROOT`, `CCDASH_THEME`,
`CCDASH_ART`, `CCDASH_SIXEL=0`, `CCDASH_FRAME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`.
(`XDG_DATA_HOME` still moves opencode's default database path.)

Two files config.json doesn't cover:

| | |
|---|---|
| `~/.config/ccdash/art.gif` | your art: an animated gif plays, a still image sits there. Without it you get the built-in chibi. |
| `~/.config/ccdash/themes.json` | your own themes: `{"mine": {"bg": "#101010", "acc": "#ff8800"}}`. Missing roles come from the default theme. Roles: `bg txt mut dim bor acc blu grn yel red` |

Themes: ccdash, catppuccin-mocha, catppuccin-latte, dracula, nord, tokyo-night, gruvbox,
rose-pine, solarized.

Bring your own art, but please don't commit sprites you don't have the rights to.

## Privacy

ccdash reads the transcripts your agents already keep on your disk (`~/.claude/projects`,
`~/.codex/sessions`, and opencode's database, opened read-only) and writes only to
`~/.config/ccdash` and `~/.cache/ccdash`. It runs `git worktree list` in each repo it finds;
`git worktree add` and `remove` run only when you press `c` or `D`.

It makes one kind of network request: for Claude's plan limits, it sends the OAuth token Claude
Code saved (in `~/.claude/.credentials.json`, or the macOS Keychain) to
`api.anthropic.com/api/oauth/usage` — the same endpoint Claude Code uses. At most once every 5
minutes, and it backs off when rate limited. The endpoint is undocumented and could change.
Codex's limits come from its transcripts, with no request at all.

Costs are what the API would charge for those tokens. On a subscription you aren't billed per
token, so read it as how much you're getting out of the plan. For opencode, ccdash shows the
cost opencode itself records.

## What ccdash costs to run

`p` puts up a box with what this run has spent on itself: the p50/p95/worst of a layout, a
repaint, an art frame, a transcript scan and a limits fetch, plus bytes written, CPU,
resident memory and frames drawn per second. It refreshes once a second while it's up, and
the counters are always on, so the box is never empty when you open it.

```sh
ccdash --bench 200     # 200 frames rendered and painted into nothing, then the same report
ccdash --profile       # opens the box on start and leaves the report in ~/.cache/ccdash/perf.txt
```

`--bench` is the one to quote when a change touches the paint loop: it needs no terminal, so
the numbers are comparable between runs.

## Contributing

It's one file, `ccdash.py`, and the tests live in it too:

```sh
python3 ccdash.py --selftest
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the rest: the shape of the file, how to add an agent
or a setting, and what to run before opening a PR.

## License

MIT. Not affiliated with Anthropic, OpenAI or the opencode project.
