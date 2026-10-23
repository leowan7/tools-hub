# tools-hub — handoff 2026-08-19 (idempotency race and wallet double charge)

Two ways the site could take a customer's money twice are now closed, both shipped to production. The idempotency guard that protects ten payment routes was never actually locking anything, and the wallet's automatic top-up could fire two charges from a single burst of finished jobs. Both are fixed, merged and deployed, and the required database change has been applied. Six independent review agents checked the work; nothing is blocked and nothing needs a decision.

## Done this session

- Fixed the idempotency guard so exactly one caller wins a race. It previously used a database call that quietly succeeds for every simultaneous caller, under a comment claiming the opposite. Shipped as PR #162, merged `2f0637c`, deploy green.
- Fixed the wallet auto-reload double charge. The 24 hour limit counted a record that only appears after Stripe confirms payment, so two jobs finishing a second apart both saw zero and both charged the card. Shipped as PR #163, merged `41a0933`, deploy green.
- Applied migration `0042` to the production database, which PR #163 requires. Nothing in this repo applies migrations automatically, so this was a manual step and is now done.
- Made the idempotency guard refuse the request instead of letting it through when the database errors. It previously logged "failing open" and ran the payment anyway.
- Corrected the premise the work was requested under. This was briefed as preventative, needed before a future change to how the web server handles concurrency. It was already live: the shipped server config runs two worker processes, which is enough to trigger the race today.
- Ran six independent QC rounds and acted on every finding. Four on the idempotency change, two on the wallet change.
- Cleaned up both branches and the temporary working directories.

## Next steps

- Fix `send_low_balance_email`, which claims in its own documentation that its caller limits it to one message per day. No caller does, so a batch of fifty finished jobs sends one customer fifty emails. The two auto-reload notification emails have the same problem. The fix is now cheap because this session added the exact pattern it needs.
- Decide whether to make the auto-reload monthly spending cap count charges when they are sent rather than when Stripe confirms them. As it stands the cap can be exceeded if confirmations stop arriving. Requires a second database migration.
- Optionally add a test covering the general rule that nothing may exit early between claiming a charge and making it. The test that exists only covers one specific case.

## Notes

- The durable lesson from this session is not either bug. Across all six review rounds the **code** was proven correct by testing at the first round and never needed changing again, while the **written explanations** around it were wrong in every single round, including in the commits whose only purpose was correcting the previous round's wrong explanations. Four generations deep. One commit added a note forbidding a specific false claim and left a copy of that exact claim twelve lines below it.
- Why this happens: a wrong comment has no failing test. Testing proves the code and nothing at all checks the sentence beside it, which makes written explanation the only thing here with no safety net, and the first thing anyone reads during an incident.
- Several numbers were asserted rather than measured, including inside a commit whose own comment says to measure them. Worth treating any count, distance or "Nth time" in a comment as unverified until re-run.
- Main moved twice during the session and gained 129 tests between the branch point and the merge, so every test count was re-measured rather than quoted from earlier in the day.
- The `.planning/` folder in this repo contains a single file from April and is stale. This project is tracked by handoff notes in `docs/`, of which there are now 33.
