"""Developability Scout is reachable without an account.

It is the only member of the "See if a binder will hold up in the lab"
catalog band, so gating it made that entire band a login wall while its
catalog card advertised "see how it works".

Safe to open because the handler spends nothing and persists nothing: it
calls ``score_developability()``, a pure function, and renders a template.
No GPU, no wallet, no storage, no user identity. These tests pin BOTH
halves of that: the routes answer anonymously, AND the trust boundary that
makes it safe to do so is still enforced.
"""

from __future__ import annotations

import pytest

from app import create_app

# A real VH framework sequence, long enough to clear the 10-residue floor.
VALID_VH = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKG"
    "RFTISRDNSKNTLYLQMNSLRAEDTAVYYCAKDLGRRGYFDYWGQGTLVTVSS"
)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_get_is_anonymous(client):
    """No session, no redirect."""
    resp = client.get("/developability")
    assert resp.status_code == 200, (
        f"anonymous GET /developability returned {resp.status_code}; "
        "a 302 means the login gate is back and band 5 is walled again"
    )


def test_post_scores_anonymously(client):
    """Opening the form but gating submit is the promise-not-kept case."""
    resp = client.post(
        "/developability/score",
        data={"sequence": VALID_VH, "chain_type": "VH"},
    )
    assert resp.status_code == 200
    assert b"login" not in resp.request.path.encode()


# --- the trust boundary that makes anonymous access safe ------------------
# These bound the work an anonymous request can ask for. If any of these
# stops rejecting, opening the route stops being safe.


@pytest.mark.parametrize(
    "sequence,reason",
    [
        ("ACDEF", "below the 10-residue floor"),
        ("A" * 2001, "above the 2000-residue ceiling"),
        ("ACDEFGHIKLXXXZZZ", "non-canonical residues"),
        ("", "empty"),
    ],
)
def test_invalid_input_is_rejected(client, sequence, reason):
    resp = client.post(
        "/developability/score",
        data={"sequence": sequence, "chain_type": "VH"},
    )
    assert resp.status_code == 200, f"expected a re-rendered form for {reason}"
    body = resp.get_data(as_text=True).lower()
    assert "error" in body or "must be" in body or "before submitting" in body, (
        f"input {reason} was not rejected -- the length/alphabet bound is what "
        "keeps anonymous cost fixed"
    )


def test_unknown_chain_type_falls_back_not_crashes(client):
    resp = client.post(
        "/developability/score",
        data={"sequence": VALID_VH, "chain_type": "../../etc/passwd"},
    )
    assert resp.status_code == 200


def test_scoring_writes_nothing(monkeypatch, client):
    """The claim that justifies anonymous access: no persistence.

    If a future change adds a DB write here, it needs an identity and this
    route should not have been left open.
    """
    import shared.credits as credits

    def _boom(*a, **k):
        raise AssertionError(
            "developability scoring reached the database; it is supposed to "
            "be a pure function, which is why the route is anonymous"
        )

    monkeypatch.setattr(credits, "get_service_client", _boom, raising=False)
    resp = client.post(
        "/developability/score",
        data={"sequence": VALID_VH, "chain_type": "VH"},
    )
    assert resp.status_code == 200
