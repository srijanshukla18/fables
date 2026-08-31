## Debugging & Investigation

When investigating Kubernetes issues, always cite specific kubectl output
(events, describe, logs) as evidence before stating conclusions. Never assert a
root cause without pointing to the exact line or event that supports it.

## Workflow Conventions

For production readiness audits and large TODO lists, break work into discrete
checkpointed steps. After completing each step, summarize what was done and what
remains so the session can be resumed if interrupted.
