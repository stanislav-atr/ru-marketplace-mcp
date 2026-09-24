# Capsule brief — the hand-off from the creative stage

A capsule is designed in a Claude Project and searched here. The brief is the
contract between the two: Markdown the designer can read, with one YAML block
the search maps field by field onto `lamoda_search`. Every field is optional —
what is missing is asked once or left wide, never guessed narrow.

## Template

```yaml
capsule: "Autumn city, rain-proof"     # a name, for the ledger
gender: men                            # men | women
sizes:                                 # what fits; brand notes win over the default
  ru: 50                               # Russian size for tops/outerwear
  bottoms: "32/32"
  notes: "Mango runs small: take L"
budget:                                # ₽, current price
  per_item_max: 20000
season: демисезон                      # демисезон | зима | лето | мульти
coverage: full                         # full (default): every page, every photo | quick
palette:                               # named colours the slots reuse
  navy:  {hex: "#1E2638", words: "deep, near-black navy"}
  olive: {hex: "#5A5B3C", words: "green before khaki; not sage, not brown"}
slots:
  - id: rain-jacket
    garment: "hooded rain jacket"      # what it is, in plain words
    ru: ["ветровка с капюшоном"]        # search words; filled in when absent
    category: "Верхняя одежда"          # Lamoda title or ID
    colour: navy                       # palette key, hex, or a family (синий)
    pattern: однотонный
    materials: []                      # хлопок, лен, шерсть, полиамид...
    price: {max: 20000}
    must: ["hood", "waterproof or membrane"]
    avoid: ["insulated", "colour-block"]
    fit: "hip length, ~70-75 cm"
    finalists: 3                       # how many to confirm with cards
    notes: "Harrington-ish cut welcome"
```

## How each field is used

| Brief field | Becomes | Note |
|---|---|---|
| `gender`, `category` | `gender`, `category` | category also keeps "куртка" from pulling in other garments |
| `ru` / `garment` | `query` | garment family words, never colour words |
| `colour` | `colors` (families) | chosen **wide**: every Lamoda family the hex could be filed under (olive → хаки + зеленый); the photo decides the shade, and borderlines are listed, not dropped |
| `pattern`, `materials`, `season` | `patterns`, `materials`, `seasons` | hard filters; each one is checked as applied |
| `price`, `budget.per_item_max` | `price_min` / `price_max` | current price; Lamoda Club price is reported apart |
| `sizes` | `sizes` | only when the operator wants in-stock-in-my-size; otherwise sizes are shown per item |
| `coverage: full` | `all_pages=true`, every candidate photographed | nothing cut; `quick` = first page and a first photo sheet |
| `must`, `avoid`, `fit` | photo and card review | judged on photos and on `lamoda_card(detail=true)` attributes and measurements |
| `finalists` | cards opened | the rest of the matches stay listed |

## What comes back

Per slot, three lists — nothing that matched the filters is dropped silently:

1. **Finalists**: SKU · brand · title · colour · sizes in stock · price · url ·
   one line on why (confirmed with a card).
2. **Also matching**: every other photo match, same columns without the card.
3. **Borderline**: shade or cut is arguable, one word on why.

Plus the pool size (`total_found`, items seen, photos judged) and any warning
(`filter_not_applied`, `pool_incomplete`, `pool_truncated`).

## Instruction for the Claude Project

Paste into the Project's instructions so the creative chat ends with a brief:

> When we settle a capsule, end with a "Capsule brief" section: one fenced
> YAML block following the template in `capsule-brief.md` (capsule, gender,
> sizes, budget, season, coverage, palette with hex and words, slots with
> garment, ru, category, colour, pattern, materials, price, must, avoid, fit,
> finalists, notes). Keep every colour as a hex plus words. Leave a field out
> rather than guess it.
