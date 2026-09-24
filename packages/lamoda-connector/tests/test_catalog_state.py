"""Offline tests for the Nuxt-state search, filters, the product-page card and photos.

Fixtures are the real output of ``catalog.SEARCH_STATE_JS`` / ``CARD_STATE_JS``
captured live on 2026-09-24 (provenance alongside), trimmed, never invented.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastmcp.exceptions import ToolError
from lamoda_connector import catalog, server
from mcp.types import ImageContent, TextContent

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH_STATE = json.loads((FIXTURES / "search_state_live.json").read_text(encoding="utf-8"))
CARD_STATE = json.loads((FIXTURES / "card_state_live.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    server._cache._data.clear()
    server._seen.clear()
    monkeypatch.setattr(server, "_min_gap", 0.0)
    server._pacer.reset()
    server._graphql_ok()


def _patch_render(monkeypatch, state, seen_urls):
    async def fake_render(query, ctx, url=None):
        seen_urls.append(url)
        return {"items": [], "title": "", "body_snippet": "", "state": state}

    monkeypatch.setattr(server, "_cdp_render_search", fake_render)


def _code(exc: pytest.ExceptionInfo) -> str:
    return json.loads(str(exc.value))["error"]


# ------------------------------------------------------------------ vocabulary ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("синий", ("643", "синий")),
        ("Синяя", ("643", "синий")),
        ("navy", ("643", "синий")),
        ("тёмно-синий", ("643", "синий")),
        ("olive", ("3847", "хаки")),
        ("оливковые", ("3847", "хаки")),
        ("серая", ("635", "серый")),
        ("серебряный", ("3843", "серебряный")),
        ("Зелёный", ("637", "зеленый")),
        ("643", ("643", "синий")),
    ],
)
def test_colours_resolve_from_russian_forms_english_and_ids(raw, expected):
    assert catalog.resolve_color(raw) == expected


@pytest.mark.parametrize("raw", ["neon", "", "синий'; drop", "c0lor"])
def test_an_unknown_colour_is_refused_not_guessed(raw):
    assert catalog.resolve_color(raw) is None


def test_the_url_carries_gender_path_and_comma_joined_filters():
    url = catalog.build_search_url(
        "бомбер мужской",
        gender="men",
        color_ids=["3847", "637"],
        brand_ids=["18717"],
        sizes=["48", "50"],
        sort="new",
        page=2,
    )
    assert url.startswith("https://www.lamoda.ru/c/4152/default-men/?q=%D0%B1")
    assert "&colors=3847,637&brands=18717&size_values=48,50&sort=new&page=2" in url


def test_default_sort_and_first_page_stay_out_of_the_url():
    url = catalog.build_search_url("кеды")
    assert url == "https://www.lamoda.ru/catalogsearch/result/?q=%D0%BA%D0%B5%D0%B4%D1%8B"


@pytest.mark.parametrize("path", ["../etc/passwd", "https://evil.example/x.jpg", "/R/T/x.svg", None])
def test_only_gallery_paths_become_cdn_urls(path):
    assert catalog.image_url(path) is None


def test_gallery_paths_map_to_the_sizes_the_cdn_serves():
    path = "/R/T/RTLAFO975401_38361358_1_v1_2x.jpg"
    assert catalog.image_url(path) == f"https://a.lmcdn.ru/product{path}"
    assert catalog.image_url(path, "small") == f"https://a.lmcdn.ru/img236x341{path}"


@pytest.mark.parametrize("size", ["48", "M", "44/46", "3XL"])
def test_size_values_pass_a_strict_shape(size):
    assert catalog.valid_size(size) == size


@pytest.mark.parametrize("size", ["", "48&x=1", "4" * 20, "м"])
def test_malformed_sizes_are_refused(size):
    assert catalog.valid_size(size) is None


# ---------------------------------------------------------------------- prices ----


def _product(sku: str) -> dict:
    return next(p for p in SEARCH_STATE["products"] if p["sku"] == sku)


def test_club_price_is_reported_apart_never_as_the_everyday_price():
    row = catalog.search_item(_product("RTLAFO975401"))
    assert (row["price_rub"], row["old_price_rub"], row["loyalty_price_rub"]) == (15999.0, None, 15200.0)


def test_a_public_sale_keeps_the_original_as_old_price():
    # price_amount is the current price; the original is the ladder's first step.
    row = catalog.search_item(_product("MP002XM08CB9"))
    assert (row["price_rub"], row["old_price_rub"]) == (10917.0, 20599.0)


def test_a_promo_layer_is_a_public_price():
    row = catalog.search_item(_product("MP002XM0RTJV"))
    assert (row["price_rub"], row["old_price_rub"], row["loyalty_price_rub"]) == (11985.0, 15980.0, None)


def test_no_ladder_falls_back_to_the_amount_and_zero_is_never_a_price():
    assert catalog.split_prices(4990, []) == (4990.0, None, None)
    assert catalog.split_prices(0, [{"price": "0", "type": None}]) == (None, None, None)


# ---------------------------------------------------------------------- search ----


async def test_search_reads_brand_colour_sizes_and_photo_from_state(monkeypatch):
    urls: list = []
    _patch_render(monkeypatch, SEARCH_STATE, urls)
    result = await server.lamoda_search("бомбер", gender="men", colors=["navy"])
    assert "/c/4152/default-men/" in urls[0] and "colors=643" in urls[0]
    assert result.total_found == 650 and result.pages == 11 and result.page == 1
    assert result.filters_applied == {"gender": "men", "colors": ["синий"]}
    first = result.items[0]
    assert (first.sku, first.brand, first.color) == ("RTLAFO975401", "Lyle & Scott", "синий")
    assert first.image_url == "https://a.lmcdn.ru/product/R/T/RTLAFO975401_38361358_1_v1_2x.jpg"
    assert first.url == "https://www.lamoda.ru/p/rtlafo975401/"
    assert all(item.color == "синий" for item in result.items)
    assert result.facets is not None and any(f.title == "хаки" and f.id == "3847" for f in result.facets.colors)
    assert result.meta.warnings == []


async def test_only_in_stock_sizes_are_listed(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    result = await server.lamoda_search("бомбер")
    raw = _product("RTLAFO975401")["sizes"]
    expected = [catalog.size_label(s[0], s[1]) for s in raw if s[2] is True]
    assert result.items[0].sizes_in_stock == expected
    assert all("(" in s for s in expected)  # Russian size first, the brand label in brackets


async def test_limit_trims_items_but_every_seen_sku_is_remembered_for_photos(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    result = await server.lamoda_search("бомбер", limit=2)
    assert result.count == 2
    assert len(server._seen) == len(SEARCH_STATE["products"])
    assert server._seen["MP002XM08CBB"]["gallery"]


async def test_an_unknown_colour_is_a_bad_request_before_any_page_load(monkeypatch):
    urls: list = []
    _patch_render(monkeypatch, SEARCH_STATE, urls)
    with pytest.raises(ToolError) as exc:
        await server.lamoda_search("бомбер", colors=["neon"])
    assert _code(exc) == "bad_request" and "хаки" in str(exc.value)
    assert urls == []


async def test_a_malformed_size_is_a_bad_request(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    with pytest.raises(ToolError) as exc:
        await server.lamoda_search("бомбер", sizes=["48&page=9"])
    assert _code(exc) == "bad_request"


async def test_brand_names_resolve_through_the_facet_then_filter(monkeypatch):
    urls: list = []
    _patch_render(monkeypatch, SEARCH_STATE, urls)
    brand = SEARCH_STATE["facets"]["brands"][0]
    result = await server.lamoda_search("бомбер", brands=[brand[1].upper(), "No Such Brand"])
    assert "brands=" not in urls[0]
    assert f"brands={brand[0]}" in urls[1]
    assert result.filters_applied["brands"] == [brand[1].upper()]
    assert "brands_not_in_results: No Such Brand" in result.meta.warnings


async def test_no_matching_brand_is_not_found_not_an_unfiltered_search(monkeypatch):
    urls: list = []
    _patch_render(monkeypatch, SEARCH_STATE, urls)
    with pytest.raises(ToolError) as exc:
        await server.lamoda_search("бомбер", brands=["No Such Brand"])
    assert _code(exc) == "not_found"
    assert len(urls) == 1


async def test_a_state_with_no_products_is_an_empty_result_not_drift(monkeypatch):
    empty = {"products": [], "pagination": {"page": 1, "pages": 0, "found": 0}, "facets": {}}
    _patch_render(monkeypatch, empty, [])
    result = await server.lamoda_search("бомбер", colors=["хаки"], sizes=["78"])
    assert result.count == 0 and result.total_found == 0
    assert "no_results" in result.meta.warnings


# ------------------------------------------------------------------------ card ----


def _patch_card_page(monkeypatch, state, calls):
    async def fake_cdp_card(sku, ctx):
        calls.append(sku)
        return state

    monkeypatch.setattr(server, "_cdp_card", fake_cdp_card)


async def test_detail_card_reads_the_product_page(monkeypatch):
    calls: list = []
    _patch_card_page(monkeypatch, CARD_STATE, calls)
    card = await server.lamoda_card("https://www.lamoda.ru/p/rtlaej669901/", detail=True)
    assert calls == ["RTLAEJ669901"] and card.tier_used == "cdp"
    assert (card.brand, card.color) == ("Fred Perry", "синий")
    assert (card.price_rub, card.old_price_rub, card.loyalty_price_rub) == (33050.0, 47899.0, 31398.0)
    assert card.images and all(u.startswith("https://a.lmcdn.ru/product/") for u in card.images)
    assert card.attributes["Сезон"] == "демисезон, лето"
    first, second = card.sizes[0], card.sizes[1]
    assert (first.size, first.stock, first.is_available) == ("44/46 (XS)", 0, False)
    assert first.measurements is None  # fit data only for a size one can order
    assert (second.size, second.is_available) == ("46/48 (S)", True)
    assert second.measurements == "Обхват груди 92-96 см"
    assert "Цвет" not in card.attributes and "Артикул" not in card.attributes
    assert len(card.images) <= 3
    assert server._seen["RTLAEJ669901"]["gallery"]


async def test_a_blocked_graphql_falls_back_to_the_page(monkeypatch):
    async def blocked(sku, ctx):
        server.raise_tool_error(server.TransportDownError("Lamoda GraphQL answered HTTP 403."))

    monkeypatch.setattr(server, "_graphql_card", blocked)
    _patch_card_page(monkeypatch, CARD_STATE, [])
    card = await server.lamoda_card("RTLAEJ669901")
    assert card.tier_used == "cdp" and card.price_rub == 33050.0
    assert card.meta.warnings == ["graphql_tier_unavailable"]


async def test_not_found_is_an_answer_and_does_not_fall_back(monkeypatch):
    async def missing(sku, ctx):
        server.raise_tool_error(server.NotFoundError("no product"))

    calls: list = []
    monkeypatch.setattr(server, "_graphql_card", missing)
    _patch_card_page(monkeypatch, CARD_STATE, calls)
    with pytest.raises(ToolError) as exc:
        await server.lamoda_card("RTLAEJ669901")
    assert _code(exc) == "not_found" and calls == []


async def test_detail_falls_back_to_graphql_and_says_detail_is_missing(monkeypatch):
    async def blocked_page(sku, ctx):
        server.raise_tool_error(server.TransportDownError("CDP unavailable"))

    async def graphql(sku, ctx):
        return {"sku": sku, "name": "Ветровка", "brand_name": "Fred Perry", "price_amount": 33050, "sizes": []}

    monkeypatch.setattr(server, "_cdp_card", blocked_page)
    monkeypatch.setattr(server, "_graphql_card", graphql)
    card = await server.lamoda_card("RTLAEJ669901", detail=True)
    assert card.tier_used == "graphql"
    assert "detail_unavailable: product page blocked, price and sizes only" in card.meta.warnings


async def test_a_page_navigation_timeout_is_transport_down_and_hands_over(monkeypatch):
    """Captured live 2026-09-24: Page.goto timed out and, unconverted, skipped the
    GraphQL fallback and surfaced as a generic failure."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def slow_page(url, wait_ms):
        raise TimeoutError("Page.goto: Timeout 20000ms exceeded.")
        yield

    async def graphql(sku, ctx):
        return {"sku": sku, "name": "Бомбер", "brand_name": "Lyle & Scott", "price_amount": 15999, "sizes": []}

    monkeypatch.setattr(server, "open_page", slow_page)
    monkeypatch.setattr(server, "_graphql_card", graphql)
    with pytest.raises(ToolError) as exc:
        await server._cdp_card("RTLAFO975401", None)
    assert _code(exc) == "transport_down"
    card = await server.lamoda_card("RTLAFO975401", detail=True)
    assert card.tier_used == "graphql" and "page_tier_unavailable" in card.meta.warnings


async def test_parallel_cards_queue_for_chrome_without_timing_out(monkeypatch):
    """Captured live 2026-09-24: six parallel detail cards, the later ones timed
    out while waiting for the Chrome lock, before ever navigating."""
    import asyncio
    from contextlib import asynccontextmanager

    class Page:
        def __init__(self, sku):
            self.sku = sku

        async def evaluate(self, expression):
            return json.dumps({**CARD_STATE, "sku": self.sku})

    @asynccontextmanager
    async def page_taking_a_while(url, wait_ms):
        await asyncio.sleep(0.05)
        yield Page(url.rstrip("/").rsplit("/", 1)[-1].upper())

    monkeypatch.setattr(server, "open_page", page_taking_a_while)
    monkeypatch.setattr(server, "TIMEOUT", 0.12)  # each page fits; the queue of five does not
    skus = [f"RTLAEJ66990{i}" for i in range(5)]
    states = await asyncio.gather(*(server._cdp_card(sku, None) for sku in skus))
    assert [s["sku"] for s in states] == skus


async def test_when_every_tier_fails_each_reason_is_reported(monkeypatch):
    async def page_timeout(sku, ctx):
        server.raise_tool_error(server.TransportDownError("product page unreachable: TimeoutError"))

    async def graphql_403(sku, ctx):
        server.raise_tool_error(server.TransportDownError("Lamoda GraphQL answered HTTP 403."))

    monkeypatch.setattr(server, "_cdp_card", page_timeout)
    monkeypatch.setattr(server, "_graphql_card", graphql_403)
    with pytest.raises(ToolError) as exc:
        await server.lamoda_card("RTLAEJ669901", detail=True)
    message = json.loads(str(exc.value))["message"]
    assert _code(exc) == "transport_down"
    assert "page: product page unreachable: TimeoutError" in message
    assert "graphql: Lamoda GraphQL answered HTTP 403." in message


def test_other_colours_take_the_sku_from_the_gallery_when_absent():
    fields = catalog.card_fields(
        {
            "sku": "RTLAFO975401",
            "other_colors": [
                {
                    "sku": None,
                    "gallery0": "/R/T/RTLAFO975301_38253736_1_v1_2x.jpg",
                    "colors": ["бежевый"],
                    "in_stock": True,
                }
            ],
        }
    )
    assert fields["other_colors"] == [
        {"sku": "RTLAFO975301", "color": "бежевый", "in_stock": True, "url": "https://www.lamoda.ru/p/rtlafo975301/"}
    ]


def test_a_rating_outside_one_to_five_is_not_reported():
    assert catalog.card_fields({"rating": 100})["rating"] is None
    assert catalog.card_fields({"rating": 4.6})["rating"] == 4.6


# ---------------------------------------------------------------------- photos ----


def _patch_fetch(monkeypatch, fetched, fail=()):
    async def fake_fetch(url):
        fetched.append(url)
        if any(f in url for f in fail):
            raise ValueError("HTTP 404")
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(server, "_fetch_image", fake_fetch)


async def test_photos_of_searched_skus_need_no_page_load(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    await server.lamoda_search("бомбер")
    cards: list = []
    _patch_card_page(monkeypatch, CARD_STATE, cards)
    fetched: list = []
    _patch_fetch(monkeypatch, fetched)
    result = await server.lamoda_images(["rtlafo975401", "MP002XM08CB9"], photos_per_item=2)
    assert cards == []
    assert len(fetched) == 4 and all("/img236x341/" in u for u in fetched)
    content = result.content
    assert isinstance(content[0], TextContent)
    index = json.loads(content[0].text)
    assert [row["sku"] for row in index["items"]] == ["RTLAFO975401", "MP002XM08CB9"]
    assert isinstance(content[1], TextContent) and content[1].text == "#1 RTLAFO975401 Lyle & Scott синий"
    assert isinstance(content[2], ImageContent) and content[2].mimeType == "image/jpeg"
    assert sum(isinstance(c, ImageContent) for c in content) == 4


async def test_an_unseen_sku_opens_its_product_page_once(monkeypatch):
    cards: list = []
    _patch_card_page(monkeypatch, CARD_STATE, cards)
    fetched: list = []
    _patch_fetch(monkeypatch, fetched)
    result = await server.lamoda_images(["RTLAEJ669901"], size="medium")
    assert cards == ["RTLAEJ669901"]
    assert fetched and "/img389x562/" in fetched[0]
    assert json.loads(result.content[0].text)["items"][0]["brand"] == "Fred Perry"


async def test_one_missing_photo_is_a_warning(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    await server.lamoda_search("бомбер")
    _patch_fetch(monkeypatch, [], fail=("RTLAFO975401",))
    result = await server.lamoda_images(["RTLAFO975401", "MP002XM08CB9"])
    index = json.loads(result.content[0].text)
    assert any(w.startswith("RTLAFO975401: photo unavailable") for w in index["warnings"])
    assert sum(isinstance(c, ImageContent) for c in result.content) == 1


async def test_no_photo_at_all_is_transport_down(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    await server.lamoda_search("бомбер")
    _patch_fetch(monkeypatch, [], fail=("lmcdn",))
    with pytest.raises(ToolError) as exc:
        await server.lamoda_images(["RTLAFO975401"])
    assert _code(exc) == "transport_down"


async def test_a_malformed_sku_is_a_bad_request():
    with pytest.raises(ToolError) as exc:
        await server.lamoda_images(["!!"])
    assert _code(exc) == "bad_request"


# ------------------------------------------------------------- A: economy pass ----


def _jpeg(color: str) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (236, 341), color).save(out, format="JPEG")
    return out.getvalue()


async def test_photos_arrive_as_one_labelled_sheet_by_default(monkeypatch):
    import io

    from PIL import Image

    _patch_render(monkeypatch, SEARCH_STATE, [])
    await server.lamoda_search("бомбер")

    async def fetch(url):
        return _jpeg("navy")

    monkeypatch.setattr(server, "_fetch_image", fetch)
    skus = [p["sku"] for p in SEARCH_STATE["products"][:5]]
    result = await server.lamoda_images(skus)
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1 and len(result.content) == 2
    import base64

    sheet = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
    assert sheet.size == (4 * 236, 2 * (341 + 22))  # 4 columns, labels above each tile
    index = json.loads(result.content[0].text)
    assert [row["n"] for row in index["items"]] == [1, 2, 3, 4, 5] and index["warnings"] == []


async def test_an_undecodable_photo_batch_falls_back_to_separate_images(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    await server.lamoda_search("бомбер")

    async def fetch(url):
        return b"not a jpeg"

    monkeypatch.setattr(server, "_fetch_image", fetch)
    result = await server.lamoda_images(["RTLAFO975401"])
    index = json.loads(result.content[0].text)
    assert index["warnings"] == ["sheet_unavailable: ValueError; photos sent separately"]
    assert [type(c).__name__ for c in result.content] == ["TextContent", "TextContent", "ImageContent"]


async def test_graphql_is_skipped_after_repeated_refusals_and_retried_later(monkeypatch):
    calls: list = []

    async def refused(sku, ctx):
        calls.append(sku)
        server.raise_tool_error(server.TransportDownError("Lamoda GraphQL answered HTTP 403."))

    monkeypatch.setattr(server, "_graphql_card", refused)
    _patch_card_page(monkeypatch, CARD_STATE, [])
    for _ in range(2):
        await server.lamoda_card("RTLAEJ669901")
    assert len(calls) == 2
    third = await server.lamoda_card("RTLAEJ669901")
    assert len(calls) == 2 and third.tier_used == "cdp"
    assert "graphql_tier_skipped: refused recently" in third.meta.warnings
    monkeypatch.setattr(server, "_graphql_skip_until", 0.0)  # the skip window ran out
    await server.lamoda_card("RTLAEJ669901")
    assert len(calls) == 3


async def test_a_graphql_answer_clears_the_refusal_count(monkeypatch):
    answers = iter([False, True, False])

    async def flaky(sku, ctx):
        if next(answers):
            return {"sku": sku, "name": "Бомбер", "brand_name": "X", "price_amount": 100, "sizes": []}
        server.raise_tool_error(server.TransportDownError("HTTP 403"))

    monkeypatch.setattr(server, "_graphql_card", flaky)
    _patch_card_page(monkeypatch, CARD_STATE, [])
    for _ in range(3):
        await server.lamoda_card("RTLAEJ669901")
    assert server._graphql_skip_until == 0.0  # refused, answered, refused: never two in a row


async def test_search_rows_omit_null_and_empty_fields(monkeypatch):
    _patch_render(monkeypatch, SEARCH_STATE, [])
    result = await server.lamoda_search("бомбер")
    rows = result.model_dump(by_alias=True)["items"]
    lyle = next(r for r in rows if r["sku"] == "RTLAFO975401")
    assert "old_price_rub" not in lyle and lyle["loyalty_price_rub"] == 15200.0
    assert all(v is not None and v != [] for row in rows for v in row.values())
    assert result.items[0].old_price_rub is None  # attribute access is unchanged
