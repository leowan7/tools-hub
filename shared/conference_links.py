"""Conference short-link destinations for the /talk/<campaign> redirector.

The /talk/<campaign> route on tools.ranomics.com (D5 of the growth plan)
is a short link printed on conference posters, slides, and badges. It
302-redirects to a UTM-tagged tool or landing page so we can attribute
post-talk traffic to the campaign that drove it.

Each entry maps a campaign slug (used in the printed URL, e.g.
``/talk/pegs-2026``) to a destination URL. The route handler appends
``utm_source=conference-<campaign>``, ``utm_medium=outbound`` and
``utm_campaign=<campaign>`` so the analytics pipeline ties every click
back to the originating conference.

The ``default`` entry is also used as the fallback destination when an
unknown campaign slug is requested; the route handler still attaches
UTM params so the click is captured.
"""
from __future__ import annotations

# Pre-populated placeholders. Swap to real upcoming conferences as
# they are confirmed; keep the slugs short and printable.
CONFERENCE_LINKS: dict[str, str] = {
    "pegs-2026": "https://tools.ranomics.com/tools/bindcraft",
    "ai-bio-2026": "https://tools.ranomics.com/tools/rfdiffusion",
    "default": "https://tools.ranomics.com/",
}
