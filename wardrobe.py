"""Read the authenticated account's named saved outfit, without profile fallback."""
import re
import logging
from urllib.parse import urlparse

API = "https://api.imvu.com"
LOG = logging.getLogger("ramsei.wardrobe")


def card_name(data):
    if not isinstance(data, dict):
        return None
    return next((data[k] for k in ("outfit_name", "outfitName", "name")
                 if isinstance(data.get(k), str)), None)


def schema_diagnostic(node, stage):
    """Field labels only: never log outfit names, product IDs, or raw payloads."""
    def fields(value):
        return ",".join(sorted(k for k in value if isinstance(k, str)
                              and re.fullmatch(r"[A-Za-z_]{1,40}", k))[:25]) if isinstance(value, dict) else "not_object"
    data = node.get("data") if isinstance(node, dict) else None
    relations = node.get("relations") if isinstance(node, dict) else None
    LOG.warning("Wardrobe schema: parser=v11.3 stage=%s node_fields=%s data_fields=%s relation_fields=%s",
                stage, fields(node), fields(data), fields(relations))


def resolved_node(payload, reference, uid):
    nodes = payload.get("denormalized", {}) if isinstance(payload, dict) else {}
    if not isinstance(nodes, dict):
        return {}
    if isinstance(nodes.get(reference), dict):
        return nodes[reference]
    # A collection edge may resolve to the canonical outfit node. This ID comes
    # from the authenticated response, not an additional user-supplied request.
    response_id = payload.get("id")
    if isinstance(response_id, str):
        try:
            outfit_path(response_id, uid)
        except OutfitError:
            return {}
        node = nodes.get(response_id)
        if isinstance(node, dict):
            return node
    return {}


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


async def outfit_card(api, reference, nodes, uid, target):
    """Dereference collection edges and fetch incomplete cards, with a hop cap."""
    current = reference
    visited = {current}
    fetched = set()
    node = nodes.get(current, {})
    for _ in range(16):
        data = node.get("data") if isinstance(node, dict) else None
        name = card_name(data)
        if name is not None and (name.strip().casefold() != target or
                (isinstance(data.get("pids"), list) and data["pids"])):
            return node
        relations = node.get("relations", {}) if isinstance(node, dict) else {}
        ref = relations.get("ref") if isinstance(relations, dict) else None
        if isinstance(ref, dict):
            ref = ref.get("id")
        if ref is not None:
            outfit_path(ref, uid)  # same authenticated API and allowed outfit paths only
            if ref in visited:
                raise OutfitError("outfit_reference_cycle")
            visited.add(ref)
            current = ref
            node = nodes.get(current, {})
            continue
        if current in fetched:
            schema_diagnostic(node, "full_card_incomplete")
            raise OutfitError("unsupported_outfit_fields")
        fetched.add(current)
        detail = await api("GET", outfit_path(current, uid))
        detail_nodes = detail.get("denormalized", {}) if isinstance(detail, dict) else {}
        if isinstance(detail_nodes, dict):
            nodes.update(detail_nodes)
        node = resolved_node(detail, current, uid)
    raise OutfitError("outfit_reference_limit_reached")


async def load_named_outfit(api, uid, name="Bot"):
    """Use the caller's authenticated API session; never another user's wardrobe.

    Raw API schema: /user/user-{own_id}/outfits; outfit_name and pids.
    No purchase, rename, profile mutation, or default outfit is performed here.
    """
    uid = str(uid)
    if not re.fullmatch(r"\d+", uid) or not isinstance(name, str) or not name.strip():
        raise OutfitError("invalid_outfit_selection")
    target = name.strip().casefold()
    LOG.info("Wardrobe parser v11.3: looking up the configured saved outfit; following ref relations")
    path = f"/user/user-{uid}/outfits"
    seen = set()
    selected = None
    for offset in range(0, 5000, 25):
        payload = await api("GET", path, params={"start_index": offset, "limit": 25})
        nodes, items = collection_node(payload, path)
        new_count = 0
        for item in items:
            reference = item.get("id") if isinstance(item, dict) else item
            outfit_path(reference, uid)
            if reference in seen:
                continue
            seen.add(reference)
            new_count += 1
            node = await outfit_card(api, reference, nodes, uid, target)
            if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
                schema_diagnostic(node, "detail_node_missing")
                raise OutfitError("unsupported_outfit_fields")
            data = node.get("data", {})
            # The TS wrapper exposes outfitName, but its serializer maps this
            # property to outfit_name on the wire. Read the raw API field first.
            outfit_name = card_name(data)
            if not isinstance(outfit_name, str):
                schema_diagnostic(node, "detail_name_missing")
                raise OutfitError("unsupported_outfit_fields")
            if outfit_name.strip().casefold() != target:
                continue
            if selected is not None:
                raise OutfitError("duplicate_outfit_name")
            if not isinstance(data.get("pids"), list):
                schema_diagnostic(node, "detail_products_missing")
            selected = {"name": outfit_name.strip(), "id": reference, "products": product_ids(data.get("pids"))}
        if len(items) < 25:
            if selected is None:
                raise OutfitError("named_outfit_not_found")
            return selected
        if new_count == 0:
            raise OutfitError("outfit_pagination_not_advancing")
    raise OutfitError("outfit_search_limit_reached")
