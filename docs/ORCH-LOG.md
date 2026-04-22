# Orchestrator Arbitration Log

Append-only record of cross-stream decisions the orchestrator makes when streams cross file-ownership boundaries or need a shared contract changed. Prevents the rationale from evaporating.

Streams are defined in [PRODUCT-PLAN.md](PRODUCT-PLAN.md#parallel-agent-workstreams): A (Hub), B (CPU tools), C (Pipeline integrations), D (Atomics factory, with sub-streams D1..D9), E (Kendrew validation), F (Marketing), G (Enterprise hardening).

## How to append

One entry per decision. Format:

```
### <YYYY-MM-DD> — <short title>

- **Requested by:** stream <X>
- **Affects:** stream(s) <Y>, file(s) <path>
- **Question:** <what needed deciding>
- **Decision:** <what the orchestrator ruled>
- **Rationale:** <why — one or two sentences>
- **Follow-up:** <commit SHA of the resulting change, or "n/a">
```

Never edit past entries. If a decision is reversed, append a new entry referencing the earlier one.

---

## Entries

_No entries yet. First arbitration expected during Wave 0 as Stream A publishes the `modal_client.submit()` contract and Stream E stands up the Modal `staging` environment._
