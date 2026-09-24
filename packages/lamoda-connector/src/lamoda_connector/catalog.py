"""Lamoda catalog vocabulary, URL building and the SSR state readers.

Lamoda renders with Nuxt, and ``window.__NUXT__.payload.state.payload`` carries
the data the grid and the product page are drawn from. Captured live through
the operator's Chrome on 2026-09-24:

* search/catalog page: ``products`` (60 per page) with ``brand.name``,
  ``model_name``, ``color_family``, ``gallery`` (image paths), ``prices`` and
  per-size ``sizes[].is_available``; ``facets`` with the filter vocabulary and
  counts (``colors``, ``size_values``, ``brands`` ...); ``pagination``
  ``{page, pages, found}``;
* product page: ``product`` with ``colors``, ``gallery``, ``attributes``
  (composition, season, lengths), ``description``, ``sizes[].stock_quantity``
  with body measurements, ``colored_products`` (the same model in other
  colours), ``average_rating`` and ``counters.reviews``.

Reading that state is sturdier than reading the DOM: the DOM carries neither
the brand, the colour, nor the image gallery. The DOM extractor stays as the
fallback for a page that ships without the state.

Filters are plain query parameters on the catalog URL, verified live on
2026-09-24: ``colors`` and ``size_values`` and ``brands`` take comma-joined IDs
(``colors=3847,637`` returned 271 + 171 = 442), ``page`` pages by 60, and the
gender category paths below scope a text query to men's or women's goods.

The functions here are pure so the offline suite can drive them from captured
fixtures; the JavaScript readers run in the page and return compact JSON.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from mcp_core import resilience as R

SITE_BASE = "https://www.lamoda.ru"
IMAGE_BASE = "https://a.lmcdn.ru"

SEARCH_PATH = "/catalogsearch/result/"
# The "Мужчинам" / "Женщинам" links on a search page. A text query under these
# paths searches that gender's whole catalogue (clothes, shoes, accessories).
GENDER_PATHS = {"men": "/c/4152/default-men/", "women": "/c/4153/default-women/"}

SORTS = ("default", "new", "price_asc", "price_desc", "discount")

# The CDN answers only a fixed set of sizes; anything else is HTTP 400.
IMAGE_SIZES = {"small": "img236x341", "medium": "img389x562"}

# Lamoda's colour filter vocabulary (facet ``colors``), keyed by its own title.
COLOR_IDS: dict[str, str] = {
    "бежевый": "647",
    "белый": "615",
    "бирюзовый": "30862",
    "бордовый": "3865",
    "голубой": "859",
    "горчичный": "44055",
    "желтый": "613",
    "зеленый": "637",
    "золотой": "3851",
    "коричневый": "641",
    "красный": "619",
    "молочный": "44054",
    "мультиколор": "631",
    "оранжевый": "629",
    "розовый": "623",
    "серебряный": "3843",
    "серый": "635",
    "синий": "643",
    "сиреневый": "44053",
    "фиолетовый": "689",
    "хаки": "3847",
    "черный": "645",
}

# English names and shades Lamoda has no family for, mapped to the family a
# shopper would look in. Olive has no family of its own: Lamoda files it as хаки.
_COLOR_ALIASES: dict[str, str] = {
    "beige": "бежевый",
    "white": "белый",
    "turquoise": "бирюзовый",
    "teal": "бирюзовый",
    "burgundy": "бордовый",
    "maroon": "бордовый",
    "light blue": "голубой",
    "sky blue": "голубой",
    "mustard": "горчичный",
    "yellow": "желтый",
    "green": "зеленый",
    "gold": "золотой",
    "brown": "коричневый",
    "red": "красный",
    "cream": "молочный",
    "ecru": "молочный",
    "off-white": "молочный",
    "multicolor": "мультиколор",
    "orange": "оранжевый",
    "pink": "розовый",
    "silver": "серебряный",
    "grey": "серый",
    "gray": "серый",
    "blue": "синий",
    "navy": "синий",
    "lilac": "сиреневый",
    "purple": "фиолетовый",
    "violet": "фиолетовый",
    "khaki": "хаки",
    "olive": "хаки",
    "black": "черный",
}
# Russian stems, so "синяя", "оливковые" or "тёмно-синий" resolve too.
_STEM_ALIASES: dict[str, str] = {"оливков": "хаки", "темно-син": "синий", "темносин": "синий", "кремов": "молочный"}
_ADJ_ENDING = re.compile(r"(ый|ий|ой|ая|яя|ое|ее|ые|ие)$")

_ID_RE = re.compile(r"^\d{1,7}$")
_SIZE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z/.\-]{0,11}$")
_IMAGE_PATH_RE = re.compile(r"^/[A-Z0-9]/[A-Z0-9]/[A-Za-z0-9_]{6,80}\.(?:jpg|jpeg|png|webp)$")


def _stem(word: str) -> str:
    return _ADJ_ENDING.sub("", word)


def resolve_color(raw: str) -> tuple[str, str] | None:
    """Map a colour name, alias or numeric filter ID to ``(id, lamoda_title)``.

    Returns ``None`` for a name Lamoda has no family for, so the caller can say
    so instead of silently dropping the filter.
    """
    word = raw.strip().lower().replace("ё", "е")
    if not word:
        return None
    if _ID_RE.match(word):
        title = next((t for t, i in COLOR_IDS.items() if i == word), word)
        return word, title
    if word in COLOR_IDS:
        return COLOR_IDS[word], word
    if word in _COLOR_ALIASES:
        title = _COLOR_ALIASES[word]
        return COLOR_IDS[title], title
    stem = _stem(word)
    if stem in _STEM_ALIASES:
        title = _STEM_ALIASES[stem]
        return COLOR_IDS[title], title
    for title, cid in COLOR_IDS.items():
        if _stem(title) == stem:
            return cid, title
    return None


def valid_size(raw: str) -> str | None:
    """A size filter value as Lamoda keys it ("48", "M", "44/46"), or None."""
    value = raw.strip()
    return value if _SIZE_RE.match(value) else None


def is_filter_id(raw: str) -> bool:
    return bool(_ID_RE.match(raw.strip()))


def build_search_url(
    query: str,
    *,
    gender: str | None = None,
    color_ids: list[str] | None = None,
    brand_ids: list[str] | None = None,
    sizes: list[str] | None = None,
    sort: str | None = None,
    page: int = 1,
) -> str:
    """Build the catalog URL. Every filter value must already be validated.

    Values reach the URL only through ``urlencode``; commas are kept literal
    because that is how Lamoda's own filter links join several values.
    """
    path = GENDER_PATHS.get(gender or "", SEARCH_PATH)
    params: list[tuple[str, str]] = [("q", query)]
    if color_ids:
        params.append(("colors", ",".join(color_ids)))
    if brand_ids:
        params.append(("brands", ",".join(brand_ids)))
    if sizes:
        params.append(("size_values", ",".join(sizes)))
    if sort and sort != "default":
        params.append(("sort", sort))
    if page > 1:
        params.append(("page", str(page)))
    return f"{SITE_BASE}{path}?{urllib.parse.urlencode(params, safe=',', quote_via=urllib.parse.quote)}"


def image_url(path: str, size: str | None = None) -> str | None:
    """Full CDN URL for a gallery path; None when the path is not a gallery path."""
    if not isinstance(path, str) or not _IMAGE_PATH_RE.match(path):
        return None
    segment = IMAGE_SIZES.get(size or "", "product")
    return f"{IMAGE_BASE}/{segment}{path}"


def product_url(sku: str) -> str:
    return f"{SITE_BASE}/p/{sku.lower()}/"


# ------------------------------------------------------------ in-page readers ----

# Both readers return compact JSON so a 60-product page stays well under the
# body cap, and ``null`` when the page carries no Nuxt state (the caller then
# falls back to the DOM extractor).
SEARCH_STATE_JS = r"""
() => {
  const n = window.__NUXT__;
  const pl = n && n.payload && n.payload.state && n.payload.state.payload;
  if (!pl || !Array.isArray(pl.products)) return JSON.stringify(null);
  const facet = (name) => {
    const f = (pl.facets || []).find(x => x && x.name === name);
    const vals = f && f.list_value && Array.isArray(f.list_value.values) ? f.list_value.values : [];
    return vals.map(v => [String(v.key), v.title, v.found]);
  };
  const products = pl.products.filter(p => p && typeof p.sku === 'string').map(p => ({
    sku: p.sku,
    name: p.name || null,
    model: p.model_name || null,
    brand: p.brand && p.brand.name || null,
    color: p.color_family || null,
    gallery: Array.isArray(p.gallery) ? p.gallery.slice(0, 4) : [],
    price_amount: p.price_amount,
    prices: Array.isArray(p.prices) ? p.prices.map(x => ({price: x.price, type: x.discount && x.discount.type || null})) : [],
    sellable: p.is_sellable,
    sizes: Array.isArray(p.sizes) ? p.sizes.map(s => [s.size || null, s.brand_size || null, s.is_available]) : [],
  }));
  const pg = pl.pagination || {};
  return JSON.stringify({
    products,
    pagination: {page: pg.page, pages: pg.pages, found: pg.found},
    facets: {colors: facet('colors'), sizes: facet('size_values'), brands: facet('brands')},
  });
}
"""

CARD_STATE_JS = r"""
() => {
  const n = window.__NUXT__;
  const pl = n && n.payload && n.payload.state && n.payload.state.payload;
  const p = pl && pl.product;
  if (!p || typeof p.sku !== 'string') return JSON.stringify(null);
  const titles = (arr) => Array.isArray(arr) ? arr.map(c => c && c.title).filter(Boolean) : [];
  return JSON.stringify({
    sku: p.sku,
    title: p.title || null,
    // The product page names these differently from the grid: brand.title and
    // model_title (grid: brand.name and model_name). Captured 2026-09-24.
    model: p.model_title || null,
    brand: p.brand && (p.brand.title || p.brand.name) || null,
    colors: titles(p.colors),
    gallery: Array.isArray(p.gallery) ? p.gallery.slice(0, 8) : [],
    price: p.price,
    old_price: p.old_price,
    loyalty_price: p.prices && p.prices.loyalty_base && p.prices.loyalty_base.price || null,
    in_stock: p.is_in_stock,
    sellable: p.is_sellable,
    sizes: Array.isArray(p.sizes) ? p.sizes.map(s => ({
      size: s.title || null, brand_size: s.brand_title || null,
      stock: s.stock_quantity, measurements: s.primary_description || null,
    })) : [],
    attributes: Array.isArray(p.attributes) ? p.attributes.slice(0, 20).map(a => [a.title, a.value]) : [],
    description: typeof p.description === 'string' ? p.description.slice(0, 800) : null,
    rating: p.average_rating,
    reviews: p.counters && p.counters.reviews,
    other_colors: Array.isArray(p.colored_products) ? p.colored_products.slice(0, 12).map(c => ({
      sku: c.sku || null,
      gallery0: Array.isArray(c.gallery) && c.gallery[0] || null,
      colors: titles(c.colors),
      in_stock: c.is_in_stock,
    })) : [],
  });
}
"""


# ----------------------------------------------------------- Python parsing ----


def _price(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.replace(" ", "").replace(" ", "")
    return R.coerce_price(value)


def split_prices(price_amount: Any, prices: Any) -> tuple[float | None, float | None, float | None]:
    """Return ``(everyday, old, loyalty)`` from a search product's price ladder.

    ``prices`` is a ladder: the original price, then each discount layer
    (``onsite``, ``onsite_promo`` — public sales). A ``loyalty`` layer is
    Lamoda Club — a membership price, reported apart and never as the everyday
    price (the same rule as Yandex Plus). ``price_amount`` is the current
    price, not the original: captured 2026-09-24, MP002XM08CB9 had
    ``price_amount`` 10917 against a ladder of 20599 -> 10917 (onsite).
    """
    amount = _price(price_amount)
    public: list[float] = []
    loyalty: float | None = None
    for step in prices if isinstance(prices, list) else []:
        if not isinstance(step, dict):
            continue
        value = _price(step.get("price"))
        if value is None:
            continue
        if step.get("type") == "loyalty":
            loyalty = value if loyalty is None else min(loyalty, value)
        else:
            public.append(value)
    everyday = min(public) if public else amount
    candidates = [*public, *([amount] if amount is not None else [])]
    highest = max(candidates) if candidates else None
    old = highest if highest is not None and everyday is not None and highest > everyday else None
    if loyalty is not None and everyday is not None and loyalty >= everyday:
        loyalty = None
    return everyday, old, loyalty


def size_label(size: Any, brand_size: Any) -> str | None:
    """ "48/50 (M)" — Russian size first, the brand's own label when it differs."""
    ru = size if isinstance(size, str) and size else None
    brand = brand_size if isinstance(brand_size, str) and brand_size else None
    if ru and brand and brand != ru:
        return f"{ru} ({brand})"
    return ru or brand


def search_item(product: dict[str, Any]) -> dict[str, Any]:
    """One compact state product -> the fields of ``LamodaSearchItemOut``."""
    sku = str(product.get("sku") or "")
    name = product.get("name")
    model = product.get("model")
    title = " ".join(x for x in (name, model) if isinstance(x, str) and x) or None
    everyday, old, loyalty = split_prices(product.get("price_amount"), product.get("prices"))
    raw_sizes = product.get("sizes")
    sizes: list[Any] = raw_sizes if isinstance(raw_sizes, list) else []
    in_stock_sizes = [
        label
        for s in sizes
        if isinstance(s, list) and len(s) == 3 and s[2] is True
        for label in [size_label(s[0], s[1])]
        if label
    ]
    gallery = [p for p in product.get("gallery") or [] if isinstance(p, str)]
    return {
        "sku": sku,
        "title": title,
        "brand": product.get("brand") or None,
        "color": product.get("color") or None,
        "price_rub": everyday,
        "old_price_rub": old,
        "loyalty_price_rub": loyalty,
        "in_stock": product.get("sellable") if isinstance(product.get("sellable"), bool) else None,
        "sizes_in_stock": in_stock_sizes,
        "image_url": image_url(gallery[0]) if gallery else None,
        "url": product_url(sku) if sku else None,
        "_gallery": gallery,
    }


def facet_values(raw: Any) -> list[tuple[str, str, int | None]]:
    out: list[tuple[str, str, int | None]] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, list) and len(entry) == 3 and entry[0] is not None:
            out.append((str(entry[0]), str(entry[1] or entry[0]), R.coerce_int(entry[2])))
    return out


def resolve_brands(names: list[str], brand_facet: list[tuple[str, str, int | None]]) -> tuple[list[str], list[str]]:
    """Map brand names to filter IDs through the page's own brand facet.

    Exact case-insensitive title match first, then a unique prefix match.
    Returns ``(ids, unresolved_names)``.
    """
    ids: list[str] = []
    unresolved: list[str] = []
    by_title = {title.lower(): key for key, title, _ in brand_facet}
    for name in names:
        wanted = name.strip().lower()
        if wanted in by_title:
            ids.append(by_title[wanted])
            continue
        prefixed = [key for key, title, _ in brand_facet if title.lower().startswith(wanted)]
        if len(prefixed) == 1:
            ids.append(prefixed[0])
        else:
            unresolved.append(name)
    return ids, unresolved


# Product-page attributes that repeat other fields or never inform a choice.
# Dropping them keeps a detailed card near 1k tokens instead of 2-3k.
_CARD_ATTRIBUTE_NOISE = frozenset({"Цвет", "Узор", "Артикул", "Гарантийный срок", "Страна производства", "Вид спорта"})
_CARD_IMAGES = 3


def card_fields(product: dict[str, Any]) -> dict[str, Any]:
    """Compact product-page state -> the extra fields of ``LamodaCardResponse``."""
    colors = [c for c in product.get("colors") or [] if isinstance(c, str)]
    gallery = [p for p in product.get("gallery") or [] if isinstance(p, str)]
    price = _price(product.get("price"))
    old = _price(product.get("old_price"))
    loyalty = _price(product.get("loyalty_price"))
    sizes = []
    for s in product.get("sizes") or []:
        if not isinstance(s, dict):
            continue
        stock = R.coerce_int(s.get("stock"))
        available = (stock > 0) if stock is not None else None
        sizes.append(
            {
                "size": size_label(s.get("size"), s.get("brand_size")),
                "is_available": available,
                "stock": stock,
                # Fit data only matters for a size one can order.
                "measurements": (s.get("measurements") or None) if available is not False else None,
            }
        )
    attributes = {
        str(a[0]): str(a[1])
        for a in product.get("attributes") or []
        if isinstance(a, list) and len(a) == 2 and a[0] and a[1] and a[0] not in _CARD_ATTRIBUTE_NOISE
    }
    others = []
    for c in product.get("other_colors") or []:
        if not isinstance(c, dict):
            continue
        sku = c.get("sku")
        if not sku and isinstance(c.get("gallery0"), str):
            # Gallery files are named after the SKU: /R/T/RTLAFO975301_38253736_1_v1_2x.jpg
            sku = c["gallery0"].rsplit("/", 1)[-1].split("_", 1)[0]
        if not isinstance(sku, str) or not sku:
            continue
        others.append(
            {
                "sku": sku.upper(),
                "color": ", ".join(x for x in c.get("colors") or [] if isinstance(x, str)) or None,
                "in_stock": c.get("in_stock") if isinstance(c.get("in_stock"), bool) else None,
                "url": product_url(sku),
            }
        )
    rating = product.get("rating")
    rating_value = float(rating) if isinstance(rating, (int, float)) and 0 < rating <= 5 else None
    in_stock = product.get("in_stock")
    return {
        "title": " ".join(x for x in (product.get("title"), product.get("model")) if isinstance(x, str) and x) or None,
        "brand": product.get("brand") or None,
        "color": ", ".join(colors) or None,
        "price_rub": price,
        "old_price_rub": old if old is not None and price is not None and old > price else None,
        "loyalty_price_rub": loyalty if loyalty is not None and price is not None and loyalty < price else None,
        "is_available": in_stock if isinstance(in_stock, bool) else None,
        "sizes": sizes,
        "images": [u for u in (image_url(p) for p in gallery) if u][:_CARD_IMAGES],
        "description": product.get("description") or None,
        "attributes": attributes,
        "rating": rating_value,
        "reviews_count": R.coerce_int(product.get("reviews")),
        "other_colors": others,
        "_gallery": gallery,
    }
