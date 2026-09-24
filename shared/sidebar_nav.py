"""Group list for the signed-in left rail (``templates/_sidebar.html``).

The tool groups are the catalog's own intent bands -- this module calls
``group_catalog`` rather than restating the taxonomy, so renaming a band
in ``shared/tools_catalog.py`` moves the rail heading with it.

Rendered only for signed-in users, so it is built lazily from a Jinja
global instead of a context processor; a signed-out marketing page never
pays for the catalog walk.
"""

from flask import url_for


def _link(endpoint: str, label: str, **values) -> dict | None:
    """Return a rail row, or ``None`` when the endpoint does not resolve.

    Dropping an unresolvable row keeps the rail from raising
    ``BuildError`` inside ``base.html``, which every one of the 58
    templates extending it would inherit.
    """
    try:
        return {"label": label, "href": url_for(endpoint, **values)}
    except Exception:  # noqa: BLE001 — unregistered blueprint / no request ctx
        return None


def sidebar_groups() -> list[dict]:
    """Return ``[{title, items:[{label, href}]}]`` for the left rail."""
    from shared.tools_catalog import (  # noqa: PLC0415 — avoid import cycle
        _build_tools_catalog,
        group_catalog,
    )

    groups: list[dict] = []

    home = [
        _link("public.index", "Home"),
        _link("targets.targets_list", "Targets"),
        _link("campaigns.compute_campaigns_list", "Campaigns"),
    ]
    home = [i for i in home if i]
    if home:
        groups.append({"title": "Overview", "items": home})

    for band, members in group_catalog(_build_tools_catalog()):
        items = [
            {"label": t["name"], "href": t["route"]}
            for t in members
            if t.get("route") and t["route"] != "#"
        ]
        if items:
            groups.append({"title": band, "items": items})

    manage = [
        _link("jobs.jobs_list", "My jobs"),
        _link("wallet.wallet_overview", "Wallet"),
        _link("auth.account", "Account"),
        _link("platform_account.account_api_keys", "API keys"),
    ]
    manage = [i for i in manage if i]
    if manage:
        groups.append({"title": "Manage", "items": manage})

    return groups


def _covers(href: str, path: str) -> bool:
    """True when ``path`` is ``href`` or a page nested under it.

    Prefix matching is what makes ``/jobs/<id>`` light up "My jobs".
    ``/`` is matched exactly, or Home would be active everywhere.
    """
    if href == "/":
        return path == "/"
    return path == href or path.startswith(href + "/")


def sidebar_active_href(groups: list[dict], path: str) -> str:
    """Return the ONE href the rail should mark active for ``path``.

    Longest match wins because the rail's hrefs nest: ``/account/api-keys``
    sits under ``/account``, so a plain "does this href cover the path"
    test lights up two rows on the API keys page.
    """
    if not path:
        return ""
    path = path.rstrip("/") or "/"
    best = ""
    for group in groups:
        for item in group["items"]:
            href = (item.get("href") or "").rstrip("/") or "/"
            if _covers(href, path) and len(href) > len(best):
                best = href
    return best
