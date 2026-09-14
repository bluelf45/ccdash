# Security

## What ccdash touches

- **Reads** the transcripts your agents already keep on your disk: `~/.claude/projects`,
  `~/.codex/sessions`, and opencode's SQLite database, opened read-only. It parses them; it
  never writes to them.
- **Sends** one thing over the network: the OAuth token Claude Code saved (in
  `~/.claude/.credentials.json`, or the macOS Keychain) to `api.anthropic.com/api/oauth/usage`,
  to read your plan limits — the same endpoint Claude Code itself uses. At most once every
  5 minutes. There is no other request, no telemetry, and no other host.
- **Writes** only `~/.config/ccdash` (settings and themes) and `~/.cache/ccdash` (the scan
  and limits caches, and the `--profile` report). Both honour `$XDG_CONFIG_HOME` and
  `$XDG_CACHE_HOME`.
- **Runs** `git worktree list` in each repo it finds. `git worktree add` and `git worktree
  remove` run only when you press `c` or `D`. It runs `tmux` to open windows, and the agent's
  own command (`claude`, `codex`, `opencode`) when you resume a session.

## Reporting a vulnerability

Please report privately, not in a public issue: use GitHub's private vulnerability reporting
on this repository (Security → Report a vulnerability).

Include what an attacker can do, and how you got there — a transcript, a settings file or a
terminal that triggers it is worth more than a description. A first reply should come within
a week.

Things worth reporting: the token reaching any host other than `api.anthropic.com`, a
transcript or config file that makes ccdash execute something, a write outside the two
directories above, or a path that leaks credentials into the drawn screen, the scan cache or
the `--profile` report.
