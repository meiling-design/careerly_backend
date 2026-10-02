"""Identity profile assembly for 「我的」 (P3-3).

``GET /api/v1/profile`` serves the identity block at the top of 「我的」:
display name, handle, membership badge.

Data sources, and the one that is missing
-----------------------------------------
* ``displayName`` ← ``auth.db: users.name`` (whatever was typed at registration)
* ``handle``      ← ``users.phone``, **masked** — see below
* ``membershipLabel`` ← ``careerly.db: user_quota.is_member``

⚠️ Why the phone is masked
--------------------------
``handle`` is rendered on screen next to the avatar. Echoing the raw phone
number would put a full identifier into every screenshot, share, and screen
recording. ``13800000000`` → ``138****0000`` keeps it recognisable to its owner
while removing the useful part for anyone else. The full number is still
available at the auth layer for anything that genuinely needs it.

⚠️ ``membershipLabel`` when the user is not a member
----------------------------------------------------
Returns ``""`` rather than 「免费用户」. The Flutter UI renders the badge only
when the label is non-empty, and a badge that says "you have nothing" is worse
than no badge. Whether free users should see *some* label is a product call
(needs Figma), not something to guess here.
"""

from __future__ import annotations

from typing import Any, Optional

from app.careerly.schemas import UserProfile

MEMBER_LABEL = "会员"


def mask_phone(phone: Optional[str]) -> str:
    """``13800000000`` → ``138****0000``; passes through anything unexpected.

    Deliberately conservative: if the value is not a recognisable 11-digit
    mainland number, it is returned unchanged rather than mangled — guessing at
    formats produces ``****`` for short or international numbers.
    """
    raw = (phone or "").strip()
    if len(raw) == 11 and raw.isdigit():
        return f"{raw[:3]}****{raw[-4:]}"
    return raw


def build_user_profile(
    auth_user: Optional[dict],
    quota: Any = None,
) -> UserProfile:
    """Assemble ``UserProfile`` from the auth row plus the quota row."""
    if not auth_user:
        return UserProfile()

    name = (auth_user.get("name") or "").strip()
    phone = auth_user.get("phone")

    is_member = bool(getattr(quota, "isMember", False)) if quota is not None else False

    return UserProfile(
        displayName=name,
        handle=mask_phone(phone),
        membershipLabel=MEMBER_LABEL if is_member else "",
    )
