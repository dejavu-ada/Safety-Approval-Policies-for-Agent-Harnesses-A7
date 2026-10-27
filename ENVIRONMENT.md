# Environment and verification status

- Host: macOS 15, Apple Silicon (`arm64`)
- Python: 3.12.14
- Virtual environment: `.venv/`
- AgentDojo: 0.1.35
- pytest: 9.1.1
- Dependency lock: `requirements.lock`

## Verified

- AgentDojo's installed source was inspected before implementing the executor integration.
- The approval gate replaces AgentDojo's actual `ToolsExecutor` and checks nested tool calls before execution.
- The local test suite passed: `28 passed` using `./.venv/bin/python -m pytest`.
- A live smoke benchmark completed under `results/my-smoke/`: workspace `user_task_0`, direct attack, 14 injection cases, budgets `k=0,1`, all three policies.
- The AgentDojo warning about injection tasks not being solved as normal user tasks is emitted by a separate preflight check; the benchmark continued and wrote its results.

## Current experiment limits

- The smoke run covers one of 40 workspace user tasks, not the full suite.
- No over-permissive failure candidate appeared in this run.
- `metrics.csv` cost totals cover scored user-task cases and omit model usage from AgentDojo's standalone injection-task checks. They are partial estimates, not the provider's complete bill.
- Full multi-k and full-suite evaluation remain for the testing teammate to run after reviewing the expected API cost.
