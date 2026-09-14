# ccdash

A terminal dashboard over the coding agents running on one machine: their plan limits, what
today cost, and what each repo's agents are doing right now.

## Language

### What it watches

**Agent**:
A coding CLI whose transcripts ccdash reads: Claude Code, Codex CLI or opencode.
_Avoid_: tool, assistant, provider

**Session**:
One conversation with an agent, read from its transcript; working, waiting, error or idle.
_Avoid_: chat, conversation, thread

**Repo**:
A git repository found under the root, together with all of its worktrees.
_Avoid_: project

**Worktree**:
One checkout of a repo: its own main checkout, or one added beside it. A session belongs to
the worktree its folder is in.
_Avoid_: branch, checkout

**Other**:
The stand-in repo that holds every session in no repo under the root. It has no worktrees.

### Screens

**Repos screen**:
The top-level screen: one card per repo.
_Avoid_: home, overview, repo view

**Repo screen**:
The screen for one opened repo: its worktrees, each followed by its sessions, as a list.
_Avoid_: detail view, worktree view
