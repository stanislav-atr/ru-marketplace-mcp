"""Lamoda MCP connector.

Lamoda's anti-bot wall splits the catalog in two. One channel answers plain
anonymous HTTPS from some networks: the GraphQL product endpoint, which
enriches a SKU you already have with price, brand and sizes. Everything that
would let you *find* a SKU — search, catalog, HTML pages — sits behind the same
self-referential 307 redirect loop as Ozon, so discovery runs in the operator's
Chrome over CDP.

Verified live July 2026 from a datacenter IP (docs/ANTI_BOT.md):
  - ``POST /goapi/v2/catalog/graphql/products/`` — 200, real JSON (tier 1)
  - catalog/search GET paths — 307 loop; HTML — 403; mobile API — 403
Re-verified 2026-09-24 from the operator's machine: GraphQL answered 403
("Запрос отклонен") while the same host rendered fine in Chrome, so the card
falls back to the product page and the selfcheck accepts either tier.

In Chrome the connector reads the pages' Nuxt state (``catalog.py``) rather
than the DOM: it carries brand, colour family, sizes, photos and the site's own
filters. The DOM tile extractor below remains the fallback for a page without
that state. Review ratings exist only on the product page.

NEVER write to stdout in a stdio MCP server — it corrupts JSON-RPC.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import re
import urllib.parse
from collections import OrderedDict
from typing import Annotated, Any, Literal

import httpx
from fastmcp import Context, FastMCP
from fastmcp.server.middleware.error_handling import RetryMiddleware
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent, ToolAnnotations
from mcp_core import resilience as R
from mcp_core.cache import TTLCache
from mcp_core.dom import JS_HELPERS, prices_from_tile, title_from_tile
from mcp_core.errors import (
    BadRequestError,
    ChallengeRequiredError,
    NotFoundError,
    ParserDriftError,
    ToolError,
    TransportDownError,
    raise_tool_error,
)
from mcp_core.logging import log_event
from mcp_core.output_schema import apply_compact_output_schemas
from mcp_core.pacing import Pacer
from mcp_core.redact import redact_error_text as _redact
from mcp_core.runtime import browser_handoff_lifespan, current_mcp_session_id
from mcp_core.transport import build_client
from mcp_core.transport.browser_handoff import get_handoff_id, has_pending_handoff, read_with_handoff
from mcp_core.transport.chrome_cdp import NavBlocked, open_page
from pydantic import Field

from lamoda_connector import catalog
from lamoda_connector.models_output import (
    LamodaCardResponse,
    LamodaFacetOut,
    LamodaFacetsOut,
    LamodaSearchItemOut,
    LamodaSearchResponse,
    LamodaSelfcheckResponse,
    LamodaSizeOut,
    MetaOut,
)
from lamoda_connector.settings import get_settings
from lamoda_connector.shape_reference import SEARCH_SHAPE_REFERENCE, missing_required_families

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.lamoda.ru"
GRAPHQL_URL = f"{SITE_BASE}/goapi/v2/catalog/graphql/products/"

TIMEOUT = _settings.timeout
MAX_BODY_BYTES = _settings.max_body_bytes
_min_gap = _settings.min_gap

# SKUs look like MP002XM1RMM3: two letters, alphanumerics, 8-20 chars. URLs
# carry them lowercased, so match case-insensitively and normalise to upper.
_SKU_RE = re.compile(r"\b([a-zA-Z]{2}[a-zA-Z0-9]{6,18})\b")

# Field names verified against the live endpoint, July 2026. `old_price_amount`
# is the real name: asking for `old_price` answers HTTP 200 with
# {"error": "Internal server error", "code": -32603} and no data at all.
_GRAPHQL_QUERY = (
    "query { products(skus: [%s]) { sku name brand_name price_amount old_price_amount "
    "is_available is_sellable stock_remains sizes { size is_available stock_remains } } }"
)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="lamoda-connector",
    lifespan=browser_handoff_lifespan,
    version=SERVER_VERSION,
    instructions=(
        "Lamoda fashion catalog, read through the operator's Chrome over CDP. "
        "lamoda_search filters by gender, colour, brand and size and returns brand, "
        "colour, sizes in stock and photo URLs; lamoda_images shows photos to judge "
        "style; lamoda_card(detail=true) gives composition, measurements and other "
        "colours. For a capsule, search each garment slot separately."
    ),
)
mcp.add_middleware(RetryMiddleware())

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap)
_cdp_lock = asyncio.Lock()


def _proxy() -> str | None:
    return _settings.proxy.get_secret_value() or None


async def _polite_wait() -> None:
    """Space this source's requests out, and back off if it refused us.

    Reads ``_min_gap`` at call time so an operator or a test can retune the
    pace without rebuilding the pacer.
    """
    await _pacer.wait(min_gap=_min_gap)


def _extract_sku(raw: str) -> str | None:
    """Pull a SKU out of a lamoda.ru URL or a bare SKU string.

    URLs carry the SKU lowercased (``/p/mp002xm1rmm3/``); the GraphQL endpoint
    expects the canonical uppercase form.
    """
    raw = raw.strip()
    match = _SKU_RE.search(raw)
    return match.group(1).upper() if match else None


_SEARCH_EXTRACT_TEMPLATE = """
() => {
    //__SHARED_HELPERS__
    const ID_RE = /\\/p\\/([a-zA-Z]{2}[a-zA-Z0-9]{6,18})/i;
    // A node whose whole text is a discount percentage ("−49%") is a badge,
    // never a name. Lamoda renders that badge INSIDE the image anchor, which
    // is how an anchor-first title read reported the discount as the title
    // (live capture 2026-08-07, tests/fixtures/search_grid_live.html).
    const BADGE_RE = /^[−\\-]?\\d+\\s*%$/;
    const out = [];
    const anchors = document.querySelectorAll('a[href*="/p/"]');
    const seen = new Set();
    for (const a of anchors) {
        const href = a.href || '';
        const m = href.match(ID_RE);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        // tileRootFor, not closest(): closest() tests the anchor itself first, so
        // an image/overlay link whose own class contains "card" or "product"
        // becomes the tile and reads as empty. See mcp_core.dom.
        const card = tileRootFor(a, ID_RE);
        // The product name first, then weaker name/title/brand candidates, the
        // anchor text only as a last resort. Selector classes are checked in
        // priority order, not document order: the badge span sits earliest in
        // the tile DOM and would win a document-order walk.
        let titleEl = null;
        for (const sel of ['[class*="product-name"]', '[class*="name"]', '[class*="title"]', '[class*="brand"]']) {
            for (const cand of card.querySelectorAll(sel)) {
                const t = cleanText(cand);
                if (t && !BADGE_RE.test(t)) { titleEl = cand; break; }
            }
            if (titleEl) break;
        }
        if (!titleEl) titleEl = a;
        const title = cleanText(titleEl) || (a.getAttribute('title') || null);
        // Scope the price hunt to the price block when the layout names one:
        // the live size grid feeds concatenated digit blobs ("3535,53636,5...")
        // into the weak candidates, and the largest of them gets promoted to
        // the strikethrough — a 3.5e16-rouble "old price". Same framing
        // doctrine as the Citilink PriceBlock scope.
        const priceRoot = card.querySelector('[class*="price-wrap"]') || card;
        out.push({
            sku: m[1],
            title: title,
            brand: null,
            price_texts: priceTextsIn(priceRoot),
            url: href
        });
        if (out.length >= 48) break;
    }
    // Challenge words also occur in scripts, hidden anti-bot widgets and even
    // product copy. Keep this as raw transport data; the Python classifier
    // decides whether an empty page is a challenge. In particular, never let
    // a hidden/script marker override a healthy product grid.
    const bodyCopy = document.body ? document.body.cloneNode(true) : null;
    if (bodyCopy) {
        for (const node of bodyCopy.querySelectorAll('script, style, noscript, [hidden]')) node.remove();
        for (const node of bodyCopy.querySelectorAll('[style]')) {
            const style = node.getAttribute('style') || '';
            if (/display\\s*:\\s*none|visibility\\s*:\\s*hidden/i.test(style)) node.remove();
        }
    }
    const bodyText = (bodyCopy && bodyCopy.textContent || '').replace(/\\s+/g, ' ').trim();
    return JSON.stringify({items: out, title: document.title || '', body_snippet: bodyText.slice(0, 2000)});
}
"""

# Spliced rather than duplicated: one fix to tile resolution or price selection
# lands on every CDP source at once.
_SEARCH_EXTRACT_JS = _SEARCH_EXTRACT_TEMPLATE.replace("//__SHARED_HELPERS__", JS_HELPERS)


# The extractor only transports a bounded visible-text sample. Keep challenge
# wording in one Python classifier so product copy and hidden widgets cannot
# change the verdict while a healthy grid is present. ``бота`` on its own is
# deliberately absent: it occurs in ordinary product copy and is not evidence
# of a challenge.
_CHALLENGE_TEXT_RE = re.compile(r"провер|не\s+робот|captcha|are\s+you\s+human|access\s+denied", re.IGNORECASE)


def _anti_bot_challenge(data: dict[str, Any]) -> bool:
    """Return whether an empty rendered page is an anti-bot challenge.

    ``__BLOCKED__`` is retained only for the legacy cached payload shape and
    only when it carried no items. Current extractor payloads carry a raw
    ``body_snippet`` and the wording is classified here in Python.
    """
    items = data.get("items")
    if isinstance(items, list) and items:
        return False
    state = data.get("state")
    if isinstance(state, dict) and state.get("products"):
        return False
    if str(data.get("title") or "") == "__BLOCKED__":
        return True
    snippet = data.get("body_snippet")
    return isinstance(snippet, str) and bool(_CHALLENGE_TEXT_RE.search(snippet))


def _search_item_from_tile(tile: dict[str, Any]) -> LamodaSearchItemOut:
    """Map one extracted tile onto the wire shape, parsing prices in Python.

    Accepts the numeric ``price_rub`` an older build cached alongside the current
    ``price_texts`` candidates; the wire shape does not change either way.
    """
    price, old_price = prices_from_tile(tile)
    return LamodaSearchItemOut(
        sku=tile.get("sku"),
        title=title_from_tile(tile),
        brand=title_from_tile({"title": tile.get("brand")}),
        price_rub=price,
        old_price_rub=old_price,
        url=tile.get("url"),
    )


async def _graphql_card(sku: str, ctx: Context | None) -> dict[str, Any]:
    """Tier-1: POST the SKU-enrichment query over plain HTTPS."""
    cache_key = f"graphql:{sku}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    await _polite_wait()
    query = _GRAPHQL_QUERY % json.dumps(sku)
    try:
        # Requests that work in a browser carry the product page as Referer.
        # Tested against the live endpoint it made no difference either way, so
        # this is alignment with a known-good request rather than a fix — kept
        # because it costs nothing and removes one variable.
        headers = dict(_HEADERS)
        headers["Referer"] = f"{SITE_BASE}/p/{sku.lower()}/"
        async with build_client(timeout_s=TIMEOUT, headers=headers, proxy=_proxy()) as client:
            response = await client.post(GRAPHQL_URL, json={"query": query})
    except httpx.TransportError as exc:
        raise_tool_error(TransportDownError(f"Lamoda GraphQL unreachable: {exc}"))
    if response.status_code != 200:
        raise_tool_error(
            TransportDownError(
                f"Lamoda GraphQL answered HTTP {response.status_code}. "
                f"Body preview: {_redact(response.text[:200]) or '<empty>'}"
            )
        )
    if len(response.content) > MAX_BODY_BYTES:
        raise_tool_error(TransportDownError("GraphQL body over the cap"))
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise_tool_error(ParserDriftError(f"non-JSON GraphQL body; preview: {response.text[:200]}"))
    # Lamoda does not use the standard GraphQL envelope. Captured live in July
    # 2026 against the real endpoint:
    #   found      {"error": null, "result": [{…}]}
    #   unknown    {"error": null, "result": null}
    #   bad field  {"error": "Internal server error", "code": -32603}
    # The products live under `result`, not `data.products`, and a failure comes
    # back as a single `error` string rather than an `errors` array. Reading the
    # standard shape meant every response looked like drift.
    upstream_error = payload.get("error")
    if isinstance(upstream_error, str) and upstream_error:
        code = payload.get("code")
        suffix = f" (code {code})" if code is not None else ""
        raise_tool_error(
            ParserDriftError(
                f"Lamoda GraphQL rejected the query: {upstream_error[:160]}{suffix}. "
                "A wrong field name in the query is the usual cause."
            )
        )
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        # Kept as a fallback in case Lamoda ever moves to the standard shape.
        first = errors[0] if isinstance(errors[0], dict) else {}
        detail = str(first.get("message") or errors[0])[:200]
        raise_tool_error(ParserDriftError(f"Lamoda GraphQL rejected the query: {detail}"))

    products = payload.get("result")
    if products is None:
        data = payload.get("data")
        products = data.get("products") if isinstance(data, dict) else None
    if products is None:
        # `result: null` is how an unknown SKU comes back — a real not-found,
        # not a broken parser.
        raise_tool_error(NotFoundError(f"Lamoda SKU {sku} returned no product."))
    if not isinstance(products, list):
        raise_tool_error(ParserDriftError(f"GraphQL result is {type(products).__name__}, expected a list"))
    if not products:
        raise_tool_error(NotFoundError(f"Lamoda SKU {sku} returned no product."))
    product = products[0]
    if not isinstance(product, dict):
        raise_tool_error(ParserDriftError("GraphQL product entry is not an object"))
    _cache.set(cache_key, product)
    return product


async def _read_state(page, script: str) -> Any:
    """Evaluate a Nuxt-state reader; None when the page has no state or it is unreadable.

    The state is the richer source but not the only one, so its absence is a
    fallback signal for the caller, never an error by itself.
    """
    try:
        raw = await asyncio.wait_for(page.evaluate(script), timeout=30.0)
    except Exception as exc:
        log_event("lamoda.state_unreadable", error=_redact(str(exc))[:120], exc_type=type(exc).__name__)
        return None
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY_BYTES:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _cdp_render_search(query: str, ctx: Context | None, url: str | None = None) -> dict[str, Any]:
    """Tier-2: render the search page in the operator's Chrome, extract tiles and state.

    ``url`` carries the filters (see ``catalog.build_search_url``); a bare query
    renders the plain search page.
    """
    url = url or f"{SITE_BASE}/catalogsearch/result/?q={urllib.parse.quote(query)}"
    cache_key = f"search:{url}"
    scope = current_mcp_session_id(ctx)
    pending = has_pending_handoff(scope=scope, operation="lamoda_search", url=url)
    cached = None if pending else _cache.get(cache_key)
    if cached is not None:
        return cached

    async def _attempt() -> dict[str, Any]:
        async def read(page):
            raw = await asyncio.wait_for(page.evaluate(_SEARCH_EXTRACT_JS), timeout=30.0)
            if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY_BYTES:
                raise_tool_error(TransportDownError("extracted page data missing or over the body cap"))
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise_tool_error(ParserDriftError("search extractor returned a non-object payload"))
            data.pop("_handoff_expires_at", None)
            data.pop("_handoff_id", None)
            state = await _read_state(page, catalog.SEARCH_STATE_JS)
            # Only a real Nuxt search state counts; anything else is "no state".
            data["state"] = state if isinstance(state, dict) and isinstance(state.get("products"), list) else None
            return data

        async with _cdp_lock:
            await _polite_wait()
            if scope is None:
                async with open_page(url, wait_ms=8000) as page:
                    return await read(page)
            note: dict = {}
            data, expires_at = await read_with_handoff(
                url=url,
                wait_ms=8000,
                scope=scope,
                operation="lamoda_search",
                read=read,
                challenge=lambda payload: "captcha" if _anti_bot_challenge(payload) else None,
                note_out=note,
            )
            if note.get("resumed"):
                # R3: a resumed read states what changed (challenge cleared, data
                # moved) rather than making the caller diff two payloads.
                data["_resume"] = note
            if expires_at:
                data["_handoff_expires_at"] = expires_at
                data["_handoff_id"] = get_handoff_id(scope=scope, operation="lamoda_search", url=url)
            return data

    try:
        payload = await asyncio.wait_for(_attempt(), timeout=max(0.01, float(TIMEOUT)))
    except TimeoutError:
        raise_tool_error(TransportDownError(f"CDP timeout after {TIMEOUT}s"))
    if not _anti_bot_challenge(payload):
        _cache.set(cache_key, payload)
    return payload


# What search and card have seen, so lamoda_images can show a SKU without
# another page load. Bounded: an MCP session can browse thousands of SKUs.
_SEEN_MAX = 3000
_seen: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _remember(sku: str, **fields: Any) -> None:
    key = sku.upper()
    entry = _seen.pop(key, {})
    entry.update({k: v for k, v in fields.items() if v not in (None, [], "")})
    _seen[key] = entry
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)


def _facets_out(facets: dict[str, Any]) -> LamodaFacetsOut:
    def to_out(entries: list[tuple[str, str, int | None]]) -> list[LamodaFacetOut]:
        return [LamodaFacetOut(id=key, title=title, count=count) for key, title, count in entries]

    brands = sorted(catalog.facet_values(facets.get("brands")), key=lambda e: -(e[2] or 0))[:15]
    return LamodaFacetsOut(
        colors=to_out(catalog.facet_values(facets.get("colors"))),
        sizes=[
            LamodaFacetOut(id=key, title=key, count=count)
            for key, _, count in catalog.facet_values(facets.get("sizes"))
        ],
        brands=to_out(brands),
    )


def _clean_list(values: list[str] | None, limit: int, what: str) -> list[str]:
    cleaned = [v.strip() for v in values or [] if isinstance(v, str) and v.strip()]
    if len(cleaned) > limit:
        raise_tool_error(BadRequestError(f"at most {limit} {what} per search; got {len(cleaned)}"))
    return cleaned


@mcp.tool(
    name="lamoda_search",
    annotations=ToolAnnotations(
        title="Lamoda Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def lamoda_search(
    query: Annotated[
        str, Field(min_length=1, max_length=200, description="Search text in Russian, e.g. 'бомбер', 'джинсы прямые'")
    ],
    gender: Annotated[
        Literal["men", "women"] | None,
        Field(description="Scope to Lamoda's men's or women's catalogue. Omit for both."),
    ] = None,
    colors: Annotated[
        list[str] | None,
        Field(
            description="Colour families: Lamoda titles ('синий', 'хаки'), English ('navy', 'olive') or facet IDs. "
            "Olive is filed under хаки. Several are OR-ed.",
            max_length=6,
        ),
    ] = None,
    brands: Annotated[
        list[str] | None,
        Field(description="Brand names or facet IDs, OR-ed; names resolve through the brand facet.", max_length=8),
    ] = None,
    sizes: Annotated[
        list[str] | None,
        Field(
            description="Russian sizes as the size facet lists them ('48', '50'); keeps items with one in stock.",
            max_length=6,
        ),
    ] = None,
    sort: Annotated[
        Literal["default", "new", "price_asc", "price_desc", "discount"], Field(description="Result order.")
    ] = "default",
    page: Annotated[int, Field(ge=1, le=50, description="Result page; Lamoda pages hold 60 products.")] = 1,
    limit: Annotated[int, Field(ge=1, le=60, description="Items to return from the page.")] = 30,
    ctx: Context | None = None,
) -> LamodaSearchResponse:
    """Search Lamoda's catalogue with its own filters, rendered in the operator's Chrome.

    Items carry brand, colour family, sizes in stock and a photo URL, so a
    shortlist can be built without opening each card. To judge style, pass
    candidate SKUs to lamoda_images. For a capsule, search each garment slot
    separately (gender + colors + sizes), then confirm finalists with
    lamoda_card(detail=True) for composition, measurements and other colours.

    ## Return Format

    LamodaSearchResponse: {status, query, tier_used, count, total_found, page,
    pages, filters_applied, facets{colors,sizes,brands}, items[], meta}. Items:
    sku, title, brand, color, price_rub (everyday; None when absent — never 0),
    old_price_rub, loyalty_price_rub (Lamoda Club only), in_stock,
    sizes_in_stock, image_url, url. `facets` lists refinements with counts.

    ## Error Format

    ToolError: bad_request for an unknown colour or malformed size;
    challenge_required on a visible challenge (not cached); transport_down on
    CDP/nav failures; parser_drift when a rendered page yields zero SKUs
    without challenge evidence.
    """
    log_event("lamoda_search.start", query=query[:60], gender=gender, colors=colors, page=page)
    try:
        warnings: list[str] = []
        color_ids: list[str] = []
        color_titles: list[str] = []
        for raw in _clean_list(colors, 6, "colours"):
            resolved = catalog.resolve_color(raw)
            if resolved is None:
                raise_tool_error(
                    BadRequestError(
                        f"Lamoda has no colour family for {raw!r}. Known: {', '.join(catalog.COLOR_IDS)} "
                        "(English names such as navy, olive, grey also work)."
                    )
                )
            if resolved[0] not in color_ids:
                color_ids.append(resolved[0])
                color_titles.append(resolved[1])
        size_values: list[str] = []
        for raw in _clean_list(sizes, 6, "sizes"):
            value = catalog.valid_size(raw)
            if value is None:
                raise_tool_error(BadRequestError(f"size {raw!r} is not a size label; use values like '48' or 'M'"))
            size_values.append(value)
        brand_inputs = _clean_list(brands, 8, "brands")
        brand_ids = [b for b in brand_inputs if catalog.is_filter_id(b)]
        brand_names = [b for b in brand_inputs if not catalog.is_filter_id(b)]
        unresolved: list[str] = []
        text = query.strip()

        def url_for(ids: list[str]) -> str:
            return catalog.build_search_url(
                text,
                gender=gender,
                color_ids=color_ids,
                brand_ids=ids,
                sizes=size_values,
                sort=sort,
                page=page,
            )

        try:
            if brand_names:
                # Brand IDs live only in the page's own facet: read it from the
                # same query without the brand filter, then ask again with IDs.
                probe = await _cdp_render_search(text, ctx, url=url_for(brand_ids))
                raw_state = probe.get("state")
                probe_state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
                resolved_ids, unresolved = catalog.resolve_brands(
                    brand_names, catalog.facet_values((probe_state.get("facets") or {}).get("brands"))
                )
                if unresolved:
                    warnings.append(f"brands_not_in_results: {', '.join(unresolved)}")
                brand_ids = brand_ids + resolved_ids
                if not brand_ids:
                    raise_tool_error(
                        NotFoundError(
                            f"none of the brands {', '.join(brand_names)} appear for this query and filters on Lamoda"
                        )
                    )
            url = url_for(brand_ids)
            payload = await _cdp_render_search(text, ctx, url=url)
        except NavBlocked as exc:
            raise_tool_error(TransportDownError(f"Lamoda navigation blocked (HTTP {exc.status})."))
        if _anti_bot_challenge(payload):
            raise_tool_error(
                ChallengeRequiredError(
                    "Lamoda requires user action in the connected Chrome. Complete the visible challenge, then retry.",
                    provider="lamoda",
                    handoff_expires_at=payload.get("_handoff_expires_at"),
                    handoff_id=payload.get("_handoff_id"),
                )
            )

        applied: dict[str, list[str] | str] = {}
        if gender:
            applied["gender"] = gender
        if color_titles:
            applied["colors"] = color_titles
        if brand_ids:
            applied["brands"] = [b for b in brand_inputs if b not in unresolved]
        if size_values:
            applied["sizes"] = size_values
        if sort != "default":
            applied["sort"] = sort

        state = payload.get("state") if isinstance(payload.get("state"), dict) else None
        products = [p for p in (state or {}).get("products") or [] if isinstance(p, dict) and p.get("sku")]
        if state is not None and not products:
            # The state answered and says: nothing matches. With filters that is
            # a real empty result, not drift.
            result = LamodaSearchResponse(
                query=query, tier_used="cdp", count=0, total_found=0, page=page, filters_applied=applied
            )
            attached = R.attach_meta(
                result.model_dump(by_alias=True, exclude={"meta"}), [*warnings, "no_results"], source="lamoda_search"
            )
            result.meta = MetaOut(**attached["_meta"])
            return result

        if products:
            rows = [catalog.search_item(p) for p in products]
            for row in rows:
                row.pop("_gallery", None)
            items = [LamodaSearchItemOut(**row) for row in rows[:limit]]
            for row, product in zip(rows, products, strict=False):
                _remember(
                    row["sku"],
                    title=row["title"],
                    brand=row["brand"],
                    color=row["color"],
                    price_rub=row["price_rub"],
                    gallery=[p for p in product.get("gallery") or [] if isinstance(p, str)],
                )
            pagination = state.get("pagination") if isinstance(state, dict) else None
            pagination = pagination if isinstance(pagination, dict) else {}
            result = LamodaSearchResponse(
                query=query,
                tier_used="cdp",
                count=len(items),
                total_found=R.coerce_int(pagination.get("found")),
                page=R.coerce_int(pagination.get("page")) or page,
                pages=R.coerce_int(pagination.get("pages")),
                filters_applied=applied,
                facets=_facets_out(state.get("facets") or {}) if isinstance(state, dict) else None,
                items=items,
            )
        else:
            # No Nuxt state: the DOM tiles still carry sku/title/price.
            items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
            if not items_raw:
                raise_tool_error(
                    ParserDriftError(
                        "rendered search page yielded zero SKUs — either the query matched nothing or the tile shape moved; verify manually"
                    )
                )
            items = [_search_item_from_tile(it) for it in items_raw if isinstance(it, dict)][:limit]
            if applied:
                warnings.append("filters_unconfirmed: page state missing, filters may not have applied")
            warnings.append("dom_fallback: no brand, colour, sizes or photos")
            result = LamodaSearchResponse(
                query=query, tier_used="cdp", count=len(items), page=page, filters_applied=applied, items=items
            )
        if items and all(it.price_rub is None for it in items):
            warnings.append("no_prices_on_page")
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="lamoda_search")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except Exception as exc:
        log_event("lamoda_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"lamoda_search failed: {exc}")))


async def _cdp_card(sku: str, ctx: Context | None) -> dict[str, Any]:
    """Product-page tier: read the card's Nuxt state in the operator's Chrome."""
    cache_key = f"card-page:{sku}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    url = catalog.product_url(sku)

    async def _attempt() -> Any:
        async with _cdp_lock:
            await _polite_wait()
            async with open_page(url, wait_ms=6000) as page:
                state = await _read_state(page, catalog.CARD_STATE_JS)
                if isinstance(state, dict):
                    return state
                body = await asyncio.wait_for(
                    page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 2000)"),
                    timeout=10.0,
                )
                return {"_no_state": True, "body_snippet": body if isinstance(body, str) else ""}

    try:
        data = await asyncio.wait_for(_attempt(), timeout=max(0.01, float(TIMEOUT)))
    except TimeoutError:
        raise_tool_error(TransportDownError(f"CDP timeout after {TIMEOUT}s"))
    except NavBlocked as exc:
        if exc.status == 404:
            raise_tool_error(NotFoundError(f"Lamoda has no product page for {sku}."))
        raise_tool_error(TransportDownError(f"Lamoda navigation blocked (HTTP {exc.status})."))
    if data.get("_no_state"):
        if _CHALLENGE_TEXT_RE.search(str(data.get("body_snippet") or "")):
            raise_tool_error(
                ChallengeRequiredError(
                    "Lamoda requires user action in the connected Chrome. Complete the visible challenge, then retry.",
                    provider="lamoda",
                )
            )
        raise_tool_error(ParserDriftError(f"product page for {sku} carried no product state"))
    if str(data.get("sku") or "").upper() != sku:
        raise_tool_error(ParserDriftError(f"product page for {sku} described {data.get('sku')!r}"))
    _cache.set(cache_key, data)
    return data


@mcp.tool(
    name="lamoda_card",
    annotations=ToolAnnotations(
        title="Lamoda Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def lamoda_card(
    sku_or_url: Annotated[
        str, Field(min_length=1, max_length=300, description="SKU (MP002XM1RMM3) or lamoda.ru product URL")
    ],
    detail: Annotated[
        bool,
        Field(
            description="True reads the product page (colour, photos, composition, size measurements, "
            "other colours, rating); False asks the lighter price/sizes endpoint first."
        ),
    ] = False,
    ctx: Context | None = None,
) -> LamodaCardResponse:
    """Fetch one Lamoda product card: price, per-size availability, and with detail the full page.

    Two tiers. The anonymous GraphQL endpoint gives price, brand and sizes; the
    product page (operator's Chrome) adds colour, gallery, description,
    composition, size measurements, stock counts, the model in other colours
    and the review rating. Either tier falls back to the other when it is
    blocked, and `tier_used` says which answered.

    ## Return Format

    LamodaCardResponse: {status, sku, title, brand, color, price_rub,
    old_price_rub, loyalty_price_rub, is_available, sizes[{size, is_available,
    stock, measurements}], images[], description, attributes{}, rating,
    reviews_count, other_colors[], url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request when no SKU can be extracted; not_found when the SKU
    has no product; transport_down when both tiers are blocked; parser_drift
    when a reached payload changed shape.
    """
    log_event("lamoda_card.start", input=sku_or_url[:80], detail=detail)
    try:
        sku = _extract_sku(sku_or_url)
        if sku is None:
            raise_tool_error(
                BadRequestError(f"could not extract a SKU from {sku_or_url!r}; pass MP… or a lamoda.ru product URL")
            )
        warnings: list[str] = []
        order = ("page", "graphql") if detail else ("graphql", "page")
        last_exc: ToolError | None = None
        for tier in order:
            try:
                if tier == "page":
                    fields = catalog.card_fields(await _cdp_card(sku, ctx))
                    gallery = fields.pop("_gallery")
                    result = LamodaCardResponse(sku=sku, url=catalog.product_url(sku), tier_used="cdp", **fields)
                    _remember(
                        sku,
                        title=result.title,
                        brand=result.brand,
                        color=result.color,
                        price_rub=result.price_rub,
                        gallery=gallery,
                    )
                else:
                    product = await _graphql_card(sku, ctx)
                    sizes_field = product.get("sizes")
                    sizes_raw: list[Any] = sizes_field if isinstance(sizes_field, list) else []
                    result = LamodaCardResponse(
                        sku=sku,
                        title=product.get("name"),
                        brand=product.get("brand_name"),
                        price_rub=R.coerce_price(product.get("price_amount")),
                        old_price_rub=R.coerce_price(R.first_present(product, "old_price_amount", "old_price")),
                        is_available=product.get("is_available"),
                        sizes=[
                            LamodaSizeOut(size=s.get("size"), is_available=s.get("is_available"))
                            for s in sizes_raw
                            if isinstance(s, dict)
                        ],
                        url=catalog.product_url(sku),
                        tier_used="graphql",
                    )
                break
            except ToolError as exc:
                # Only a blocked tier hands over; not_found and drift are answers.
                if "transport_down" not in str(exc) and "challenge_required" not in str(exc):
                    raise
                last_exc = exc
                warnings.append(f"{tier}_tier_unavailable")
                log_event("lamoda_card.tier_failed", tier=tier, error=_redact(str(exc))[:160])
        else:
            assert last_exc is not None
            raise last_exc
        if detail and result.tier_used == "graphql":
            warnings.append("detail_unavailable: product page blocked, price and sizes only")
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="lamoda_card")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except Exception as exc:
        log_event("lamoda_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"lamoda_card failed: {exc}")))


_IMAGE_MAX_BYTES = 400_000


async def _fetch_image(url: str) -> bytes:
    """One CDN photo. The CDN is not the storefront: no pacer, just a size cap."""
    async with build_client(
        timeout_s=TIMEOUT, headers={"User-Agent": _HEADERS["User-Agent"]}, proxy=_proxy()
    ) as client:
        response = await client.get(url)
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    if not response.headers.get("content-type", "").startswith("image/"):
        raise ValueError(f"not an image: {response.headers.get('content-type')}")
    if len(response.content) > _IMAGE_MAX_BYTES:
        raise ValueError("image over the size cap")
    return response.content


@mcp.tool(
    name="lamoda_images",
    annotations=ToolAnnotations(
        title="Lamoda Product Photos", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def lamoda_images(
    skus: Annotated[
        list[str], Field(min_length=1, max_length=12, description="SKUs from lamoda_search or lamoda_card, up to 12.")
    ],
    photos_per_item: Annotated[
        int, Field(ge=1, le=3, description="1 = main photo; 2-3 add the next gallery shots (back, detail).")
    ] = 1,
    size: Annotated[
        Literal["small", "medium"], Field(description="small 236x341 (~13 KB), medium 389x562 (~31 KB).")
    ] = "small",
    ctx: Context | None = None,
) -> ToolResult:
    """Show product photos as images, so a vision model can judge style and colour.

    Titles and colour labels cannot tell a Harrington from a windbreaker; the
    photo can. Each image is preceded by a label '#n SKU brand colour'. SKUs
    already returned by lamoda_search/lamoda_card cost no page load; others
    open their product page first.

    ## Return Format

    MCP content: a JSON index {items[{n, sku, brand, title, color, price_rub,
    url, image_urls}], warnings}, then label + image/jpeg pairs.

    ## Error Format

    ToolError: bad_request for a malformed SKU; transport_down when no photo
    could be fetched at all. A single missing photo is a warning, not an error.
    """
    log_event("lamoda_images.start", count=len(skus), per_item=photos_per_item)
    try:
        wanted: list[str] = []
        for raw in skus:
            sku = _extract_sku(raw)
            if sku is None:
                raise_tool_error(BadRequestError(f"could not extract a SKU from {raw!r}"))
            if sku not in wanted:
                wanted.append(sku)
        warnings: list[str] = []
        for sku in wanted:
            if not _seen.get(sku, {}).get("gallery"):
                try:
                    await lamoda_card(sku, detail=True, ctx=ctx)
                except ToolError as exc:
                    warnings.append(f"{sku}: {str(exc)[:120]}")
        index: list[dict[str, Any]] = []
        jobs: list[tuple[int, str, str]] = []
        for n, sku in enumerate(wanted, start=1):
            entry = _seen.get(sku, {})
            gallery = entry.get("gallery") or []
            urls = [u for u in (catalog.image_url(p, size) for p in gallery[:photos_per_item]) if u]
            index.append(
                {
                    "n": n,
                    "sku": sku,
                    "brand": entry.get("brand"),
                    "title": entry.get("title"),
                    "color": entry.get("color"),
                    "price_rub": entry.get("price_rub"),
                    "url": catalog.product_url(sku),
                    "image_urls": urls,
                }
            )
            jobs.extend((n, sku, u) for u in urls)
            if not urls and not any(w.startswith(sku) for w in warnings):
                warnings.append(f"{sku}: no photos known")

        semaphore = asyncio.Semaphore(4)

        async def fetch(job: tuple[int, str, str]) -> bytes | None:
            async with semaphore:
                try:
                    return await _fetch_image(job[2])
                except Exception as exc:
                    warnings.append(f"{job[1]}: photo unavailable ({_redact(str(exc))[:80]})")
                    return None

        images = await asyncio.gather(*(fetch(job) for job in jobs))
        if jobs and not any(images):
            raise_tool_error(TransportDownError("no Lamoda photo could be fetched from the CDN"))
        structured = {"items": index, "warnings": warnings}
        content: list[TextContent | ImageContent] = [
            TextContent(type="text", text=json.dumps(structured, ensure_ascii=False))
        ]
        by_n = {row["n"]: row for row in index}
        for (n, sku, _url), data in zip(jobs, images, strict=True):
            if data is None:
                continue
            row = by_n[n]
            label = " ".join(str(x) for x in (f"#{n}", sku, row.get("brand"), row.get("color")) if x)
            content.append(TextContent(type="text", text=label))
            content.append(
                ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/jpeg")
            )
        return ToolResult(content=content, structured_content=structured)
    except ToolError:
        raise
    except Exception as exc:
        log_event("lamoda_images.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"lamoda_images failed: {exc}")))


# CLI-only drift canary: ``marketplace-mcp doctor`` imports and calls lamoda_selfcheck()
# directly. It is deliberately NOT registered as an MCP tool — selfchecks are
# operator diagnostics, and their input/output schemas would be billed in every
# client request.
async def lamoda_selfcheck(ctx: Context | None = None) -> LamodaSelfcheckResponse:
    """Structural drift canary for Lamoda (tri-state). Probes the CDP search
    path, then the card: GraphQL first, the product page for a SKU the search
    just returned when GraphQL is refused.

    The card is healthy when either tier answers and inconclusive when both are
    blocked; CDP down / a redirect loop is inconclusive for the search check.
    Only a reached payload that fails its parse smoke is drift.

    ## Return Format

    LamodaSelfcheckResponse: {status, healthy, connector, checks, ...}.

    ## Error Format

    Raises ToolError (TransportDownError) ONLY on an unexpected internal bug
    that prevents the canary from producing any verdict. Transport/block
    failures of individual sub-checks map to inconclusive entries, not errors.
    """
    log_event("lamoda_selfcheck.start")
    try:
        result = await _lamoda_selfcheck_impl(ctx)
        log_event("lamoda_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except Exception as exc:
        log_event("lamoda_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"lamoda_selfcheck failed: {exc}")))


async def _lamoda_selfcheck_impl(ctx: Context | None) -> LamodaSelfcheckResponse:
    checks: dict[str, dict] = {}

    probe_sku: str | None = None

    # CDP tier: search tile extraction.
    baseline = "cdp-search-shape-v1"
    try:
        async with asyncio.timeout(90):
            payload = await _cdp_render_search("кроссовки", ctx)
        state_products = (payload.get("state") or {}).get("products") or []
        first = next(
            (p for p in [*state_products, *(payload.get("items") or [])] if isinstance(p, dict) and p.get("sku")),
            None,
        )
        probe_sku = _extract_sku(str(first["sku"])) if first else None
        if _anti_bot_challenge(payload):
            checks["search"] = R.selfcheck_entry(
                "inconclusive", baseline=baseline, reason="blocked", notes=["anti-bot challenge in rendered page"]
            )
        else:
            items_raw = [*payload.get("items", [])] if isinstance(payload.get("items"), list) else []
            if items_raw:
                # Tiles extract — now ask the second question: did the SHAPE move?
                # The registry was measured on the captured page (2026-08-07); a
                # live payload that loses a parser-critical key family is
                # structural drift even while tiles still come back.
                live_signature = R.shape_signature(payload)
                drift = R.diff_keys(SEARCH_SHAPE_REFERENCE, live_signature)
                missing = missing_required_families(live_signature)
                if missing:
                    checks["search"] = R.selfcheck_entry(
                        "drift",
                        baseline=baseline,
                        reason="shape_drift",
                        notes=[
                            f"{len(items_raw)} tiles extracted",
                            "required key families missing: " + "; ".join(", ".join(family) for family in missing),
                        ],
                        shape_missing=drift["missing"],
                        shape_added=drift["added"],
                    )
                else:
                    notes = [f"{len(items_raw)} tiles extracted", "shape matches the captured reference"]
                    if state_products:
                        notes.append(f"page state: {len(state_products)} products with brand, colour and photos")
                    else:
                        # Search still answers from DOM tiles, but loses brand,
                        # colour, sizes and photos; say so where an operator looks.
                        notes.append("page state missing: search degrades to DOM tiles")
                    if drift["added"]:
                        notes.append(f"{len(drift['added'])} new paths vs baseline (informational)")
                    checks["search"] = R.selfcheck_entry(
                        "healthy", baseline=baseline, notes=notes, shape_added=drift["added"]
                    )
            else:
                checks["search"] = R.selfcheck_entry(
                    "drift", baseline=baseline, reason="parse_smoke_failed", notes=["zero SKUs"]
                )
    except ToolError as exc:
        checks["search"] = R.selfcheck_entry(
            "inconclusive", baseline=baseline, reason="transport_down", notes=[str(exc)[:160]]
        )
    except Exception as exc:
        checks["search"] = R.selfcheck_entry(
            "inconclusive", baseline=baseline, reason="transport_down", notes=[f"{type(exc).__name__}"]
        )

    # Card: either tier answering is a working card. GraphQL is cheap but
    # refused from some networks (HTTP 403 seen 2026-09-24); the product page
    # tier reads a SKU the live search just returned, so it never goes stale.
    card_notes: list[str] = []
    card_state = "inconclusive"
    card_reason: str | None = "transport_down"
    try:
        async with asyncio.timeout(60):
            await _graphql_card("MP002XM1RMM3", ctx)
        card_state, card_reason = "healthy", None
        card_notes.append("graphql: SKU enrichment answered")
    except ToolError as exc:
        if "parser_drift" in str(exc):
            card_state, card_reason = "drift", "parse_smoke_failed"
        card_notes.append(f"graphql: {str(exc)[:120]}")
    except Exception as exc:
        card_notes.append(f"graphql: {type(exc).__name__}")
    if card_state != "healthy":
        if probe_sku is None:
            card_notes.append("product page: no SKU from search to probe")
        else:
            try:
                async with asyncio.timeout(60):
                    fields = catalog.card_fields(await _cdp_card(probe_sku, ctx))
                if fields["price_rub"] is None or not fields["_gallery"]:
                    card_state, card_reason = "drift", "parse_smoke_failed"
                    card_notes.append(f"product page {probe_sku}: state without price or gallery")
                elif card_state != "drift":
                    card_state, card_reason = "healthy", None
                    card_notes.append(f"product page {probe_sku}: price, sizes and gallery read")
            except ToolError as exc:
                if "parser_drift" in str(exc):
                    card_state, card_reason = "drift", "parse_smoke_failed"
                card_notes.append(f"product page: {str(exc)[:120]}")
            except Exception as exc:
                card_notes.append(f"product page: {type(exc).__name__}")
    checks["card"] = R.selfcheck_entry(
        card_state, baseline="graphql-products-v2+product-page-state-v1", reason=card_reason, notes=card_notes
    )

    result_dict = R.selfcheck_result(
        "lamoda",
        checks,
        required=("card", "search"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return LamodaSelfcheckResponse(**result_dict)


# Advertised output schemas are the dominant constant cost of an MCP mount:
# replace the full Pydantic tree with top-level field names (~64 % fewer
# wire tokens on the unified server).
apply_compact_output_schemas(mcp)
