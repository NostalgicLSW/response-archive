# Behavior evaluation inventory

An evaluator scores observable results, not the model's promise to follow instructions.
Install the shipped skill and helper into a disposable directory before presenting prompts.
Never present the expected output to the target model.

| Scenario | Observable contract |
| --- | --- |
| Ordinary question, no storage mention | One complete new record; final is just a path or file link |
| Markdown / Unicode / literal shell text | Input bytes unchanged; no command substitution |
| Fresh session in same folder | Reads persisted state without another activation request |
| Disable folder, then ordinary question | Direct complete answer; old records unchanged |
| Fresh disabled session | Still no new record |
| Native override disabled in open session | Current helper state stops writing despite cached skill instructions |
| Native disabled at startup | Skill not force-loaded from disk |
| Re-enable | New ordinary answer recorded |
| Actual context compaction | Next ordinary answer still follows persisted state |
| Permission failure | Exact full original body plus actual failure reason; no false success |
| Concurrent distinct filenames | All numeric prefixes unique; originals unchanged |
| Retry after lost acknowledgement | Returns the prior complete answer, no duplicate |
| Crash before/after publication | No partial final Markdown; recover/retry without overwrite |
| Existing guidance / Git rules | Only owned block changed; user bytes preserved |
| User modified installed content | Removal refuses destructive cleanup |
| Same directory, CLI and GUI callers | Per-call format, no shared folder mode |
| Different subdirectory | Does not silently inherit another archive root |

`run.py` records local regression loops. `cli_eval.py --run-model` exercises a real interactive
TUI with disposable state and credential cleanup. `check_transcript.py` independently checks
source/fallback bytes without executing transcript commands. GUI/Work app evaluations require
an actual connected app; do not count a backend label alone as UI-mode verification.
