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
    matches = []
    for url, node in data.get("denormalized", {}).items():
        match = re.fullmatch(r"https://api\.imvu\.com/user/user-([1-9]\d*)", url)
        info = node.get("data", {})
        actual = info.get("username")
        if match and isinstance(actual, str) and actual.casefold() == username.casefold():
            matches.append((match[1], actual))
    if len(matches) != 1:
        raise UserLookupError("مش لاقي حساب واحد بالاسم ده. اتأكد من اسم الحساب الأصلي بالظبط 🔎")
    return matches[0]


async def resolve_username(api, value):
    username = clean_username(value)
    data = await api("GET", "/user?" + urlencode({"username": username}))
    return exact_user(data, username)
