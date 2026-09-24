---
name: lamoda-connector
description: Use this skill when the operator shops Lamoda for clothes, shoes or accessories — a specific item, a style in given colours, or a whole capsule wardrobe to turn into a cart-ready shortlist. Trigger on "ламода", "lamoda", "капсула", "capsule wardrobe", "подбери образ", "find me a jacket like". Search, full cards and photos run through the operator's Chrome; a light card also answers over anonymous GraphQL. Skip for non-Lamoda tasks.
---

# Lamoda Connector

Lamoda pages are Nuxt SSR, and the connector reads the page state rather than
scraping tiles. That state gives what a shopper needs: brand, colour family,
sizes in stock, photos, and the site's own filters (gender, colour, brand,
size) with counts. Discovery runs in the operator's Chrome over CDP.

## When to use
- Find items by type, colour, brand and size, men's or women's (CDP tier)
- Judge style from photos: titles cannot tell a Harrington from a windbreaker
- One product's full card: composition, size measurements and stock, other colours, rating
- Turn a capsule request into a shortlist the operator can put in the cart

## Tools available
- `lamoda_search(query, gender?, category?, colors?, brands?, sizes?, materials?,
  patterns?, styles?, seasons?, price_min?, price_max?, sale_only?, sort?, page?, all_pages?, limit?)`
  — filtered search; `all_pages=true` returns the whole pool (up to 300)
  in one call. IDs may be passed as numbers. Items carry sku, title, brand, color, price_rub,
  old_price_rub, loyalty_price_rub, sizes_in_stock, image_url, url; the
  response carries total_found, pages and `facets` (subcategories, colours,
  sizes, top brands, materials, prints, styles, seasons, with counts) to
  refine by. A filter Lamoda did not apply is a `filter_not_applied: <name>`
  warning — treat those results as unfiltered on that axis.
- `lamoda_images(skus, photos_per_item?, size?, layout?)` — up to 12 products'
  photos, by default as one grid image whose tiles are labelled `#n SKU`; the
  JSON index maps `n` to brand, title and colour. `layout="separate"` sends one
  image per photo. Needs a vision-capable model.
- `lamoda_card(sku_or_url, detail?)` — `detail=true` reads the product page:
  colour, gallery, description, composition/season/lengths (`attributes`),
  per-size stock and body measurements, the same model in other colours,
  rating. Without detail it asks the light GraphQL endpoint (price, brand,
  sizes) and falls back to the page when that is blocked; after two refusals
  in a row GraphQL is skipped for ten minutes (`graphql_tier_skipped`).
  Detailed cards stay lean: three photo URLs, no duplicate attributes, and
  measurements only for sizes in stock. Several cards are best requested as
  parallel calls in one turn — they queue for Chrome without timing out.

**Not an MCP tool:** `lamoda_selfcheck()` probes search and card. It is CLI-only —
`marketplace-mcp doctor lamoda` runs it.

## Capsule → shortlist workflow
1. **Decompose** the request into slots: garment type in Russian (the catalogue
   is Russian — "куртка", "бомбер", "брюки чинос", "кардиган"), colour
   families, the operator's size, gender. Ask for the size once if unknown.
   A brief from the creative stage follows `capsule-brief.md` (next to this
   file); map its fields as that table says.
2. **Search each slot** with filters, not with colour words in the query:
   `lamoda_search("бомбер", gender="men", colors=["navy", "olive"], sizes=["50"])`.
   Olive has no family of its own — Lamoda files it under хаки. Several colours
   in one call are OR-ed. Lamoda names garments loosely (a Harrington may be a
   "Бомбер", "Ветровка" or "Куртка-рубашка"), so query the family, not the style.
   Put the brief's other hard constraints into filters too, not the query:
   `materials=["хлопок"]`, `patterns=["однотонный"]`, `price_max=15000`,
   `seasons=["демисезон"]`. `category` keeps a word like "куртка" from pulling
   in other garments: pass a title ("Верхняя одежда") or an ID from
   `facets.categories`, which lists the next level down. Styles and seasons
   appear only once a category is set.
   **Cover the whole pool; never trim it.** Pass `all_pages=true`: it reads
   every page in one burst and de-duplicates (pages fetched minutes apart
   overlap and skip items — Lamoda re-ranks). Items are not ordered by fit,
   so cutting at any row loses matches in proportion. Pick colour families
   **wide**: labels are seller-assigned and coarse (on 2026-09-25 only 42% of
   синий was deep navy; olive greens were filed under хаки, зеленый held sage
   and teal). Leave `limit` unset.
3. **Look before choosing**: photograph every candidate with `lamoda_images`,
   12 per sheet, calls in parallel. Sort into match / borderline / reject by
   the photo; the label is only a family, and the photo decides the shade.
   Keep borderlines in the hand-over — the operator decides, not the cut.
4. **Refine** with `facets`: a brand that keeps matching, a colour the operator
   did not name but fits, a category one level down.
5. **Confirm finalists** with `lamoda_card(sku, detail=true)`: the size is in
   stock, composition and season fit, measurements fit the operator; offer a
   finalist's `other_colors` when the slot has an alternative colour.
6. **Hand over** per slot: finalists (SKU, brand, title, colour, size in
   stock, price, url, one line on why), then every other match, then the
   borderlines with a word on why — nothing that matched is dropped. Cart and checkout stay with the
   operator; this connector never writes.

## Gotchas
- A missing price is `null`, never `0`: `price_rub: null` means Lamoda had no
  usable price — treat it as no data, never as a free item. Search rows, sizes
  and other-colour rows omit null and empty fields: an absent field is the same
  "no data" as a null one.
- `price_rub` is the everyday price with public sales applied.
  `loyalty_price_rub` is Lamoda Club only, never the everyday price.
- An unknown colour name is `bad_request` listing the known families; it is
  never silently dropped. Brand names resolve through the page's brand facet;
  unmatched names come back in `brands_not_in_results`.
- `pool_incomplete` / `pool_truncated` / `page_failed` say part of the pool was
  not seen; `limited` says rows were cut by `limit`. Report them, never hide
  them; narrow the filters or retry rather than presenting the pool as whole.
- `no_results` with a filter set is a real empty result — widen a filter
  rather than retrying the same call.
- `dom_fallback` in warnings means the page shipped without its state: items
  still carry sku, title and price, but no brand, colour, sizes or photos.
- Product pages can slow past the navigation timeout after a burst of card
  loads (seen 2026-09-24, cleared within minutes). `transport_down` from
  `lamoda_card` is retryable: wait a minute, and open cards for finalists
  only — `lamoda_images` reuses search data and loads no product pages.
- Lamoda publishes a review rating only on the product page (`rating`, 1-5,
  with `reviews_count`); search results carry none.
- An empty search with a detected visible challenge returns `challenge_required`;
  it is not cached. Complete the interaction in the connected Chrome profile,
  then retry. Empty extraction without challenge evidence remains parser drift,
  not proof of "no results".
- With `CHROME_CHALLENGE_HANDOFF_S` enabled, `handoff_expires_at` confirms a
  retained search tab. Complete its interaction, then repeat the same query in
  the same MCP session before expiry to read that tab without a new navigation.
## DSH activation

In DeepSeek Harness, the default profile exposes only `compare_prices` and
`compare_sources` through the cheap compare mount. Per-marketplace tools and
`marketplace_sources` require `RU_MARKETPLACE_MCP_FULL=1` and a profile restart;
do not call them in the default mode.
