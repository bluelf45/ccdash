# Contributing

Thanks for looking. ccdash is one file, `ccdash.py`, and it stays that way.

## The rules

- **Standard library only.** Pillow is the one optional dependency, for animated art and
  sixel; everything must work without it.
- **Python 3.8.** No walrus-free purity needed (3.8 has it), but no `match`, no
  `X | Y` type syntax, no `dict |` merge.
- **No new files.** A new module is a no; so is a new config file, a test directory or a
  vendored helper.
- **POSIX.** Linux and macOS. `termios`, `tty`, `fcntl` and `resource` are already imported.

## The shape of the file

Sections are marked with a `# ---- name` comment band, in dependency order: settings, themes,
text and colour, the terminal probe, limits, sessions and readers, repos, panels, draw,
selftest, demo, perf, tui, main.

Two invariants hold the paint loop together:

- **`render()` is pure.** Same data in, same lines out — no disk, no network, no clock
  beyond `datetime.now()`. That is why a resize costs nothing and why `--bench` can time it.
- **`Refresher` owns all slow I/O.** It publishes `snap`, replaced whole. The main loop reads
  it once a tick and never writes to it; nothing mutates a published snapshot.

## Tests

The tests live in `selftest()`, in the same file:

```sh
python3 ccdash.py --selftest     # prints: ok
```

They are bare `assert expr, "prose reason"` — the reason says what the code is *for*, not
what the assert compares. Where a check needs different globals, swap and restore by hand:

```python
real_file, CONFIG_FILE = CONFIG_FILE, tempfile.mktemp()
...
CONFIG_FILE = real_file
```

Everything a check needs must be built inline — a fixture string, `tempfile.mkdtemp()`, a
lambda standing in for `snapshot`. No fixture files, no network, no reading the real
`~/.claude`.

## Adding an agent

1. An entry in `AGENTS`: where its transcripts live (`glob` with `%s` for the session id, or
   `list` when they are rows in a database, like opencode's), and how to resume one.
2. A reader that returns what `blank()` holds.
3. An inline fixture in `selftest()`, built from a real transcript, asserting what the reader
   pulls out of it.
4. The agent's name in `DEFAULTS["agents"]`, and the glob in `apply_config()`'s rewrite loop
   if its folder is configurable.

## Adding a setting

1. A key in `DEFAULTS`, with the value it is worth when the file leaves it out.
2. A `RANGE` entry if it is a number — every numeric knob is clamped, never rejected.
3. A line in `apply_config()` putting it into the global the code already reads.
4. A row in the README's Configuration table.
5. A selftest assertion for whatever the knob actually changes.

`ENVS` is for the handful of knobs worth overriding for one run; most are not.

## Before you open a PR

```sh
python3 ccdash.py --selftest
python3 ccdash.py --demo --once          # the layout still lays out
ruff check ccdash.py                     # ruff 0.16.7, config in pyproject.toml
```

If you touched the paint loop — `render`, `paint`, `sync`, the panels, the art — run
`ccdash --bench 200` before and after and put both numbers in the PR. A repaint that got
slower needs a reason.
