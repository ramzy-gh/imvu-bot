"""Read the authenticated account's named saved outfit, without profile fallback."""
import re
from urllib.parse import urlparse

API = "https://api.imvu.com"


class OutfitError(ValueError):
    """A fixed diagnostic code, never raw private wardrobe data."""


def product_ids(values):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise OutfitError("invalid_outfit_products")
    ids = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise OutfitError("invalid_outfit_products")
        value = str(value)
        if not re.fullmatch(r"[1-9]\d{0,11}", value):
            raise OutfitError("invalid_outfit_products")
        if value not in ids:
            ids.append(value)
    if len(" ".join(ids)) > 400:
        raise OutfitError("outfit_too_large_for_announcement")
    return ids


def outfit_path(reference, uid):
    if not isinstance(reference, str):
        raise OutfitError("invalid_outfit_reference")
    parsed = urlparse(reference)
    if (parsed.scheme != "https" or parsed.netloc != "api.imvu.com"
            or parsed.query or parsed.fragment or parsed.params):
        raise OutfitError("invalid_outfit_reference")
    # Read only references returned by this account's own outfits collection.
    if not (re.fullmatch(r"/outfit/[A-Za-z0-9_-]+", parsed.path) or
            re.fullmatch(r"/user/user-" + re.escape(uid) + r"/outfits/[A-Za-z0-9_-]+", parsed.path)):
        raise OutfitError("invalid_outfit_reference")
    return parsed.path


def collection_node(payload, path):
    nodes = payload.get("denormalized", {}) if isinstance(payload, dict) else {}
    if not isinstance(nodes, dict):
        raise OutfitError("unsupported_outfit_collection")
    for key, node in nodes.items():
        parsed = urlparse(key)
        if parsed.scheme == "https" and parsed.netloc == "api.imvu.com" and parsed.path == path:
            if isinstance(node, dict) and isinstance(node.get("data"), dict) and isinstance(node["data"].get("items"), list):
                return nodes, node["data"]["items"]
    raise OutfitError("unsupported_outfit_collection")


async def load_named_outfit(api, uid, name="Bot"):
    """Use the caller's authenticated API session; never another user's wardrobe.

    Observed client schema: /user/user-{own_id}/outfits; outfitName and pids.
    No purchase, rename, profile mutation, or default outfit is performed here.
    """
    uid = str(uid)
    if not re.fullmatch(r"\d+", uid) or not isinstance(name, str) or not name.strip():
        raise OutfitError("invalid_outfit_selection")
    target = name.strip().casefold()
    path = f"/user/user-{uid}/outfits"
    seen = set()
    selected = None
    for offset in range(0, 5000, 25):
        payload = await api("GET", path, params={"start_index": offset, "limit": 25})
        nodes, items = collection_node(payload, path)
        new_count = 0
        for item in items:
            reference = item.get("id") if isinstance(item, dict) else item
            detail_path = outfit_path(reference, uid)
            if reference in seen:
                continue
            seen.add(reference)
            new_count += 1
            node = nodes.get(reference)
            if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
                detail = await api("GET", detail_path)
                detail_nodes = detail.get("denormalized", {}) if isinstance(detail, dict) else {}
                node = detail_nodes.get(reference, {}) if isinstance(detail_nodes, dict) else {}
            if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
                raise OutfitError("unsupported_outfit_fields")
            data = node.get("data", {})
            outfit_name = data.get("outfitName", data.get("name"))
            if not isinstance(outfit_name, str):
                raise OutfitError("unsupported_outfit_fields")
            if outfit_name.strip().casefold() != target:
                continue
            if selected is not None:
                raise OutfitError("duplicate_outfit_name")
            selected = {"name": outfit_name.strip(), "id": reference, "products": product_ids(data.get("pids"))}
        if len(items) < 25:
            if selected is None:
                raise OutfitError("named_outfit_not_found")
            return selected
        if new_count == 0:
            raise OutfitError("outfit_pagination_not_advancing")
    raise OutfitError("outfit_search_limit_reached")
