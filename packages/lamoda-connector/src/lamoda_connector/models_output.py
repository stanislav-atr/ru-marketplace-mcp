"""Pydantic output models for the Lamoda MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Lamoda carries the shared envelope unchanged."""


class LamodaSearchItemOut(BaseModel):
    sku: str | None = Field(default=None, description="Lamoda SKU (e.g. MP002XM1RMM3).")
    title: str | None = Field(default=None, description="Product type plus model name, e.g. 'Бомбер HARRINGTON'.")
    brand: str | None = Field(default=None, description="Brand name.")
    color: str | None = Field(
        default=None, description="Lamoda's colour family ('синий', 'хаки'); a label, not a shade."
    )
    price_rub: float | None = Field(
        default=None, description="Everyday price in rubles, public discounts applied; None when absent — never 0."
    )
    old_price_rub: float | None = Field(default=None, description="Pre-discount price in rubles.")
    loyalty_price_rub: float | None = Field(
        default=None, description="Lamoda Club member price; needs membership, so never the everyday price."
    )
    in_stock: bool | None = Field(default=None, description="Whether Lamoda sells the item now; None when unknown.")
    sizes_in_stock: list[str] = Field(
        default_factory=list, description="Sizes currently orderable, Russian size first: '48/50 (M)'."
    )
    image_url: str | None = Field(
        default=None, description="Main product photo; pass the SKU to lamoda_images to see it."
    )
    url: str | None = Field(default=None, description="Canonical product URL.")


class LamodaFacetOut(BaseModel):
    id: str = Field(description="Filter value to pass back (colour/brand ID or size).")
    title: str = Field(description="Human label.")
    count: int | None = Field(default=None, description="Matching products for the current query and filters.")


class LamodaFacetsOut(BaseModel):
    colors: list[LamodaFacetOut] = Field(default_factory=list, description="Colour families present in the results.")
    sizes: list[LamodaFacetOut] = Field(default_factory=list, description="Sizes present in the results.")
    brands: list[LamodaFacetOut] = Field(default_factory=list, description="Most frequent brands (top 15).")


class LamodaSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (cdp).")
    count: int = Field(default=0, description="Number of items returned.")
    total_found: int | None = Field(default=None, description="Products matching query and filters across all pages.")
    page: int | None = Field(default=None, description="Page returned (60 products per Lamoda page).")
    pages: int | None = Field(default=None, description="Pages available for this query and filters.")
    filters_applied: dict[str, list[str] | str] = Field(
        default_factory=dict, description="Filters Lamoda actually received, by Lamoda's own labels."
    )
    facets: LamodaFacetsOut | None = Field(
        default=None, description="Refinement options with counts; None on DOM fallback."
    )
    items: list[LamodaSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class LamodaSizeOut(BaseModel):
    size: str | None = Field(default=None, description="Size label, Russian size first: '48/50 (M)'.")
    is_available: bool | None = Field(default=None, description="Whether the size is sellable.")
    stock: int | None = Field(default=None, description="Units left when Lamoda reports them (product page tier).")
    measurements: str | None = Field(default=None, description="Body measurements the size fits, when published.")


class LamodaOtherColorOut(BaseModel):
    sku: str = Field(description="SKU of the same model in another colour.")
    color: str | None = Field(default=None, description="Its colour family.")
    in_stock: bool | None = Field(default=None, description="Whether it is sellable now.")
    url: str = Field(description="Product URL.")


class LamodaCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    sku: str | None = Field(default=None, description="Lamoda SKU.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    color: str | None = Field(default=None, description="Colour family (product page tier only).")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Strikethrough price in rubles.")
    loyalty_price_rub: float | None = Field(default=None, description="Lamoda Club member price (page tier).")
    is_available: bool | None = Field(default=None, description="Whether the product is sellable now.")
    sizes: list[LamodaSizeOut] = Field(default_factory=list, description="Per-size availability.")
    images: list[str] = Field(default_factory=list, description="Gallery photo URLs (page tier).")
    description: str | None = Field(default=None, description="Seller's description: cut, details (page tier).")
    attributes: dict[str, str] = Field(
        default_factory=dict, description="Composition, season, lengths, model's size — Lamoda's labels (page tier)."
    )
    rating: float | None = Field(default=None, description="Average review rating 1-5 (page tier); None when unrated.")
    reviews_count: int | None = Field(default=None, description="Number of reviews (page tier).")
    other_colors: list[LamodaOtherColorOut] = Field(
        default_factory=list, description="The same model in other colours (page tier)."
    )
    url: str = Field(default="", description="Canonical product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: graphql (price/sizes only) or cdp (full page).")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class LamodaSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class LamodaSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, LamodaSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
