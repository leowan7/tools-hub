"""Jinja render smoke tests for wallet templates and pricing.

Closes Wave 4 Fix 8. Each test boots the real Flask app, pushes a
request context (with realistic query args and session state where
the template depends on it), and calls ``render_template`` with a
fixture context that mirrors what the production route passes. A
failure here means a contract mismatch between an app.py route and
a template, or a stray Jinja syntax error.

The Wave 2 contract mismatches that landed on main and only got
caught at cross diff review (sections 4.1, 4.2, 4.3 of
``docs/WAVE2-REVIEW.md``) would have failed these tests at CI.

Templates covered:

  templates/wallet/overview.html       (account dashboard)
  templates/wallet/topup.html          (4 variants: standalone, gate,
                                        success, error)
  templates/wallet/transactions.html   (ledger view, with + without rows)
  templates/pricing.html               (logged in + anonymous)

The partial ``templates/wallet/_partials.html`` is rendered transitively
when topup forms include it; it does not have its own dedicated test
yet because it is imported as macros rather than rendered standalone.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from datetime import datetime, timezone

import pytest
from flask import render_template

pytestmark = pytest.mark.usefixtures("isolate_supabase")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """Boot the Flask app with the wallet feature flags on.

    Mirrors the fixture in ``tests/test_wallet_api.py`` so endpoint
    registrations (``url_for('tool_form', ...)``, ``url_for('signup')``,
    ``url_for('index')``, ``url_for('wallet_topup')``) resolve.
    """
    monkeypatch.setenv("FLAG_TOOL_MPNN", "on")
    monkeypatch.setenv("FLAG_TOOL_BINDCRAFT", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _wallet_fixture(balance=42.50, frozen=False, auto_reload_on=False):
    """Realistic wallet dict matching shared/wallet.py shape."""
    return {
        "user_id": "u-smoke",
        "balance_usd": Decimal(str(balance)),
        "auto_reload_enabled": auto_reload_on,
        "auto_reload_threshold_usd": Decimal("10.00"),
        "auto_reload_amount_usd": Decimal("50.00"),
        "auto_reload_monthly_cap_usd": Decimal("1000.00"),
        "wallet_frozen": frozen,
        "spent_today_usd": Decimal("3.25"),
        "spent_30d_usd": Decimal("47.10"),
        "signup_credit_used_usd": Decimal("5.00"),
        "stripe_customer_id": None,
    }


def _transaction_fixture(kind="topup", amount=20.00, balance_after=42.50):
    """One ledger row matching shared/wallet.py shape."""
    return {
        "id": "tx-1",
        "kind": kind,
        "amount_usd": Decimal(str(amount)),
        "balance_after_usd": Decimal(str(balance_after)),
        "created_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc),
        "tool_slug": "mpnn" if kind in ("hold", "charge") else None,
        "job_id": "job-1" if kind in ("hold", "charge") else None,
        "parent_tx_id": None,
        "note": None,
        "stripe_event_id": "evt_test_1" if kind == "topup" else None,
    }


# ---------------------------------------------------------------------------
# wallet/overview.html
# ---------------------------------------------------------------------------


class TestWalletOverviewTemplate:
    def test_overview_renders_with_realistic_context(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[
                    _transaction_fixture("topup"),
                    _transaction_fixture("charge", -1.25, 41.25),
                ],
                user_email="u@example.com",
            )
        assert "Current balance" in html
        assert "$42.50" in html
        assert "Top up" in html

    def test_overview_renders_with_empty_transactions(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Wallet" in html

    def test_overview_shows_topup_success_banner_when_query_arg_set(self, app):
        """request.args.get('topup_success') controls the banner."""
        with app.test_request_context("/account/wallet?topup_success=1"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Top up confirmed" in html

    def test_overview_shows_frozen_warning_when_wallet_frozen(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(frozen=True),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Wallet frozen" in html

    def test_overview_shows_unspent_signup_credit_and_expiry(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
                signup_credit_status={
                    "remaining_usd": Decimal("12.3456"),
                    "grant_usd": Decimal("15"),
                    "expires_at": datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc),
                },
            )
        assert "$12.34" in html
        assert "of $15.00 unspent" in html
        assert "expires October 28, 2026" in html

    def test_overview_without_signup_credit_status(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Signup credit" not in html
        assert "$0.00" not in html


# ---------------------------------------------------------------------------
# wallet/topup.html (4 variants)
# ---------------------------------------------------------------------------


class TestWalletTopupTemplate:
    def test_standalone_topup_form_renders(self, app):
        """No deficit_usd, no topup_success, no topup_error.

        Renders the bare 'Pick an amount' form, the auto reload panel,
        and the cost table.
        """
        with app.test_request_context("/account/wallet/topup"):
            html = render_template(
                "wallet/topup.html",
                wallet=_wallet_fixture(),
                min_topup_usd=Decimal("5.00"),
                next_url=None,
                topup_action_url="/account/wallet/checkout",
                topup_error=None,
            )
        assert "Top up your wallet" in html
        assert "Pick an amount" in html
        assert "Continue to checkout" in html
        assert "Auto reload" in html
        assert "wallet-topup-form" in html

    def test_gate_flow_shows_the_notice_without_a_figure(self, app):
        """Decorator gate render, as shared/wallet_guard.py::_render_topup_gate
        calls it. A stale ``deficit_usd`` kwarg is passed to show it is not
        printed."""
        with app.test_request_context("/tools/mpnn"):
            html = render_template(
                "wallet/topup.html",
                wallet=_wallet_fixture(balance=2.00),
                deficit_usd=Decimal("15.50"),
                min_topup_usd=Decimal("5.00"),
                next_url="/tools/mpnn",
                return_tool="mpnn",
                gate_reason="insufficient_balance",
                tool_slug="mpnn",
            )
        # Nothing resumes the job after payment, so nothing may promise it.
        assert "Top up and run" not in html
        assert "where you left off" not in html
        assert "Nothing is submitted for you" in html
        assert "Your balance does not cover this run." in html
        assert "15.50" not in html
        assert "Back to the form" in html

    def test_form_gate_is_a_link_not_a_submit(self, app):
        """The form's gate must not post the form (it had no server handler)."""
        with app.test_request_context("/tools/bindcraft"):
            tpl = app.jinja_env.from_string(
                '{% from "wallet/_partials.html" import wallet_topup_gate %}'
                '{{ wallet_topup_gate(tool_slug="bindcraft", balance_usd=5) }}'
            )
            html = tpl.render()
        assert "topup_and_run" not in html
        assert 'type="submit"' not in html
        assert 'href="/account/wallet/topup?tool=bindcraft"' in html

    def test_success_state_renders_receipt_and_return_tool_cta(self, app):
        """After Stripe Checkout returns cleanly.

        Tests the Fix 7 success branch.
        """
        with app.test_request_context(
            "/account/topup-complete?session_id=cs_test_1"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_success=True,
                stripe_session={
                    "id": "cs_test_1",
                    "amount_total": 2300,
                    "currency": "usd",
                    "status": "complete",
                    "payment_status": "paid",
                    "metadata": {"user_id": "u-smoke"},
                },
                wallet=_wallet_fixture(balance=68.00),
                return_tool="mpnn",
                return_tool_url="/tools/mpnn",
            )
        assert "Top up complete" in html
        assert "$23.00" in html
        assert "New balance $68.00" in html
        assert "Return to mpnn" in html
        assert "cs_test_1" in html
        assert "Pick an amount" not in html, (
            "success state must not render the top up form"
        )

    def test_success_state_without_return_tool_shows_browse_tools_cta(self, app):
        with app.test_request_context(
            "/account/topup-complete?session_id=cs_test_2"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_success=True,
                stripe_session={
                    "id": "cs_test_2",
                    "amount_total": 5000,
                    "currency": "usd",
                    "status": "complete",
                    "payment_status": "paid",
                },
                wallet=_wallet_fixture(balance=92.50),
                return_tool=None,
                return_tool_url=None,
            )
        assert "Top up complete" in html
        assert "View wallet" in html
        assert "Browse tools" in html

    def test_error_state_renders_banner_above_form(self, app):
        """After Stripe Checkout fails or session lookup errors.

        Tests the Fix 7 error branch. The form is preserved so the
        user can retry.
        """
        with app.test_request_context(
            "/account/topup-complete?session_id=bad"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_error=(
                    "Could not validate the Stripe session. The webhook "
                    "still credits the wallet when payment clears."
                ),
                wallet=_wallet_fixture(),
                return_tool="bindcraft",
            )
        assert "Top up did not complete" in html
        assert "Could not validate the Stripe session" in html
        # Form is still there for retry
        assert "Pick an amount" in html
        assert "Continue to checkout" in html
        # Return to tool link is rendered
        assert "bindcraft" in html


# ---------------------------------------------------------------------------
# wallet/transactions.html
# ---------------------------------------------------------------------------


class TestWalletTransactionsTemplate:
    def test_transactions_renders_with_rows(self, app):
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[
                    _transaction_fixture("signup_credit", 5.00, 5.00),
                    _transaction_fixture("topup", 50.00, 55.00),
                    _transaction_fixture("hold", -3.00, 52.00),
                    _transaction_fixture("charge", -2.40, 49.60),
                    _transaction_fixture("hold_release", 0.60, 50.20),
                ],
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=5,
            )
        assert "Transaction history" in html
        assert "Current balance" in html
        assert "5 entries" in html

    def test_transactions_renders_with_kind_filter(self, app):
        with app.test_request_context(
            "/account/wallet/transactions?kind=topup"
        ):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[_transaction_fixture("topup")],
                filter_kind="topup",
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=1,
            )
        assert "Transaction history" in html

    def test_transactions_renders_with_empty_list(self, app):
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(balance=0),
                transactions=[],
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=0,
            )
        assert "Transaction history" in html

    def test_transactions_renders_with_pagination(self, app):
        with app.test_request_context("/account/wallet/transactions?page=2"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[_transaction_fixture("topup")],
                filter_kind=None,
                page=2,
                page_size=50,
                has_next=True,
                has_prev=True,
                total_count=125,
            )
        assert "Transaction history" in html

    # ---- Net per job cost display (presentation only) --------------------

    def _tx(self, tx_id, kind, amount, balance_after,
            parent_tx_id=None, notes=None):
        """Ledger row with an explicit id + parent for lineage tests."""
        return {
            "id": tx_id,
            "kind": kind,
            "amount_usd": Decimal(str(amount)),
            "balance_after_usd": Decimal(str(balance_after)),
            "created_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc),
            "tool_slug": "mpnn" if kind in ("hold", "charge", "hold_release") else None,
            "job_id": None,
            "parent_tx_id": parent_tx_id,
            "notes": notes,
            "stripe_event_id": None,
        }

    def test_hold_plus_charge_shows_single_net_not_two_charges(self, app):
        """hold (-1.75) + charge (-1.45) for one job: net = actual 3.20.

        The row pair must read as one true cost, not two independent
        charges. The hold is relabeled 'reserved' and the charge row
        carries the 'net for this job' equal to the actual.
        """
        rows = [
            # settlement row is newest-first (charge above its hold)
            self._tx("c-1", "charge", -1.45, 48.80, parent_tx_id="h-1"),
            self._tx("h-1", "hold", -1.75, 50.25),
        ]
        # Group net = -1.75 + -1.45 = -3.20 => actual 3.20.
        annotations = {
            "h-1": {"role": "hold", "settled": True, "reserved": Decimal("1.75"),
                    "outcome": "more_charged"},
            "c-1": {"role": "settlement", "net": Decimal("-3.20")},
        }
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations=annotations,
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=2,
            )
        # One clear net for the job equal to the actual.
        assert "net for this job $3.20" in html
        # Hold reads as a reservation, not a charge.
        assert "reserved" in html
        # There is exactly one net-for-this-job label (not two charges).
        assert html.count("net for this job") == 1
        # A variance debit returned nothing.
        assert "it settled for more than was reserved" in html
        assert "unused part was returned" not in html

    def test_hold_plus_release_surplus_shows_net_and_returned_label(self, app):
        """hold (-2.00) + hold_release (+0.60): net = actual 1.40.

        No charge row, so the release is the settlement row and carries
        the net; it is also labeled as a returned reservation.
        """
        rows = [
            self._tx("r-1", "hold_release", 0.60, 50.60, parent_tx_id="h-2"),
            self._tx("h-2", "hold", -2.00, 50.00),
        ]
        # Group net = -2.00 + 0.60 = -1.40 => actual 1.40.
        annotations = {
            "h-2": {"role": "hold", "settled": True, "reserved": Decimal("2.00"),
                    "outcome": "part_returned"},
            "r-1": {"role": "settlement", "net": Decimal("-1.40")},
        }
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations=annotations,
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=2,
            )
        assert "net for this job $1.40" in html
        # The hold is a reservation and its surplus was returned.
        assert "reserved" in html
        assert "returned" in html

    def test_pending_hold_no_children_labeled_reserved_not_charged(self, app):
        """A hold with no settle child is a pending reservation.

        It must read as reserved / pending settlement, never as a charge.
        """
        rows = [self._tx("h-3", "hold", -4.00, 46.00)]
        annotations = {
            "h-3": {"role": "hold", "settled": False, "reserved": Decimal("4.00")},
        }
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations=annotations,
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=1,
            )
        assert "reserved (pending settlement)" in html
        # A pending hold is not a settled net cost.
        assert "net for this job" not in html

    @pytest.mark.parametrize("children,outcome", [
        ([("charge", 0)], "none_returned"),
        ([("charge", -1.45)], "more_charged"),
        ([("hold_release", 0.60)], "part_returned"),
        ([("hold_release", 8.00)], "all_returned"),
        ([("absorbed_variance", 0)], "none_returned"),
    ])
    def test_hold_outcome_is_read_from_the_amounts(self, children, outcome):
        from blueprints.wallet import _build_tx_lineage_annotations

        rows = [dict(self._tx("h-1", "hold", -8.00, 42.00), user_id="u-1")]
        for i, (kind, amount) in enumerate(children):
            rows.append(dict(
                self._tx(f"c-{i}", kind, amount, 42.00, parent_tx_id="h-1"),
                user_id="u-1",
            ))
        annotations = _build_tx_lineage_annotations(
            _FakeLedgerClient(rows), "u-1", rows,
        )
        assert annotations["h-1"]["outcome"] == outcome

    def test_capped_settlement_rows_say_nothing_about_an_estimate(self, app):
        """F-17: bindcraft job 489a17e0 metered $10.81 and was charged $8.00.

        settle_hold clamps the actual to the hard cap, so the clamped
        actual equals the hold and it writes a zero-amount charge noted
        "estimate matched actual" (the v_diff = 0 branch of
        supabase/migrations/0020_wallet_corrections.sql). Nothing came back,
        so the reserved row must not say the unused part was returned, and
        neither row may print the stored note.
        """
        from blueprints.wallet import _build_tx_lineage_annotations

        rows = [
            dict(self._tx("c-9", "charge", 0, 42.00, parent_tx_id="h-9",
                          notes="estimate matched actual"), user_id="u-1"),
            dict(self._tx("h-9", "hold", -8.00, 42.00), user_id="u-1"),
        ]
        annotations = _build_tx_lineage_annotations(
            _FakeLedgerClient(rows), "u-1", rows,
        )
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations=annotations,
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=2,
            )
        assert "net for this job $8.00" in html
        assert "none of it was returned when it settled" in html
        assert "unused part was returned" not in html
        assert "estimate matched actual" not in html
        assert "estimat" not in html.lower()

    def test_notes_render_regression_note_to_notes(self, app):
        """The column is notes (plural). tx.notes must render.

        The old template read tx.note (singular), always empty. This
        guards the note => notes fix.
        """
        rows = [
            self._tx("a-1", "adjustment", -0.50, 49.50,
                     notes="manual correction by ops"),
        ]
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations={},
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=1,
            )
        assert "manual correction by ops" in html


class _FakeLedgerClient:
    """Just enough of the Supabase client for the lineage annotator."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        rows, filters = self._rows, []

        class _Q:
            def select(self, *_a):
                return self

            def eq(self, col, val):
                filters.append(lambda r: r.get(col) == val)
                return self

            def in_(self, col, vals):
                filters.append(lambda r: r.get(col) in vals)
                return self

            def execute(self):
                class _R:
                    data = [r for r in rows if all(f(r) for f in filters)]
                return _R()

        return _Q()


# ---------------------------------------------------------------------------
# A run's roll-up vs the debit rows printed under it
# ---------------------------------------------------------------------------


class TestRunRollUp:
    """The headline figure on a live run has to add up to its own rows.

    A live run is one table row (the $0 anchor) carrying "$X so far" or
    "it closed at $X", with its per-heartbeat debits listed under it and
    again as their own rows. Every amount cell is rendered by
    ``display_record_usd``, half-up to 2dp, so a roll-up summed from the
    exact amounts prints cents away from the column it stands over -- the
    harm ``shared/compute_campaigns.py::display_total_usd`` exists to
    prevent, on a page that does not call it.
    """

    @staticmethod
    def _rows(n, each):
        anchor = {
            "id": "h-1", "user_id": "u-1", "kind": "hold",
            "amount_usd": Decimal("0"), "balance_after_usd": Decimal("20"),
            "created_at": "2026-11-06T10:00:00+00:00",
            "tool_slug": "esmfold2_design", "job_id": None,
            "parent_tx_id": None, "notes": None, "stripe_event_id": None,
        }
        debits = [
            dict(anchor, id="d-%d" % i, kind="run_debit",
                 amount_usd=Decimal(each), balance_after_usd=Decimal("1"),
                 created_at="2026-11-06T10:%02d:00+00:00" % (10 + i),
                 parent_tx_id="h-1")
            for i in range(n)
        ]
        return [anchor] + debits

    @staticmethod
    def _render(app, rows, annotations):
        with app.test_request_context("/account/wallet/transactions"):
            return render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=rows,
                tx_annotations=annotations,
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=len(rows),
            )

    def test_roll_up_adds_up_to_the_debit_rows_printed_under_it(self, app):
        """Thirteen debits of $1.1549: the rows print $14.95, not $15.01.

        The shape of a stalled esmfold2-design run: many heartbeat debits,
        each a fraction of a cent above a round cent. Summed exactly,
        ``taken`` was 15.0137 and the anchor read "$15.01 so far" over
        thirteen rows each reading $1.15.
        """
        from blueprints.wallet import _build_tx_lineage_annotations
        from shared.compute_campaigns import display_record_usd

        rows = self._rows(13, "-1.1549")
        annotations = _build_tx_lineage_annotations(
            _FakeLedgerClient(rows), "u-1", rows,
        )
        printed = [display_record_usd(r["amount_usd"]) for r in rows[1:]]
        assert set(printed) == {"\u2212$1.15"}
        rows_total = sum(
            (Decimal(s.lstrip("\u2212$")) for s in printed), Decimal("0"),
        )
        assert rows_total == Decimal("14.95")
        assert annotations["h-1"]["taken"] == rows_total

        html = self._render(app, rows, annotations)
        assert "charged as it runs; $14.95 so far" in html
        assert "15.01" not in html

    def test_the_net_the_closed_branch_reads_stays_exact(self, app):
        """``net`` answers "is it zero", which a rounded sum answers wrongly.

        The template picks "all of it was returned when it closed" over "it
        closed at $X" on ``ann.net == 0``
        (``templates/wallet/transactions.html``, the is_run branch), so the
        figure it reads has to be the exact one. Both of these pairs
        misclassify if each row is rounded before being summed: the full
        refund sums to -0.06 (a cost on a free run) and the 5-cent charge
        sums to +0.01 (a refund on a charged run).
        """
        from blueprints.wallet import _build_tx_lineage_annotations

        def closed(each, released):
            rows = self._rows(13, each)
            rows.append(dict(rows[0], id="r-1", kind="hold_release",
                             amount_usd=Decimal(released),
                             created_at="2026-11-06T10:30:00+00:00",
                             parent_tx_id="h-1"))
            ann = _build_tx_lineage_annotations(
                _FakeLedgerClient(rows), "u-1", rows,
            )
            return ann, self._render(app, rows, ann)

        # settle_live_run releases v_taken - v_final, so a $0 final cost
        # returns the whole 15.0163 the debits took
        # (supabase/migrations/0045_live_run_debits.sql, its ELSE arm).
        ann, html = closed("-1.1551", "15.0163")
        assert ann["h-1"]["net"] == Decimal("0")
        assert "all of it was returned when it closed" in html
        assert "closed at" not in html

        # 5.37 cents kept: a cost, however small, is not a full refund.
        ann, html = closed("-1.1549", "14.96")
        assert ann["h-1"]["net"] == Decimal("-0.0537")
        assert "it closed at $0.05" in html
        assert "all of it was returned" not in html

    def test_a_live_run_of_only_sub_cent_debits_does_not_read_as_free(self, app):
        """An unsettled run of sub-cent debits says "<$0.01", not "$0.00".

        Reachable on a nearly empty wallet: debit_live_run takes
        GREATEST(LEAST(v_step, v_balance), 0), so a balance under a cent
        yields a sub-cent debit however much the heartbeat owes
        (``supabase/migrations/0045_live_run_debits.sql``). Each row prints
        "<$0.01", so the figure standing over them may not print "$0.00";
        summing the rounded rows alone gives exactly that.
        """
        from blueprints.wallet import _build_tx_lineage_annotations
        from shared.compute_campaigns import display_record_usd

        rows = self._rows(3, "-0.001")
        ann = _build_tx_lineage_annotations(
            _FakeLedgerClient(rows), "u-1", rows,
        )
        assert ann["h-1"]["settled"] is False
        assert ann["h-1"]["taken"] == Decimal("0.003")
        assert display_record_usd(ann["h-1"]["taken"]) == "<$0.01"
        html = self._render(app, rows, ann)
        # Jinja escapes the "<" of "<$0.01".
        assert "charged as it runs; &lt;$0.01 so far" in html
        assert "$0.00 so far" not in html

    def test_a_run_of_sub_cent_debits_is_not_read_as_a_refund(self, app):
        """The closed branch is guarded on having debits, not on their sum.

        Each of these debits prints "<$0.01" but contributes 0.00 to the
        printed total, so a guard on that total being positive would send a
        fully refunded run to the "it closed at $0.00" arm instead.
        """
        from blueprints.wallet import _build_tx_lineage_annotations

        rows = self._rows(3, "-0.001")
        rows.append(dict(rows[0], id="r-1", kind="hold_release",
                         amount_usd=Decimal("0.003"),
                         created_at="2026-11-06T10:30:00+00:00",
                         parent_tx_id="h-1"))
        ann = _build_tx_lineage_annotations(
            _FakeLedgerClient(rows), "u-1", rows,
        )
        assert ann["h-1"]["taken"] == Decimal("0.003")
        assert ann["h-1"]["net"] == Decimal("0")
        html = self._render(app, rows, ann)
        assert "all of it was returned when it closed" in html
        assert "closed at $0.00" not in html


# ---------------------------------------------------------------------------
# settle_hold's note on a clamped settlement (migration 0048)
# ---------------------------------------------------------------------------


class TestSettleHoldClampNote:
    """0048 may change settle_hold's note, and nothing else.

    F-17 (``docs/qa/QA-2026-10-01-paid-sweep.md``): the 0020 definition
    noted EVERY clamped settlement "estimate matched actual", because
    clamping the metered actual down to the cap drives v_diff to 0 and
    that branch was written for an estimate that was simply right.
    """

    MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "supabase" / "migrations"

    OLD_NOTE = "p_hold_tx_id, p_failure_reason, 'estimate matched actual'"
    NEW_NOTE = """p_hold_tx_id, p_failure_reason,
               CASE WHEN p_actual_usd > p_hard_cap_usd
                    THEN 'charge clamped at the tool hard cap of ' ||
                         p_hard_cap_usd::text || ' USD; metered ' ||
                         p_actual_usd::text || ' USD'
                    ELSE 'estimate matched actual' END"""

    @classmethod
    def _body(cls, name):
        """settle_hold's definition with every SQL comment line dropped."""
        sql = (cls.MIGRATIONS / name).read_text(encoding="utf-8")
        start = sql.index("CREATE OR REPLACE FUNCTION public.settle_hold(")
        end = sql.index("GRANT EXECUTE ON FUNCTION public.settle_hold(")
        return "\n".join(
            line.rstrip() for line in sql[start:end].splitlines()
            if line.strip() and not line.strip().startswith("--")
        )

    def test_0048_changes_only_the_note(self):
        """0048's body is 0020's with one notes expression swapped.

        Compared statement for statement (comments dropped), so nobody has
        to re-derive that the charged, released and absorbed amounts are
        untouched: a stray edit to any of them fails here.
        """
        old, new = self._body("0020_wallet_corrections.sql"), self._body(
            "0048_settle_hold_clamp_note.sql")
        assert old.count(self.OLD_NOTE) == 1
        expected = "\n".join(
            line.rstrip() for line in
            old.replace(self.OLD_NOTE, self.NEW_NOTE).splitlines()
        )
        assert new == expected

    def test_the_clamped_settlement_says_it_was_clamped(self):
        """Only the unclamped arm of the note claims the estimate matched."""
        new = self._body("0048_settle_hold_clamp_note.sql")
        assert "CASE WHEN p_actual_usd > p_hard_cap_usd" in new
        clamped, sep, rest = new.partition("ELSE 'estimate matched actual'")
        assert sep and "clamped at the tool hard cap" in clamped
        assert clamped.count("estimate matched actual") == 0
        assert rest.lstrip().startswith("END")


# ---------------------------------------------------------------------------
# pricing.html
# ---------------------------------------------------------------------------


class TestPricingTemplate:
    def test_pricing_renders_for_logged_in_user(self, app):
        """session.user_email set => 'Top up your wallet' CTA."""
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
        with app.test_request_context("/pricing"):
            from flask import session as flask_session
            flask_session["user_email"] = "u@example.com"
            html = render_template("pricing.html")
        assert "Your wallet is your budget" in html
        assert "Fund your wallet" in html

    def test_pricing_renders_for_anonymous_user(self, app):
        """No session.user_email => signup CTA quotes the minimum top-up."""
        with app.test_request_context("/pricing"):
            html = render_template("pricing.html")
        assert "Your wallet is your budget" in html
        from shared.wallet import MIN_TOPUP_USD
        assert f"Start with as little as ${MIN_TOPUP_USD:.0f}" in html
