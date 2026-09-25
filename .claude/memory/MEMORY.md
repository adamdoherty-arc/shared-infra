# shared-infra memory index

Facts that matter across sessions, that aren't already load-bearing enough
to belong in `.claude/rules/`. Each topic file is dated; update in place
when a fact changes, don't leave stale copies. `~/.claude/CLAUDE.md`'s
weekly `claude-md-curator` job is pointed at this repo too — a stale topic
file found there is a finding, not a note.

## Topics

- [`bifrost-architecture.md`](topics/bifrost-architecture.md) — gateway
  topology, VK table, provider dual-state mechanics.
- [`engine-tuning.md`](topics/engine-tuning.md) — vLLM/llama.cpp flag
  history, VRAM measurements, bench results per engine attempt.
- [`config-sync.md`](topics/config-sync.md) — who writes `bifrost/
  config.json`/`config.db`, when, and the sync mechanics between them.

## How this differs from `.claude/rules/`

Rules are binding operating procedure with a hook/ratchet/test backing
them (`00-critical.md`'s "wish" rule). Memory is factual ground truth
(current provider set, current model, current measured numbers) that
changes on its own schedule as the live system changes — it doesn't need
a hook to enforce it, it needs a date stamp so a reader knows how stale it
might be. When a memory fact becomes something you'd want a hook to
enforce (e.g. "always run the sync script after a config edit"), promote
it into a rule file instead of leaving it only here.
