"""URL template substitution, in one place.

There are two modules that address Instagram by a template -- the probe, which
reads routes, and the API channel, which writes one -- and both need the same two
answers: *what does this URL become*, and *what is still missing from it*.

Those are kept together deliberately. They were once one function in one module,
and when the second caller arrived the first caller grew a table the second could
disagree with. That is not a hypothetical: ``flag_user_route_exists`` needed
``{user_id}``, ``_main`` supplied only ``{username}``, and the malformed path's
404 was reported as evidence about a route. A second copy of the substitution
rules would have been a second chance at exactly that.

The two callers want different *contracts* from the same helper, and that is
deliberate: :func:`render_url` leaves an unknown placeholder visible, because the
probe would rather show ``{user_id}`` in its output than silently address a
different endpoint. The API channel additionally insists the result has no
placeholders left, via :func:`missing_placeholders`, because there a leftover
brace is a request that cannot be sent. Same substitution, different
requirements, both stated by the caller rather than guessed here.
"""

from __future__ import annotations

import re
from typing import Mapping

#: ``{name}`` as it appears in a URL template.
#:
#: Deliberately narrow in two ways. It requires a leading letter or underscore,
#: so ``{}`` and ``{1}`` are not treated as named placeholders. And it does not
#: match a percent-encoded brace, so a URL that legitimately contains one is not
#: rejected for a reason that does not apply to it.
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_url(url: str, substitutions: Mapping[str, str]) -> str:
    """Substitute ``{name}`` placeholders, leaving unknown ones in place.

    Uses ``str.replace`` rather than ``str.format`` deliberately. ``format``
    raises ``KeyError`` on a placeholder it has no value for, and callers here
    routinely have one they intentionally do not substitute -- so a strict
    formatter would make the probe refuse to run, and the loose alternative of
    pre-formatting the table would hide which call was actually made.

    Not idempotent on nested templates, and does not need to be: a substituted
    value containing ``{...}`` is not re-expanded, which is the safe direction.
    Substitution is single-pass on purpose.
    """
    for key, value in substitutions.items():
        url = url.replace("{" + key + "}", value)
    return url


def missing_placeholders(url: str, supplied: Mapping[str, str] | set[str]) -> set[str]:
    """Names ``url`` needs that ``supplied`` does not provide.

    ``supplied`` is a mapping or any iterable of names -- a caller holding the
    substitution table and a caller holding the set of keys it can offer are both
    natural, and neither should have to build the other's type.
    """
    have = set(supplied)
    return set(PLACEHOLDER.findall(url)) - have
