"""Resolve exact IMVU account usernames, never decorative display names."""
import re
from urllib.parse import urlencode


class UserLookupError(ValueError):
    pass


def clean_username(value):
    value = value.strip().removeprefix("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", value):
        raise UserLookupError("اكتب اسم حساب IMVU الحقيقي يا نجم، مش اسم العرض المزخرف 😄")
    return value


def exact_user(data, username):
    exact = {}
    guests = {}
    for url, node in data.get("denormalized", {}).items():
        match = re.fullmatch(r"https://api\.imvu\.com/user/user-([1-9]\d*)", url)
        info = node.get("data", {})
        actual = info.get("username")
        if not match or not isinstance(actual, str):
            continue
        if actual.casefold() == username.casefold():
            exact[match[1]] = actual
        elif (not username.casefold().startswith("guest_")
                and actual.casefold() == "guest_" + username.casefold()):
            # Classic may hide Guest_; this is an account-name alias, never
            # a comparison with display_name (which is non-unique).
            guests[match[1]] = actual
    matches = list((exact or guests).items())
    if len(matches) != 1:
        raise UserLookupError("مش لاقي حساب واحد بالاسم ده. اتأكد من اسم الحساب الأصلي بالظبط 🔎")
    return matches[0]


async def resolve_username(api, value):
    username = clean_username(value)
    data = await api("GET", "/user?" + urlencode({"username": username}))
    return exact_user(data, username)
