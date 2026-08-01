"""Behaviour tests for shared.target_results.aggregate_target_candidates.

The subject is the fan in that turns one protein target's many runs into ONE
ranked table: campaign children read per campaign, standalone tool_jobs read
separately, refolds counted and dropped, everything handed to shared.ranking.

THE FAKES BELOW ARE PART OF THE SUBJECT, NOT SCAFFOLDING.

Every read this module makes goes through a PostgREST query builder, and the
production aggregator is forbidden from wrapping that read in a bare
``except Exception`` that returns an empty envelope. That prohibition only
buys anything if the fake models the builder faithfully, because the two
failure modes it protects against are both invisible otherwise:

* A builder method the fake does not implement raises AttributeError inside
  the read. Under the campaign aggregator's idiom
  (shared/compute_campaigns.py:1230-1237) that becomes an empty table and a
  GREEN suite. Here it becomes ``partial=True``, which is asserted.
* A filter the fake records but never applies is a filter the production code
  can omit entirely without any test noticing. ``.is_("campaign_id", "null")``
  is the sharp one: ``_dispatch_chunk`` stamps ``target_id`` on every campaign
  sub job, so without that filter every campaign child is read a second time
  as a standalone job and the table doubles.

So the fake applies its filters, applies the column projection, and enforces
the same max_rows clamp the real backend does. Precedent for that stance is
tests/test_data_retention.py:168-173, which says the same thing about the same
method. ``class _FakeQuery`` in tests/test_campaign_results.py is the weaker
precedent: it implements only select/eq/order/range and would raise on
``.is_()``. (Cited by name, not by line: that file is under edit on this
branch and its line numbers move.)

THE SAME DOCTRINE APPLIES TO THE TWO OWNERSHIP GATES, which are not queries
this module issues but calls it makes. Every read here runs through the
service-role client, which bypasses RLS (shared/credits.py:51-72 against the
``auth.uid() = user_id`` policy at 0005_tool_jobs.sql:59), so the ``user_id``
keyword IS the boundary. A stub that swallowed ``user_id`` in ``**kw`` would
make a cross tenant read untestable by construction, so ``_stub_campaign_lister``
models the real gate at shared/compute_campaigns.py:1063-1065 instead, and
foreign rows are seeded on the target so an omitted filter has something to
leak.

AND THE OWNERSHIP GATE HAS THREE ANSWERS, NOT TWO. ``shared.targets.get_target``
returns None for a target that is absent, for one that is another tenant's, AND
for a read that failed (it swallows every exception and returns None when it has
no client of its own). Only a SUCCESSFUL read may 404 someone, so the aggregator
re-asks through its own client, and this file models both halves: the stub
answers ownership from an in-memory set, and ``_FakeClient`` serves
``design_targets`` so the re-ask has a backend that can succeed, come back
empty, or fail. Testing the failure through the stub alone would certify a
state production cannot reach, which is the previous shape of this file's
no-client test.

``.not_`` is deliberately absent from the fake. The aggregator never calls it,
and a fake method with no caller is a claim about a surface nothing exercises.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

# Dotted rather than ``from shared import target_results``: the from-form
# reports a missing MODULE and a missing ATTRIBUTE with the same
# "cannot import name" text, so it cannot tell "not written yet" from
# "renamed". This form raises ModuleNotFoundError naming the module.
import shared.target_results as target_results


pytestmark = pytest.mark.usefixtures("isolate_supabase")


OWNER = "u-owner"
ATTACKER = "u-attacker"


# PostgREST clamps EVERY response to the project's max_rows, which
# supabase/config.toml sets to 1000, while one target can accumulate far more
# rows than that across its runs. The fake enforces the same clamp so a read
# that forgets to page truncates HERE exactly as it does in production, which
# is what makes a paging assertion mean something rather than decorate.
# test_standalone_read_pages_past_max_rows is that assertion.
_FAKE_MAX_ROWS = 1000


class _FakeResult:
    """What the supabase client returns: an object carrying ``.data``."""

    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """A PostgREST query builder over one in memory table.

    Public surface, matching what the aggregator and
    ``iter_succeeded_children`` actually call:

        select(*cols) / eq(col, val) / is_(col, val) / order(col, desc=False)
        / range(start, end) / limit(n) / execute()

    Every one of those is APPLIED in :meth:`execute`, never merely recorded.
    ``is_`` models only ``"null"``; any other argument raises rather than
    passing silently, because a filter the fake waves through is a filter the
    production code is free to get wrong.

    The recorded filters stay readable after execution so a test can assert on
    the QUERY as well as on the rows. Both are needed: a row-set assertion
    alone passes whenever some other predicate coincidentally excludes the
    rows a missing predicate would have leaked.
    """

    def __init__(self, rows, table_name, fail_when=None):
        self._rows = rows
        self._table = table_name
        self._fail_when = fail_when
        self._columns = None
        self._eq = []
        self._null = []
        self._order = None
        self._desc = False
        self._range = None
        self._limit = None
        self.executed = False

    # -- builder ------------------------------------------------------------

    def select(self, *cols, **kw):
        """Record the projection. Applied in execute().

        Projection is modelled because the standalone read has to ask for
        ``preset`` explicitly (tool_jobs.preset is a real column,
        supabase/migrations/0005_tool_jobs.sql:29) and a fake that returned
        whole seeded rows regardless would let an aggregator that omitted it
        pass. Omitting it is not cosmetic: every standalone row would then
        carry preset None, land in a different (tool, preset) cohort from the
        same tool's campaign rows, and split one population into two that each
        overstate their percentile. Pinned by
        test_one_tools_campaign_and_standalone_rows_share_one_cohort.

        Unmodelled keyword arguments raise for the same reason ``is_`` does.
        ``count="exact"`` in particular would otherwise be accepted here and
        then surface as an AttributeError on ``_FakeResult.count``, which under
        the log-and-set-partial rule reads as a transport failure rather than
        as a gap in the fake.
        """
        if kw:
            raise AssertionError(
                f"_FakeQuery.select does not model kwargs {sorted(kw)}"
            )
        names = []
        for col in cols:
            names.extend(part.strip() for part in str(col).split(","))
        self._columns = [n for n in names if n]
        return self

    def eq(self, col, val):
        """PostgREST ``.eq``, applied in execute().

        ``None`` raises. PostgREST serialises it as ``col=eq.None``, which
        against a uuid column is a 22P02 cast error and never a NULL match,
        while this fake's ``str()`` comparison would happily match every row
        whose column is NULL and so accept ``.eq("campaign_id", None)`` as a
        synonym for ``.is_("campaign_id", "null")``. That is precisely the one
        token slip test_campaign_children_are_not_double_counted_as_standalone_jobs
        exists to catch.
        """
        if val is None:
            raise AssertionError(
                f"_FakeQuery.eq does not model NULL on {col!r}; "
                "PostgREST needs .is_(col, 'null')"
            )
        self._eq.append((col, val))
        return self

    def is_(self, col, val):
        """PostgREST ``.is_(col, "null")``, applied in execute().

        This is the method tests/test_campaign_results.py's fake does not
        implement at all. Recording it without applying it would be no better:
        the double count it guards against would still not be reproducible.
        """
        if str(val).strip().lower() != "null":
            raise AssertionError(
                f"_FakeQuery.is_ models only 'null', got {val!r} on {col!r}"
            )
        self._null.append(col)
        return self

    def order(self, col, **kw):
        self._order = col
        self._desc = bool(kw.get("desc", False))
        return self

    def range(self, start, end):
        self._range = (int(start), int(end))
        return self

    def limit(self, n):
        """PostgREST ``.limit``, applied in execute() and clamped like the real
        one.

        Modelled because the ownership re-ask uses it and for no other reason:
        that read wants existence, not rows, so it asks for one. PostgREST
        clamps ``.limit`` to ``max_rows`` exactly as it clamps a bare select,
        which is why no PAGED read in this module may use it.
        """
        self._limit = int(n)
        return self

    # -- execution ----------------------------------------------------------

    def execute(self):
        self.executed = True
        if self._fail_when is not None and self._fail_when(self):
            raise RuntimeError("backend read failed")

        matched = list(self._rows)

        # Filters run over the FULL row, before the projection, because
        # PostgREST filters on columns the select never returns.
        for col, val in self._eq:
            matched = [r for r in matched if str(r.get(col)) == str(val)]
        for col in self._null:
            matched = [r for r in matched if r.get(col) is None]

        if self._order is not None:
            matched.sort(
                key=lambda r: str(r.get(self._order)), reverse=self._desc,
            )

        if self._range is not None:
            start, end = self._range
            matched = matched[start:end + 1]
        if self._limit is not None:
            matched = matched[:self._limit]
        matched = matched[:_FAKE_MAX_ROWS]

        if self._columns and "*" not in self._columns:
            matched = [
                {k: v for k, v in r.items() if k in self._columns}
                for r in matched
            ]
        return _FakeResult(matched)


class _FakeClient:
    """Serves ``tool_jobs`` and ``design_targets``, and keeps every query.

    Any other table raises. An unmodelled table that returned an empty list
    would read as "this target has no rows", which is the exact shape of the
    bug this file exists to make impossible.

    ``design_targets`` carries ONE column pair, id and user_id, because the
    only read the aggregator issues against it asks whether an owner scoped row
    exists. It is served here rather than left to the ``get_target`` stub
    because the aggregator re-asks the ownership question through THIS client
    when that call answers None: the stub can say "no row", but only a client
    can say "the read failed", and telling those two apart is the difference
    between 404ing a stranger and 404ing the owner during an outage.

    ``fail_when`` is a predicate over the query, so a test can fail ONE read
    (the standalone page, one campaign's children, the ownership re-ask) and
    assert what survives. ``query._table`` is what a predicate discriminates
    on.
    """

    def __init__(self, tool_jobs, *, targets=(), fail_when=None):
        self._tables = {
            "tool_jobs": list(tool_jobs),
            "design_targets": [
                {"id": str(t), "user_id": str(u)} for t, u in targets
            ],
        }
        self._fail_when = fail_when
        self.queries = []

    def table(self, name):
        if name not in self._tables:
            raise AssertionError(f"_FakeClient does not model table {name!r}")
        query = _FakeQuery(
            self._tables[name], name, fail_when=self._fail_when,
        )
        self.queries.append(query)
        return query


class _StubCampaign:
    """The attributes the aggregator reads off a ComputeCampaign.

    Not a real ComputeCampaign: that dataclass demands a dozen money and
    sizing fields none of which this fan in touches, and constructing them
    would suggest they matter here. ``user_id`` and ``target_id`` are carried
    so the lister stub can apply the real function's own filters rather than
    returning whatever it was handed.
    """

    def __init__(self, id, tool, preset, status="completed",
                 target_id="T", user_id=OWNER):
        self.id = id
        self.tool = tool
        self.preset = preset
        self.status = status
        self.target_id = target_id
        self.user_id = user_id


class _StubTarget:
    """Stands in for the DesignTarget the ownership gate resolves."""

    def __init__(self, id, user_id):
        self.id = id
        self.user_id = user_id


def _job_row(job_id, *, tool, preset, target_id, user_id=OWNER,
             campaign_id=None, chunk_index=None, attempt=1,
             inputs=None, candidates=(), status="succeeded"):
    """One tool_jobs row as the table actually stores it.

    ``inputs`` passes through UNCHANGED. Coercing it with ``dict(inputs or {})``
    would silently rewrite the two shapes the refold discriminator has to
    survive: a jsonb scalar (a bare string is a legal value of a
    ``jsonb NOT NULL`` column) raised inside the fixture instead of reaching
    the code, and an explicit None was turned into ``{}`` so the production
    guard was never exercised.

    ``user_id`` defaults to the owner but is a parameter, so a test can seed
    another tenant's row on this target. Without that, a filtered read and an
    unfiltered read return the same set and no tenancy assertion means
    anything.

    ``status`` defaults to succeeded and is a parameter for the same reason: a
    fixture in which every row is succeeded gives ``.eq("status", "succeeded")``
    nothing to exclude, so the filter can be deleted with no test noticing.

    A non-Mapping entry in ``candidates`` passes through as itself rather than
    through ``dict()``, which would raise in the fixture. ``candidate_records``
    returns whatever the jsonb held, so a malformed record is a shape the
    aggregator has to survive, and it is also the only way to test that
    ``_source_index`` counts the records the loop SKIPPED.
    """
    return {
        "id": job_id,
        "user_id": user_id,
        "tool": tool,
        "preset": preset,
        "status": status,
        "target_id": target_id,
        "campaign_id": campaign_id,
        "chunk_index": chunk_index,
        "attempt": attempt,
        "inputs": inputs,
        "result": {"candidates": [
            dict(c) if isinstance(c, Mapping) else c for c in candidates
        ]},
    }


def _install(monkeypatch, *, rows=(), campaigns=(), targets=(("T", OWNER),),
             fail_when=None, campaign_lister=None, get_target=None):
    """Patch the three module attributes the aggregator reads through.

    ``targets`` seeds BOTH the ``get_target`` stub and the client's
    ``design_targets`` table, so the gate and the re-ask agree unless a test
    deliberately splits them (``get_target`` overrides the stub, which is how
    "the gate failed but the row is there" is expressed).

    Returns the ``_FakeClient`` so a test can assert on the queries issued and
    the recorded ``list_campaigns_for_target`` kwargs.
    """
    client = _FakeClient(rows, targets=targets, fail_when=fail_when)
    owned = {(str(t), str(u)) for t, u in targets}
    calls = []

    def _stub_get_target(target_id, *, user_id=None):
        """Models shared/targets.py:363-386: owner scoped when user_id given.

        Returns None for a target that does not exist AND for one that is not
        this user's, which is why one sentinel covers both. The real function
        has a THIRD None: it swallows every exception and also answers None
        when it could not build a client. A test that needs that one passes its
        own ``get_target`` returning None while the seeded row stays in place.
        """
        if user_id is not None and (str(target_id), str(user_id)) not in owned:
            return None
        if not any(t == str(target_id) for t, _ in owned):
            return None
        return _StubTarget(target_id, user_id)

    def _stub_campaign_lister(target_id, *, user_id=None, include_drafts=False):
        """Models shared/compute_campaigns.py:1063-1069.

        The owner filter is applied ONLY when user_id is given, exactly as the
        real function does, so an aggregator that omits the keyword leaks every
        tenant's campaigns on that target here too.
        """
        calls.append({
            "target_id": target_id,
            "user_id": user_id,
            "include_drafts": include_drafts,
        })
        out = [c for c in campaigns if c.target_id == target_id]
        if user_id is not None:
            out = [c for c in out if c.user_id == user_id]
        if not include_drafts:
            out = [c for c in out if c.status != "draft"]
        return out

    monkeypatch.setattr(
        target_results, "get_target", get_target or _stub_get_target,
    )
    monkeypatch.setattr(
        target_results, "list_campaigns_for_target",
        campaign_lister or _stub_campaign_lister,
    )
    monkeypatch.setattr(target_results, "get_service_client", lambda: client)
    client.campaign_list_calls = calls
    return client


def _standalone_queries(client):
    """The executed queries that are the standalone read.

    Identified by the target_id predicate, which only that read carries;
    ``iter_succeeded_children`` filters on campaign_id and status alone.
    """
    return [
        q for q in client.queries
        if q.executed and q._table == "tool_jobs"
        and any(col == "target_id" for col, _ in q._eq)
    ]


def _tool_jobs_queries(client):
    return [q for q in client.queries if q._table == "tool_jobs"]


def _campaign_queries(client, campaign_id):
    return [
        q for q in client.queries
        if q.executed and ("campaign_id", campaign_id) in q._eq
    ]


def _keys(agg):
    return sorted(c.get("pdb_key") for c in agg["candidates"])


# ---------------------------------------------------------------------------
# The double count, and the server side filter that prevents it
# ---------------------------------------------------------------------------

def test_campaign_children_are_not_double_counted_as_standalone_jobs(
    monkeypatch,
):
    """A campaign child carries target_id too, so it is reachable both ways.

    ``_dispatch_chunk`` stamps the parent's ``target_id`` on every sub job it
    creates, so a query for "succeeded tool_jobs on this target" returns every
    campaign child alongside the genuinely standalone runs. Read that way the
    campaign designs arrive twice: once through the per campaign fan in and
    once more as standalone rows.

    The assertions are on the CANDIDATE SET as well as the job count, because
    a job count cannot tell a doubled table from a correct one: an
    implementation that never read the campaign side at all also reports
    standalone_jobs == 1.
    """
    target_id = "T"
    rows = [
        _job_row(
            "child-1", tool="bindcraft", preset="default",
            target_id=target_id, campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "d0.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "solo-1", tool="bindcraft", preset="default",
            target_id=target_id, campaign_id=None,
            candidates=[{"pdb_key": "d1.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    campaign = _StubCampaign("C", tool="bindcraft", preset="default")
    _install(monkeypatch, rows=rows, campaigns=[campaign])

    agg = target_results.aggregate_target_candidates(
        target_id, user_id=OWNER,
    )

    # The message carries partial so this half cannot fail pointing nowhere,
    # whichever assertion trips first.
    assert agg["standalone_jobs"] == 1, (
        f"partial={agg.get('partial')!r} tools={agg.get('tools')!r}"
    )
    # THE LOAD BEARING HALF. A fake that did not model .is_() makes the query
    # builder raise, the read fails, and partial goes true. Without this line
    # that failure reads "expected 1, got 0" and names nothing, which is the
    # hazard this file exists to prevent reproduced inside its own fix.
    assert agg["partial"] is False
    # Each design exactly once, from exactly one side of the fan in.
    assert agg["total"] == 2
    assert _keys(agg) == ["d0.pdb", "d1.pdb"]


def test_standalone_read_filters_campaign_id_server_side(monkeypatch):
    """The campaign_id filter is issued to the backend, not applied in Python.

    A client side equivalent produces the same rows and the same counts, so
    only the QUERY distinguishes them. It has to be server side: every campaign
    child's full result JSON would otherwise cross the wire a second time, and
    at the realistic worst case for this page that is tens of megabytes
    re-fetched on every load.

    The projection assertion is the other half. A read that asked for
    ``campaign_id`` would hand a future edit the column it needs to filter
    locally; not projecting it makes the local filter unwriteable.
    """
    rows = [
        _job_row(
            "child-1", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "d0.pdb", "scores": {"ipTM": 0.91}}],
        ),
    ]
    campaign = _StubCampaign("C", tool="bindcraft", preset="default")
    client = _install(monkeypatch, rows=rows, campaigns=[campaign])

    target_results.aggregate_target_candidates("T", user_id=OWNER)

    standalone = _standalone_queries(client)
    assert standalone, "no standalone read was issued at all"
    for query in standalone:
        assert "campaign_id" in query._null, (
            "standalone read did not issue .is_('campaign_id', 'null')"
        )
        assert "campaign_id" not in (query._columns or []), (
            "standalone read projected campaign_id, which is only useful for "
            "filtering in Python"
        )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def test_foreign_job_pointed_at_my_target_is_not_read(monkeypatch):
    """``.eq("user_id")`` on the standalone read is the whole boundary.

    ``tool_jobs.target_id`` is a plain nullable column with no parentage
    predicate, so owning the target does not imply owning a row that points at
    it, and the service role client bypasses the RLS policy that would
    otherwise catch this. Both the returned rows and the issued predicate are
    asserted: a row set assertion alone would pass in any fixture where some
    other filter happened to exclude the foreign row.
    """
    rows = [
        _job_row(
            "mine-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "mine.pdb", "scores": {"ipTM": 0.70}}],
        ),
        _job_row(
            "theirs-1", tool="bindcraft", preset="default", target_id="T",
            user_id=ATTACKER,
            candidates=[{"pdb_key": "secret.pdb", "scores": {"ipTM": 0.99}}],
        ),
    ]
    client = _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["standalone_jobs"] == 1, f"partial={agg['partial']!r}"
    assert _keys(agg) == ["mine.pdb"]
    for query in _standalone_queries(client):
        assert ("user_id", OWNER) in query._eq, (
            "standalone read issued no user_id predicate"
        )


def test_foreign_campaign_on_my_target_is_not_read(monkeypatch):
    """``user_id`` must reach ``list_campaigns_for_target``.

    That function applies its owner filter only when the keyword is given
    (shared/compute_campaigns.py:1063-1065), and ``iter_succeeded_children``
    filters on campaign_id and status alone, so the campaign side's entire
    tenancy safety is inherited from this one keyword. A second tenant's
    campaign on a shared target id would otherwise deliver its designs, its
    sequences and its job ids into this user's table and export.
    """
    rows = [
        _job_row(
            "mine-child", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-mine", chunk_index=0,
            candidates=[{"pdb_key": "mine.pdb", "scores": {"ipTM": 0.70}}],
        ),
        _job_row(
            "theirs-child", tool="boltzgen", preset="default", target_id="T",
            user_id=ATTACKER, campaign_id="C-theirs", chunk_index=0,
            candidates=[{"pdb_key": "secret.pdb", "scores": {"ipTM": 0.99}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C-mine", tool="bindcraft", preset="default"),
        _StubCampaign(
            "C-theirs", tool="boltzgen", preset="default", user_id=ATTACKER,
        ),
    ]
    client = _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert _keys(agg) == ["mine.pdb"], f"partial={agg['partial']!r}"
    assert agg["tools"] == ["bindcraft"]
    assert [c.id for c in agg["campaigns"]] == ["C-mine"]
    assert client.campaign_list_calls == [
        {"target_id": "T", "user_id": OWNER, "include_drafts": False},
    ]


def test_a_foreign_users_sub_job_under_my_campaign_is_dropped(monkeypatch):
    """The child read has no tenancy predicate of its own, so it is checked.

    ``iter_succeeded_children`` filters on campaign_id and status alone with
    the service role client, and the schema relates ``tool_jobs.user_id`` to
    ``compute_campaigns.user_id`` by convention only
    (0034_compute_campaigns.sql:98-120 adds the FK to the campaign and nothing
    tying the two user_id columns). No violating row is reachable through the
    application today; a re-parent, an admin clone, a direct write or a second
    future writer of ``tool_jobs.campaign_id`` would make one, and it would
    land that tenant's design, its pdb_key and its job id in this table, its
    CSV, and the ZIP that pulls its structure bytes out of storage.

    Both halves are asserted. The row set alone would pass under a projection
    that never returned ``user_id``, because then nothing could be compared and
    everything would be kept.
    """
    rows = [
        _job_row(
            "mine-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "mine.pdb", "scores": {"ipTM": 0.70}}],
        ),
        _job_row(
            "theirs-1", tool="bindcraft", preset="default", target_id="T",
            user_id=ATTACKER, campaign_id="C", chunk_index=1,
            candidates=[{"pdb_key": "secret.pdb", "scores": {"ipTM": 0.99}}],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    client = _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert _keys(agg) == ["mine.pdb"], f"partial={agg['partial']!r}"
    assert agg["total"] == 1
    for query in _campaign_queries(client, "C"):
        assert "user_id" in (query._columns or []), (
            "the child read did not project user_id, so the invariant tying a "
            "sub-job to its campaign's owner could not be checked at all"
        )


def test_foreign_target_is_not_readable(monkeypatch):
    """ok=False for a target that is not this user's, and for one that does
    not exist.

    One sentinel covers both because the owner scoped fetch cannot tell them
    apart, and the page must not distinguish them either: confirming that a
    target id exists but belongs to someone else is itself a disclosure.

    No design is read, which is the point of gating first. The only query
    either call may issue is the ownership re-ask, and it must carry the
    ``user_id`` predicate: unscoped it would answer "this id exists" for
    another tenant's target and turn the 404 into an existence oracle.
    """
    rows = [
        _job_row(
            "theirs-1", tool="bindcraft", preset="default", target_id="T",
            user_id=ATTACKER,
            candidates=[{"pdb_key": "secret.pdb", "scores": {"ipTM": 0.99}}],
        ),
    ]
    client = _install(monkeypatch, rows=rows, targets=(("T", ATTACKER),))

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)
    assert agg["ok"] is False
    assert agg["candidates"] == []
    assert _tool_jobs_queries(client) == []

    missing = target_results.aggregate_target_candidates(
        "no-such-target", user_id=OWNER,
    )
    assert missing["ok"] is False
    assert _tool_jobs_queries(client) == []
    for query in client.queries:
        assert query._table == "design_targets"
        assert ("user_id", OWNER) in query._eq, (
            "the ownership re-ask was not owner scoped"
        )


def test_empty_owned_target_is_ok_true_not_404(monkeypatch):
    """Yours and empty is ok=True with tools == [], never the 404 sentinel.

    ``_campaign_export`` gates on ``agg.get("tool") is None``
    (blueprints/campaigns.py:687-688). Reused here as ``ok = bool(tools)`` that
    idiom 404s a user who has just uploaded a target and launched nothing, or
    whose runs have not produced a design yet. The empty state and the empty
    export are correct answers; a 404 on your own object is not.

    Paired with test_foreign_target_is_not_readable on purpose: a hardcoded
    ``ok = True`` passes this test alone, and ``ok = bool(tools)`` passes that
    one alone. Only both together pin the sentinel.
    """
    _install(monkeypatch, rows=[], campaigns=[])

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["tools"] == []
    assert agg["candidates"] == []
    assert agg["total"] == 0
    assert agg["partial"] is False
    assert agg["multi_tool"] is False


def test_user_id_is_required_and_must_not_be_falsy(monkeypatch):
    """A falsy user_id raises instead of reaching the reads.

    The two falsy values fail differently and neither fails usefully: None
    makes ``list_campaigns_for_target`` skip its owner filter and return every
    tenant's campaigns, while "" reaches PostgREST as ``user_id=eq.`` against a
    uuid column, which errors on the standalone read and is swallowed into an
    empty list on the campaign one. Normalising both to a raise means the
    boundary cannot be crossed by a caller that merely forgot to resolve a
    session.
    """
    _install(monkeypatch, rows=[])
    for bad in (None, ""):
        with pytest.raises(ValueError):
            target_results.aggregate_target_candidates("T", user_id=bad)


# ---------------------------------------------------------------------------
# Refolds
# ---------------------------------------------------------------------------

def test_refold_jobs_are_not_ranked_as_designs(monkeypatch):
    """A refold is a re-measurement of a design that is already a row.

    ``_spawn_refold_job`` stamps the source job's ``target_id``
    (blueprints/jobs.py:424-431) and refolds carry no campaign_id, so they land
    squarely in the standalone population. ``candidate_records`` reads
    ``designs[]`` (shared/jobs.py:109-112), which is exactly the shape boltz2
    and esmfold emit, so without the filter they merge in SILENTLY: the
    molecule is counted twice and the second copy is filed under the REFOLDER's
    tool, so one design becomes two rows attributed to two tools.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "design.pdb", "scores": {"ipTM": 0.72}}],
        ),
        _job_row(
            "refold-1", tool="boltz2", preset="standalone", target_id="T",
            inputs={"_refold_of_job_id": "solo-1"},
            candidates=[{"pdb_key": "design.pdb", "scores": {"ipTM": 0.88}}],
        ),
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["refold_jobs"] == 1
    assert agg["standalone_jobs"] == 1
    assert agg["total"] == 1
    assert agg["tools"] == ["bindcraft"], (
        "the refold was ranked and filed under the refolder's tool"
    )


def test_a_non_dict_inputs_row_is_kept_not_crashed(monkeypatch):
    """``tool_jobs.inputs`` is jsonb NOT NULL, so a scalar is a legal value.

    ``"legacy".get`` is an AttributeError, and raised inside the standalone
    loop it would abort the WHOLE page under the log-and-set-partial rule: one
    malformed row would hide every standalone design behind a partial banner.
    A non-dict cannot carry the refold key, so it is not a refold.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            inputs="legacy",
            candidates=[{"pdb_key": "d.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["partial"] is False
    assert agg["standalone_jobs"] == 1
    assert agg["refold_jobs"] == 0
    assert _keys(agg) == ["d.pdb"]


# ---------------------------------------------------------------------------
# Retry dedupe, scoped per campaign
# ---------------------------------------------------------------------------

def test_chunk_zero_of_two_campaigns_both_survive(monkeypatch):
    """``chunk_index`` is unique only WITHIN a campaign.

    The unique index is ``(campaign_id, chunk_index, attempt)``
    (0034_compute_campaigns.sql:133-134), and every campaign has a chunk 0.
    Merge several campaigns through one dedupe map and chunk 0 of bindcraft
    evicts chunk 0 of boltzgen, silently deleting a whole sub job of designs
    the user paid for, with nothing in the envelope to signal the loss.
    """
    rows = [
        _job_row(
            "bc-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-bc", chunk_index=0,
            candidates=[{"pdb_key": "bc.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "bg-0", tool="boltzgen", preset="default", target_id="T",
            campaign_id="C-bg", chunk_index=0,
            candidates=[{"pdb_key": "bg.pdb", "scores": {"ipTM": 0.80}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C-bc", tool="bindcraft", preset="default"),
        _StubCampaign("C-bg", tool="boltzgen", preset="default"),
    ]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["total"] == 2, f"partial={agg['partial']!r}"
    assert _keys(agg) == ["bc.pdb", "bg.pdb"]
    assert agg["tools"] == ["bindcraft", "boltzgen"]
    assert agg["multi_tool"] is True


def test_highest_attempt_wins_within_one_campaign(monkeypatch):
    """Two succeeded attempts of one chunk are legal rows, not duplicates.

    The unique index admits them, ``iter_succeeded_children`` yields both, and
    only the later attempt's designs were delivered. Counting both would report
    a chunk's designs twice and rank a superseded attempt's structures beside
    the ones that replaced them.
    """
    rows = [
        _job_row(
            "c0a1", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0, attempt=1,
            candidates=[{"pdb_key": "a.pdb", "scores": {"ipTM": 0.40}}],
        ),
        _job_row(
            "c0a2", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0, attempt=2,
            candidates=[{"pdb_key": "b.pdb", "scores": {"ipTM": 0.95}}],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["total"] == 1, f"partial={agg['partial']!r}"
    assert _keys(agg) == ["b.pdb"]


def test_each_campaign_is_read_on_its_own_query_not_one_in_list(monkeypatch):
    """The campaign fan in is a LOOP. Do not "optimise" it to ``.in_()``.

    Three reasons, none of them visible in a row set assertion, which is why
    this test asserts on the queries instead. ``_MAX_CHILD_PAGES``
    (shared/compute_campaigns.py:1137) is derived PER CAMPAIGN and is exactly
    right applied that way; widened to an IN list it truncates at 101k rows
    with only a logger.error. One pathological 50k child campaign would exhaust
    a page budget shared with every campaign after it. And a single query
    cannot carry campaign_id into a per campaign dedupe map without widening
    ``iter_succeeded_children``'s select, so in practice it collapses to one
    shared map and the chunk collision above.
    """
    rows = [
        _job_row(
            "bc-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-bc", chunk_index=0,
            candidates=[{"pdb_key": "bc.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "bg-0", tool="boltzgen", preset="default", target_id="T",
            campaign_id="C-bg", chunk_index=0,
            candidates=[{"pdb_key": "bg.pdb", "scores": {"ipTM": 0.80}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C-bc", tool="bindcraft", preset="default"),
        _StubCampaign("C-bg", tool="boltzgen", preset="default"),
    ]
    client = _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)
    assert agg["partial"] is False

    for campaign_id in ("C-bc", "C-bg"):
        issued = [
            q for q in client.queries
            if q.executed and (("campaign_id", campaign_id) in q._eq)
        ]
        assert issued, (
            f"campaign {campaign_id} was never read on a query of its own"
        )


# ---------------------------------------------------------------------------
# Provenance stamped on every returned row
# ---------------------------------------------------------------------------

def test_every_row_carries_its_provenance_and_job_index_pairs_are_unique(
    monkeypatch,
):
    """The five ``_source_*`` keys, and the uniqueness ranking depends on.

    ``shared.ranking.canonical_sort_key`` documents ``(job_id, index)``
    uniqueness as a PRECONDITION the aggregation layer must satisfy: it is the
    last pair in the key, so if it collapses, every tie block falls back to
    Python's stable sort and therefore to dict iteration order over the dedupe
    map. The table would then reshuffle equal rows between two loads of a page
    that has no live refresh, and the CSV exported from each would disagree
    about row order.

    The other consumers of these keys are just as concrete:
    ``shared/exports.py`` builds the CSV's tool / campaign_id / source_job
    columns from them and the ZIP resolves each structure through
    ``fetch_bytes(_source_job_id, pdb_key)``, so a dropped stamp is a ZIP with
    no PDBs in it.

    ``_source_index`` counts the records the loop SKIPPED, which is why the
    fixture puts a malformed record between two real ones: the surviving
    indices must be 0 and 2, not 0 and 1, or two rows of the same job could
    collide as soon as one job's records are read in more than one pass.
    """
    rows = [
        _job_row(
            "bc-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-bc", chunk_index=0,
            candidates=[
                {"pdb_key": "a.pdb", "scores": {"ipTM": 0.91}},
                "not-a-record",
                {"pdb_key": "b.pdb", "scores": {"ipTM": 0.60}},
            ],
        ),
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.75}}],
        ),
    ]
    campaigns = [_StubCampaign("C-bc", tool="bindcraft", preset="default")]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)
    by_key = {c["pdb_key"]: c for c in agg["candidates"]}
    assert sorted(by_key) == ["a.pdb", "b.pdb", "s.pdb"], (
        f"partial={agg['partial']!r}"
    )

    for row in agg["candidates"]:
        for key in (
            "_source_tool", "_source_preset", "_source_campaign_id",
            "_source_job_id", "_source_index",
        ):
            assert key in row, f"{row['pdb_key']} carries no {key}"

    assert by_key["a.pdb"]["_source_campaign_id"] == "C-bc"
    assert by_key["a.pdb"]["_source_job_id"] == "bc-0"
    assert by_key["a.pdb"]["_source_preset"] == "default"
    assert by_key["a.pdb"]["_source_tool"] == "bindcraft"
    # The skipped record still consumed index 1.
    assert by_key["a.pdb"]["_source_index"] == 0
    assert by_key["b.pdb"]["_source_index"] == 2
    # A standalone row has no campaign, and says so rather than omitting it.
    assert by_key["s.pdb"]["_source_campaign_id"] is None
    assert by_key["s.pdb"]["_source_job_id"] == "solo-1"

    pairs = [
        (c["_source_job_id"], c["_source_index"]) for c in agg["candidates"]
    ]
    assert len(set(pairs)) == len(pairs), (
        "(job_id, index) collided, so canonical_sort_key is no longer a total "
        "order and equal rows reshuffle between reloads"
    )


def test_campaign_rows_carry_their_sub_job_chunk_and_standalone_rows_do_not(
    monkeypatch,
):
    """``_source_chunk`` is which sub-job produced the design.

    The candidate table renders it as the ``{tool} #{chunk}`` chip, gated on
    the key being not-None and falling through to a job-id chip when it is
    absent, and ``shared/exports.py`` exports it as ``source_chunk`` beside the
    campaign export's own column. Without it a user cannot tell which of a
    run's sub-jobs a design came from, and the target CSV silently loses a
    provenance column the campaign CSV for the same designs carries.

    A standalone job has no chunk, so the key must be ABSENT rather than None:
    a provenance column is omitted from the CSV only when NO row carries it,
    and the template's guard reads the same either way.
    """
    rows = [
        _job_row(
            "c-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "first.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "c-7", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=7,
            candidates=[{"pdb_key": "eighth.pdb", "scores": {"ipTM": 0.60}}],
        ),
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "solo.pdb", "scores": {"ipTM": 0.75}}],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)
    by_key = {c["pdb_key"]: c for c in agg["candidates"]}
    assert sorted(by_key) == ["eighth.pdb", "first.pdb", "solo.pdb"], (
        f"partial={agg['partial']!r}"
    )

    assert by_key["first.pdb"]["_source_chunk"] == 0
    assert by_key["eighth.pdb"]["_source_chunk"] == 7
    assert "_source_chunk" not in by_key["solo.pdb"]


def test_an_aliased_root_metric_is_normalized_on_the_returned_row(monkeypatch):
    """``normalize_candidate`` earns its place through the PAYLOAD, not the sort.

    iggm persists ``n_epitope_contacts`` while its declared primary metric is
    ``epitope_contacts``. Ranking resolves that alias on its own
    (``shared.ranking.resolve_metric`` normalizes before reading a value), so
    dropping the call here would leave the ORDER perfect and empty the Score
    cell and the exported ``epitope_contacts`` column for every iggm design.
    That is the failure this pins, because it is the one no ordering assertion
    can see.
    """
    rows = [
        _job_row(
            "solo-1", tool="iggm", preset="default", target_id="T",
            candidates=[{"pdb_key": "i.pdb", "n_epitope_contacts": 12}],
        ),
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert _keys(agg) == ["i.pdb"], f"partial={agg['partial']!r}"
    assert agg["candidates"][0]["scores"]["epitope_contacts"] == 12


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def test_one_tools_campaign_and_standalone_rows_share_one_cohort(monkeypatch):
    """``preset`` must be projected, or one population becomes two.

    shared.ranking keys cohorts on ``(tool, preset)``. A campaign row carries
    ``campaign.preset``, a non optional string; a standalone row carries
    whatever the select returned. Drop ``preset`` from the projection and every
    standalone row lands in ``(bindcraft, None)`` beside the campaign rows'
    ``(bindcraft, "default")``, halving both denominators and overstating the
    percentile on both sides of the split.
    """
    rows = [
        _job_row(
            "child-1", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[
                {"pdb_key": "c1.pdb", "scores": {"ipTM": 0.91}},
                {"pdb_key": "c2.pdb", "scores": {"ipTM": 0.60}},
            ],
        ),
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s1.pdb", "scores": {"ipTM": 0.75}}],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    stats = agg["per_tool"]["bindcraft"]
    assert list(stats["presets"]) == ["default"], (
        "bindcraft's designs were split across more than one cohort"
    )
    assert all(c["_cohort_n"] == 3 for c in agg["candidates"])


def test_columns_are_empty_multi_tool_and_the_tools_own_when_single(
    monkeypatch,
):
    """``columns`` lets the page degrade to today's single tool table.

    Multi tool it must be empty: no one column set describes rows whose
    metrics are on incompatible scales, and rendering one tool's columns over
    another tool's numbers is the exact claim this table refuses to make.
    """
    solo = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "a.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    _install(monkeypatch, rows=solo)
    single = target_results.aggregate_target_candidates("T", user_id=OWNER)
    assert single["multi_tool"] is False
    assert single["columns"] == [
        "ipTM", "pLDDT", "RMSD", "shape_complementarity", "SAP",
    ]

    both = solo + [
        _job_row(
            "solo-2", tool="rfantibody", preset="default", target_id="T",
            candidates=[{"pdb_key": "b.pdb", "scores": {"ipAE": 3.7}}],
        ),
    ]
    _install(monkeypatch, rows=both)
    multi = target_results.aggregate_target_candidates("T", user_id=OWNER)
    assert multi["multi_tool"] is True
    assert multi["columns"] == []


# ---------------------------------------------------------------------------
# The two passed numbers
# ---------------------------------------------------------------------------

def test_passed_total_and_per_tool_passed_are_separate_predicates(
    monkeypatch,
):
    """Two questions, two predicates. A future unification must break one.

    ``passed_total`` is ``count_passed_candidates``'s per RESULT semantics
    (shared/jobs.py:179-203), so a target total equals the sum of the run pages
    beneath it: one record in this result carries a filter signal, so the
    unsignalled sibling is excluded and the answer is 1.

    ``per_tool[t]["passed"]`` is shared.ranking's per COHORT regime, where a
    record carrying no verdict of its own is not a failure, so the answer is 2.
    They diverge in production after job recovery, which writes filter_status
    only when the streamed partial carried one.
    """
    rows = [
        _job_row(
            "solo-1", tool="pxdesign", preset="default", target_id="T",
            candidates=[
                {"pdb_key": "p.pdb",
                 "scores": {"ipTM": 0.90, "filter_status": "pass"}},
                {"pdb_key": "u.pdb", "scores": {"ipTM": 0.80}},
            ],
        ),
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["passed_total"] == 1
    assert agg["per_tool"]["pxdesign"]["passed"] == 2


def test_passed_total_counts_the_deduped_campaign_side_and_the_standalone_one(
    monkeypatch,
):
    """The rollup covers BOTH sources, and counts each design once.

    Essentially every real target gets its designs from campaigns, so a
    ``passed_total`` accumulated only on the standalone branch reads 0 on the
    page it exists to serve while every run page beneath it shows its real
    count, inverting the invariant that a target total equals the sum of those
    pages.

    The superseded attempt is the second half. Retry siblings are legal rows
    and only the later attempt's designs were delivered, so a rollup that
    counted the dedupe map's losers would report designs the user never
    received. Here attempt 1 carries three passing records that must not
    contribute at all.

    Campaign side: attempt 2 delivers one passing record and one carrying no
    verdict, and per RESULT semantics exclude the unsignalled sibling once any
    record in the same result is signalled, so it contributes 1. Standalone
    side: one pass and one fail, so it contributes 1. Total 2. The cohort
    regime answers 3 for the same rows, because unjudged is not failed there;
    the two predicates are pinned independently and must stay that way.
    """
    rows = [
        _job_row(
            "c0a1", tool="pxdesign", preset="default", target_id="T",
            campaign_id="C", chunk_index=0, attempt=1,
            candidates=[
                {"pdb_key": f"old{i}.pdb",
                 "scores": {"ipTM": 0.5, "filter_status": "pass"}}
                for i in range(3)
            ],
        ),
        _job_row(
            "c0a2", tool="pxdesign", preset="default", target_id="T",
            campaign_id="C", chunk_index=0, attempt=2,
            candidates=[
                {"pdb_key": "new-pass.pdb",
                 "scores": {"ipTM": 0.90, "filter_status": "pass"}},
                {"pdb_key": "new-unjudged.pdb", "scores": {"ipTM": 0.80}},
            ],
        ),
        _job_row(
            "solo-1", tool="pxdesign", preset="default", target_id="T",
            candidates=[
                {"pdb_key": "solo-pass.pdb",
                 "scores": {"ipTM": 0.70, "filter_status": "pass"}},
                {"pdb_key": "solo-fail.pdb",
                 "scores": {"ipTM": 0.60, "filter_status": "fail"}},
            ],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="pxdesign", preset="default")]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["total"] == 4, f"partial={agg['partial']!r}"
    assert agg["passed_total"] == 2
    assert agg["per_tool"]["pxdesign"]["passed"] == 3


# ---------------------------------------------------------------------------
# Failure disclosure
# ---------------------------------------------------------------------------

def test_partial_is_true_when_the_standalone_read_fails(monkeypatch):
    """A failed read is disclosed, never served as an honest looking empty.

    This is the idiom shared/compute_campaigns.py:1230-1237 gets wrong: a bare
    except returning an empty envelope turns a transport failure, or a builder
    method the fake does not model, into a complete looking table with a green
    suite. The campaign side's rows must still arrive, or the page loses more
    than the failure cost.
    """
    rows = [
        _job_row(
            "child-1", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "c.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    _install(
        monkeypatch, rows=rows, campaigns=campaigns,
        fail_when=lambda q: "campaign_id" in q._null,
    )

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["partial"] is True
    assert agg["standalone_jobs"] == 0
    assert _keys(agg) == ["c.pdb"], "the campaign side was lost too"


def test_an_unreadable_owned_target_is_disclosed_not_reported_empty(
    monkeypatch,
):
    """No service client is "we could not look", not "you have no runs".

    Returning the not-found envelope here would 404 the user's own target on a
    credential or transport problem; returning a clean empty one would tell
    them their funded runs produced nothing. ok=True with partial=True is the
    only honest pair.

    THE GATE IS STUBBED TO None ON PURPOSE, and that is what makes this the
    reachable state rather than an invented one. ``shared.targets`` binds the
    same ``get_service_client`` object this module does
    (shared/targets.py:30), so a process with no client has no ``get_target``
    either: it returns None for every target, owned or not. A version of this
    test that let the gate keep succeeding while the client was None was
    certifying a state production cannot produce, and the ORDER of the two
    resolutions, which is the actual fix, was free to be wrong.
    """
    _install(
        monkeypatch, rows=[], get_target=lambda target_id, **kw: None,
    )
    monkeypatch.setattr(target_results, "get_service_client", lambda: None)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["partial"] is True
    assert agg["candidates"] == []


def test_an_unreadable_ownership_read_is_disclosed_not_404(monkeypatch):
    """A FAILED ownership read must not answer "no such target".

    ``shared.targets.get_target`` swallows every exception and returns the
    same None it returns for a target that is absent
    (shared/targets.py:376-386), so a transient backend failure on that one
    read is indistinguishable at the call site from a stranger's target id.
    Served as the not-found sentinel it 404s the owner of a funded target and
    every one of its exports, while ``partial=False`` asserts that nothing
    failed. This repo has had exactly that transient (the Supabase HTTP/2 hang
    on Railway).

    So the None is re-asked through this module's own client, and only a
    successful read may 404 anyone. Here the re-ask fails, and the answer is
    the disclosure pair rather than either lie.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    _install(
        monkeypatch, rows=rows,
        get_target=lambda target_id, **kw: None,
        fail_when=lambda q: q._table == "design_targets",
    )

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True, "an unreadable ownership gate 404s the owner"
    assert agg["partial"] is True
    assert agg["candidates"] == []


def test_a_transient_gate_failure_still_serves_an_owned_targets_designs(
    monkeypatch,
):
    """When the re-ask succeeds, the table is served in full and not flagged.

    Same swallowed failure as above, but the second read gets through. The row
    is there and it is this user's, so ownership is PROVEN rather than
    assumed: the designs come back, and ``partial`` stays False because no
    read this module issued came back short.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    _install(
        monkeypatch, rows=rows, get_target=lambda target_id, **kw: None,
    )

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["partial"] is False
    assert _keys(agg) == ["s.pdb"]


def test_partial_is_true_when_the_campaign_list_read_raises(monkeypatch):
    """The campaign lister raising must not read as "no runs".

    NOTE ON WHAT THIS DOES NOT COVER. ``list_campaigns_for_target`` catches its
    own paging failure and returns the runs read so far
    (shared/compute_campaigns.py:1077-1081) with no channel to say it did, so a
    SHORT campaign list is invisible to ``partial`` and this test cannot reach
    that case. What it pins is the exception path, and the module docstring
    states the limit rather than letting the flag over claim.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]

    def _boom(target_id, **kw):
        raise RuntimeError("campaign list failed")

    _install(monkeypatch, rows=rows, campaign_lister=_boom)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["partial"] is True
    assert _keys(agg) == ["s.pdb"], "the standalone side was lost too"


def test_partial_is_true_when_one_campaigns_children_fail(monkeypatch):
    """One campaign's read failing must not cost the others their designs."""
    rows = [
        _job_row(
            "bc-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-bc", chunk_index=0,
            candidates=[{"pdb_key": "bc.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "bg-0", tool="boltzgen", preset="default", target_id="T",
            campaign_id="C-bg", chunk_index=0,
            candidates=[{"pdb_key": "bg.pdb", "scores": {"ipTM": 0.80}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C-bc", tool="bindcraft", preset="default"),
        _StubCampaign("C-bg", tool="boltzgen", preset="default"),
    ]
    _install(
        monkeypatch, rows=rows, campaigns=campaigns,
        fail_when=lambda q: ("campaign_id", "C-bg") in q._eq,
    )

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["partial"] is True
    assert _keys(agg) == ["bc.pdb"]


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

def test_standalone_read_pages_past_max_rows(monkeypatch):
    """A read that forgets to page truncates at max_rows and says nothing.

    PostgREST clamps every response to 1000 rows and clamps ``.limit()``
    identically, so an unpaged read returns a full looking page and the jobs
    past it are absent from the table, the CSV and the ZIP with
    ``partial=False`` asserting completeness. Ids are zero padded because the
    fake orders by ``str(id)``, exactly as ordering by a text column would.
    """
    rows = [
        _job_row(
            f"job-{i:05d}", tool="bindcraft", preset="default", target_id="T",
            candidates=[
                {"pdb_key": f"d{i:05d}.pdb", "scores": {"ipTM": 0.5}},
            ],
        )
        for i in range(1500)
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["standalone_jobs"] == 1500, f"partial={agg['partial']!r}"
    assert agg["total"] == 1500
    assert agg["partial"] is False
    assert agg["capped"] is True


def test_hitting_the_standalone_page_bound_sets_partial(monkeypatch):
    """A truncated read is disclosed, never served as a complete one.

    The page bound is a runaway guard, not a limit anyone should reach, which
    is exactly why nothing exercises it by accident: reaching it in a fixture
    takes shrinking the two constants. Left unpinned, the line that turns the
    bound into ``partial`` can be deleted and the table, the CSV and the ZIP
    all come back short with ``partial=False`` asserting they are whole.
    """
    rows = [
        _job_row(
            f"job-{i}", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": f"d{i}.pdb", "scores": {"ipTM": 0.5}}],
        )
        for i in range(5)
    ]
    _install(monkeypatch, rows=rows)
    monkeypatch.setattr(target_results, "_STANDALONE_PAGE_SIZE", 2)
    monkeypatch.setattr(target_results, "_MAX_STANDALONE_PAGES", 1)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["standalone_jobs"] == 2
    assert agg["partial"] is True


def test_a_failed_standalone_page_keeps_the_pages_already_read(monkeypatch):
    """One failing page costs that page, not the read.

    The campaign loop already contains a failure to the campaign it happened
    on; the standalone side owes the same containment. Discarding the pages
    already in memory turns a single failed request at offset 1000 into a
    target whose thousand earlier jobs vanish, and the envelope then reports
    zero standalone runs rather than "some of them".
    """
    rows = [
        _job_row(
            f"job-{i}", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": f"d{i}.pdb", "scores": {"ipTM": 0.5}}],
        )
        for i in range(5)
    ]
    _install(
        monkeypatch, rows=rows,
        fail_when=lambda q: q._range == (2, 3),
    )
    monkeypatch.setattr(target_results, "_STANDALONE_PAGE_SIZE", 2)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["partial"] is True
    assert agg["standalone_jobs"] == 2, (
        "the pages read before the failure were discarded"
    )
    assert _keys(agg) == ["d0.pdb", "d1.pdb"]


def test_a_running_standalone_job_is_not_read(monkeypatch):
    """``.eq("status", "succeeded")`` is a filter, not decoration.

    Nothing but a succeeded job carries candidates, so an unfiltered read
    would report runs contributing designs that have not produced any, and it
    would stop being served by ``tool_jobs_target_status_idx``, which is keyed
    ``(target_id, status)``. Asserted on the predicate as well as the count,
    because a running job's empty result contributes no candidate either way
    and only ``standalone_jobs`` and the query can tell the two apart.
    """
    rows = [
        _job_row(
            "done-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "d.pdb", "scores": {"ipTM": 0.72}}],
        ),
        _job_row(
            "running-1", tool="bindcraft", preset="default", target_id="T",
            status="running",
            candidates=[{"pdb_key": "partial.pdb", "scores": {"ipTM": 0.99}}],
        ),
    ]
    client = _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["standalone_jobs"] == 1, f"partial={agg['partial']!r}"
    assert _keys(agg) == ["d.pdb"]
    for query in _standalone_queries(client):
        assert ("status", "succeeded") in query._eq


# ---------------------------------------------------------------------------
# Provisional
# ---------------------------------------------------------------------------

def _provisional_for(monkeypatch, status):
    rows = [
        _job_row(
            "child-1", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C", chunk_index=0,
            candidates=[{"pdb_key": "c.pdb", "scores": {"ipTM": 0.91}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C", tool="bindcraft", preset="default", status=status),
    ]
    _install(monkeypatch, rows=rows, campaigns=campaigns)
    return target_results.aggregate_target_candidates("T", user_id=OWNER)


def test_a_completed_campaign_is_not_provisional(monkeypatch):
    assert _provisional_for(monkeypatch, "completed")["provisional"] is False


def test_a_non_terminal_campaign_is_provisional(monkeypatch):
    """THE test that decides which status set the check may use.

    ``funded``, ``running`` and ``completing`` are the only statuses on which
    ``CAMPAIGN_TERMINAL_STATUSES`` and ``CAMPAIGN_STATUSES`` disagree for a
    run that can appear on this page: all three are members of the second set,
    so swapping the sets makes a mid-flight campaign read as terminal and the
    page drops its "still producing designs" line while sub-jobs are still
    landing, presenting a moving percentile table as final.

    Every other status agrees between the two sets, which is why a test using
    only ``completed`` and ``paused_insufficient_funds`` left the choice
    unpinned: probed status by status, those two answer identically under
    either set.

    ``running`` is also the ordinary state of the page this module exists to
    serve, so this is not an edge case.
    """
    for status in ("funded", "running", "completing"):
        agg = _provisional_for(monkeypatch, status)
        assert agg["provisional"] is True, status


def test_a_wallet_paused_campaign_is_provisional(monkeypatch):
    """A run stopped waiting on money has not finished producing designs.

    ``paused_insufficient_funds`` is absent from ``CAMPAIGN_STATUSES`` (A38)
    AND from ``CAMPAIGN_TERMINAL_STATUSES``, so it is provisional under either
    set and this case does NOT discriminate between them; the test above is
    what does. What it pins is that a paused run is not treated as finished.
    The page owes it its own sentence rather than "still producing designs",
    which is a template concern and not this module's.
    """
    agg = _provisional_for(monkeypatch, "paused_insufficient_funds")
    assert agg["provisional"] is True


def test_a_standalone_only_target_is_not_provisional(monkeypatch):
    """Standalone jobs cannot make a target provisional.

    They are read only at status ``succeeded``, which is a tool_jobs status and
    is NOT a member of CAMPAIGN_TERMINAL_STATUSES (that set is
    completed / completed_with_failures / failed / cancelled). Testing a
    standalone job against the campaign set would mark every finished
    standalone run provisional forever, and a target with no campaign at all
    could never fall back to the paused copy because there is no paused run.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[{"pdb_key": "s.pdb", "scores": {"ipTM": 0.72}}],
        ),
    ]
    _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["standalone_jobs"] == 1
    assert agg["provisional"] is False


# ---------------------------------------------------------------------------
# The envelope carries the runs
# ---------------------------------------------------------------------------

def test_campaigns_ride_the_envelope_so_the_route_reads_once(monkeypatch):
    """``target_detail`` drops its own list_campaigns_for_target call.

    One read, not two. Asserted on the call log rather than on the returned
    list, because returning the objects while still reading them twice would
    satisfy a shape assertion and none of the point.
    """
    campaigns = [_StubCampaign("C", tool="bindcraft", preset="default")]
    client = _install(monkeypatch, rows=[], campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["campaigns"] == campaigns
    assert len(client.campaign_list_calls) == 1


def test_per_tool_carries_the_run_count_for_its_tool(monkeypatch):
    """Two runs of one tool on one target: two campaigns, one cohort."""
    rows = [
        _job_row(
            "a-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-a", chunk_index=0,
            candidates=[{"pdb_key": "a.pdb", "scores": {"ipTM": 0.91}}],
        ),
        _job_row(
            "b-0", tool="bindcraft", preset="default", target_id="T",
            campaign_id="C-b", chunk_index=0,
            candidates=[{"pdb_key": "b.pdb", "scores": {"ipTM": 0.60}}],
        ),
    ]
    campaigns = [
        _StubCampaign("C-a", tool="bindcraft", preset="default"),
        _StubCampaign("C-b", tool="bindcraft", preset="default"),
    ]
    _install(monkeypatch, rows=rows, campaigns=campaigns)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["per_tool"]["bindcraft"]["campaigns"] == 2
    assert agg["per_tool"]["bindcraft"]["total"] == 2
    assert list(agg["per_tool"]["bindcraft"]["presets"]) == ["default"]


# ---------------------------------------------------------------------------
# The cap, the sort, and the shape of the envelope itself
# ---------------------------------------------------------------------------

def _many_rows(n, *, tool="bindcraft", job="solo-1"):
    return [
        _job_row(
            job, tool=tool, preset="default", target_id="T",
            candidates=[
                {"pdb_key": f"d{i:04d}.pdb", "scores": {"ipTM": i / 1000.0}}
                for i in range(n)
            ],
        ),
    ]


def test_limit_is_forwarded_so_an_uncapped_export_is_uncapped(monkeypatch):
    """``limit`` reaches the ranking layer, and ``limit=None`` means no cap.

    The CSV and FASTA exports pass ``limit=None`` deliberately (the ZIP passes
    300 because it pulls structure bytes), so a hardcoded default here would
    silently truncate every target CSV to the top 300 of however many designs
    the user paid for, with the file still reporting itself complete.
    """
    _install(monkeypatch, rows=_many_rows(320))

    capped = target_results.aggregate_target_candidates("T", user_id=OWNER)
    assert capped["total"] == 320, f"partial={capped['partial']!r}"
    assert capped["shown"] == 300
    assert capped["capped"] is True
    assert capped["limit"] == 300

    full = target_results.aggregate_target_candidates(
        "T", user_id=OWNER, limit=None,
    )
    assert full["total"] == 320
    assert full["shown"] == 320
    assert len(full["candidates"]) == 320
    assert full["capped"] is False
    assert full["limit"] is None


def test_sort_mode_changes_the_order_and_never_the_set(monkeypatch):
    """``?sort=`` is forwarded, and an unknown value falls back rather than 400s.

    Both consuming routes pass the query string's value straight through, so a
    dropped keyword here renders percentile order under a toggle reading "by
    tool" and exports a file whose row order does not match the screen the
    export promises to match. The SET must not move: the cap is taken in
    canonical order before the display sort, which is what lets an export
    honour the active sort without changing what it contains.
    """
    rows = [
        _job_row(
            "bc-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[
                {"pdb_key": "b-best.pdb", "scores": {"ipTM": 0.95}},
                {"pdb_key": "b-worst.pdb", "scores": {"ipTM": 0.10}},
            ],
        ),
        _job_row(
            "rf-1", tool="rfantibody", preset="default", target_id="T",
            candidates=[
                {"pdb_key": "r-best.pdb", "scores": {"ipAE": 3.0}},
                {"pdb_key": "r-worst.pdb", "scores": {"ipAE": 30.0}},
            ],
        ),
    ]
    _install(monkeypatch, rows=rows)
    by_percentile = target_results.aggregate_target_candidates(
        "T", user_id=OWNER, sort_mode="percentile",
    )
    _install(monkeypatch, rows=rows)
    by_tool = target_results.aggregate_target_candidates(
        "T", user_id=OWNER, sort_mode="tool",
    )
    _install(monkeypatch, rows=rows)
    bogus = target_results.aggregate_target_candidates(
        "T", user_id=OWNER, sort_mode="not-a-mode",
    )

    order = [c["pdb_key"] for c in by_percentile["candidates"]]
    grouped = [c["pdb_key"] for c in by_tool["candidates"]]
    assert order == ["b-best.pdb", "r-best.pdb", "b-worst.pdb", "r-worst.pdb"]
    assert grouped == [
        "b-best.pdb", "b-worst.pdb", "r-best.pdb", "r-worst.pdb",
    ]
    assert sorted(order) == sorted(grouped), "the sort changed the SET"
    assert by_percentile["sort_mode"] == "percentile"
    assert by_tool["sort_mode"] == "tool"
    # Unrecognised: fall back and echo what was actually applied, so the page
    # cannot label the table with a mode it did not use.
    assert bogus["sort_mode"] == "percentile"
    assert [c["pdb_key"] for c in bogus["candidates"]] == order


def test_every_envelope_carries_the_same_keys_whatever_the_answer(monkeypatch):
    """A route reads ``agg["provisional"]`` on the 404 path without a KeyError.

    Three envelopes leave this module: populated, not-found, and unreadable.
    They are consumed by the same template and the same export helper, so a
    key present on one and missing on another is a 500 on whichever path is
    rarer, which is always the one nobody clicks before shipping.

    ``shown`` / ``unranked`` / ``limit`` are asserted here because nothing
    else asserts them at all, and the rollup line above the table is built
    from exactly those numbers.
    """
    rows = [
        _job_row(
            "solo-1", tool="bindcraft", preset="default", target_id="T",
            candidates=[
                {"pdb_key": "ranked.pdb", "scores": {"ipTM": 0.72}},
                {"pdb_key": "no-metric.pdb", "scores": {}},
            ],
        ),
    ]
    _install(monkeypatch, rows=rows)
    populated = target_results.aggregate_target_candidates(
        "T", user_id=OWNER, limit=7,
    )
    missing = target_results.aggregate_target_candidates(
        "no-such-target", user_id=OWNER,
    )
    _install(monkeypatch, rows=rows, get_target=lambda target_id, **kw: None)
    monkeypatch.setattr(target_results, "get_service_client", lambda: None)
    unreadable = target_results.aggregate_target_candidates(
        "T", user_id=OWNER,
    )

    assert sorted(populated) == sorted(missing) == sorted(unreadable)
    assert populated["shown"] == len(populated["candidates"]) == 2
    assert populated["unranked"] == 1, (
        "the row with no resolvable primary metric was not counted"
    )
    assert populated["limit"] == 7
    assert missing["ok"] is False and unreadable["ok"] is True


def test_an_unreadable_run_list_makes_the_ranking_provisional(monkeypatch):
    """``campaigns`` stays [] when the run list read fails, and any() over an
    empty list is False, so a target whose runs were mid-flight was certified
    as settled by a read that never saw them. The page then dropped the
    "Ranking is provisional" disclosure entirely.

    A read that could not enumerate the runs cannot certify they are terminal.
    """
    _install(monkeypatch, rows=(), campaigns=(), targets=(("T", OWNER),))

    def _boom(*a, **kw):
        raise RuntimeError("run list unavailable")

    monkeypatch.setattr(target_results, "list_campaigns_for_target", _boom)
    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["ok"] is True
    assert agg["partial"] is True
    assert agg["campaigns"] == []
    assert agg["provisional"] is True


def test_a_fully_readable_settled_target_is_not_provisional(monkeypatch):
    """The pair. Returning `True` unconditionally would satisfy the test above
    while permanently telling every finished target its ranking may still move.
    """
    _install(monkeypatch, rows=(), campaigns=(
        _StubCampaign("c-1", tool="bindcraft", preset="pilot",
                      status="completed"),
    ), targets=(("T", OWNER),))
    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["partial"] is False
    assert agg["provisional"] is False
