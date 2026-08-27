# CLAUDE.md

@AGENTS.md

## Claude Code-specific notes

- The "never use real market data/network in tests" rule above is load-bearing for this user
  specifically -- treat it as a hard constraint for any CLI verification, not just a style
  preference.
- Use the `code-review` skill for review passes on this repo rather than ad hoc reading; it already
  knows to check reuse/simplification/efficiency alongside correctness.
- When adding a new `AllocationTemplate` (or a new selection/weighting/entry/exit aspect), add a
  parity or contract test in the matching `tests/` directory before considering the change done --
  this codebase's test suites are what caught both real bugs introduced during the aspect-composition
  work, and its docstring/comment density exists specifically so the next change (yours) doesn't
  have to rediscover the same gotchas.
