## SQL function tests

Plain psql test scripts for migration-level invariants. These complement
the Python pytest suites in `tests/` by exercising stored functions and
ledger constraints directly against Postgres.

### Running

Each file is a single transaction that ends in `ROLLBACK`, so nothing is
persisted. Run against a database where the migration under test has
already been applied:

```bash
psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -1 \
     -f tests/sql/test_0017_wallet.sql
```

Or via the Supabase CLI against a local stack:

```bash
supabase db start
supabase db query --file tests/sql/test_0017_wallet.sql
```

A passing run prints a numbered `NOTICE` line per test block. A failing
assertion aborts the whole run because of `-v ON_ERROR_STOP=1`.

### Conventions

- One `.sql` file per migration: `test_<NNNN>_<name>.sql`.
- Wrap the whole file in `BEGIN; ... ROLLBACK;`.
- Each test block is a `DO $$ ... $$` with `RAISE EXCEPTION` on failure
  and a final `RAISE NOTICE 'test N: <one-line summary>'` on success.
- Insert fixture rows under predictable UUIDs or via `gen_random_uuid()`
  stored in a temporary table; clean-up happens automatically at
  `ROLLBACK` time.
- Reference `auth.users` columns with their minimum NOT NULL set
  (`id`, `email`, `instance_id`, `aud`, `role`) so the fixture works on
  both local Supabase and managed projects.
