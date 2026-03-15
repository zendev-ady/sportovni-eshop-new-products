"""
tests/test_category_mapper.py — Unit tests for category_mapper.py

Run:
    cd b2b_to_woocommerce
    python -m pytest tests/test_category_mapper.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import category_mapper
from category_mapper import (
    _build_mapper_params, _resolve_ids, _SPORT_ROUTING_MAP, _PATH_ALIASES,
    _B2B_CAT_TO_SPORT, _B2B_GENDER_MAP, _BAD_SPORT_VALUES,
    _NAME_GENDER_KEYWORDS, _NAME_TYPE_KEYWORDS,
)


# ---------------------------------------------------------------------------
# Minimal stubs — avoid importing full dataclasses (which pull in OpenAI etc.)
# ---------------------------------------------------------------------------

class _Variation:
    """Minimal Variation stub for size-heuristic tests."""
    def __init__(self, size_label: str):
        self.size_label = size_label


class _Group:
    """Minimal ProductGroup stub for testing."""
    def __init__(self, attrs=None, model="TEST", category="", variations=None, name=""):
        self.attrs = attrs or {}
        self.model = model
        self.category = category
        self.variations = variations or []
        self.name = name


class _Translated:
    """Minimal TranslatedGroup stub for testing."""
    def __init__(self, attrs_cs=None, name_cs=""):
        self.attrs_cs = attrs_cs or {}
        self.name_cs = name_cs


# ---------------------------------------------------------------------------
# _build_mapper_params — sport routing
# ---------------------------------------------------------------------------

class TestBuildMapperParams:
    def test_sport_routing_bojova_umeni(self):
        """'bojová umění' in attrs_cs should be routed to 'bojové sporty'."""
        group = _Group()
        translated = _Translated(attrs_cs={"sport": ["bojová umění"]})
        params = _build_mapper_params(group, translated)
        assert params["sport"] == "bojové sporty"

    def test_sport_routing_zimni_sporty(self):
        """'zimní sporty' should be routed to 'lední hokej'."""
        group = _Group()
        translated = _Translated(attrs_cs={"sport": ["zimní sporty"]})
        params = _build_mapper_params(group, translated)
        assert params["sport"] == "lední hokej"

    def test_sport_routing_trenink(self):
        """'trénink' should be routed to 'fitness'."""
        group = _Group()
        translated = _Translated(attrs_cs={"sport": ["trénink"]})
        params = _build_mapper_params(group, translated)
        assert params["sport"] == "fitness"

    def test_sport_no_routing_needed(self):
        """Known sport without routing rule passes through unchanged."""
        group = _Group()
        translated = _Translated(attrs_cs={"sport": ["fotbal"]})
        params = _build_mapper_params(group, translated)
        assert params["sport"] == "fotbal"

    def test_product_type_mapping(self):
        """Raw B2B 'Shoes' Product Type is translated to Czech 'boty'."""
        group = _Group(attrs={"Product Type": ["Shoes"]})
        translated = _Translated()
        params = _build_mapper_params(group, translated)
        assert params.get("typ") == "boty"

    def test_product_type_tshirt(self):
        group = _Group(attrs={"Product Type": ["T-shirt"]})
        translated = _Translated()
        params = _build_mapper_params(group, translated)
        assert params.get("typ") == "tričko"

    def test_category_source_mapping(self):
        """Raw B2B 'Clothing' category is mapped to Czech 'oblečení'."""
        group = _Group(attrs={"Category": ["Clothing"]})
        translated = _Translated()
        params = _build_mapper_params(group, translated)
        assert params.get("kategorie") == "oblečení"

    def test_unknown_product_type_not_added(self):
        """Unknown Product Type produces no 'typ' param (logged as debug)."""
        group = _Group(attrs={"Product Type": ["Unknown Type XYZ"]})
        translated = _Translated()
        params = _build_mapper_params(group, translated)
        assert "typ" not in params

    def test_gender_passes_through(self):
        group = _Group()
        translated = _Translated(attrs_cs={"pohlavi": ["pánské"]})
        params = _build_mapper_params(group, translated)
        assert params.get("pohlavi") == "pánské"

    # --- B2B category fallback (step 5) ---

    def test_b2b_category_fills_sport_when_absent(self):
        """group.category='Football/Men' fills sport + pohlavi when attrs are empty."""
        group = _Group(category="Football/Men")
        params = _build_mapper_params(group, _Translated())
        assert params["sport"] == "fotbal"
        assert params["pohlavi"] == "pánské"

    def test_b2b_category_does_not_override_attrs_sport(self):
        """attrs_cs sport wins over group.category sport (attrs have priority)."""
        group = _Group(category="Running/Men")
        translated = _Translated(attrs_cs={"sport": ["fotbal"]})
        params = _build_mapper_params(group, translated)
        assert params["sport"] == "fotbal"

    def test_bad_sport_value_stripped_b2b_fills_in(self):
        """'to be categorized' is stripped; B2B category then fills in correct sport."""
        group = _Group(category="Basketball/Women")
        translated = _Translated(attrs_cs={"sport": ["to be categorized"]})
        params = _build_mapper_params(group, translated)
        assert params.get("sport") == "basketbal"
        assert params.get("pohlavi") == "dámské"

    def test_b2b_category_generic_container_uses_second_segment(self):
        """'Footwear/Football' — first segment is generic, second fills sport."""
        group = _Group(category="Footwear/Football")
        params = _build_mapper_params(group, _Translated())
        assert params["sport"] == "fotbal"

    def test_lifestyle_b2b_category_no_sport_set(self):
        """'Lifestyle' B2B category leaves sport unset — gender+type routing handles it."""
        group = _Group(category="Lifestyle/Shoes/Women")
        params = _build_mapper_params(group, _Translated())
        assert "sport" not in params
        assert params.get("pohlavi") == "dámské"

    def test_b2b_category_kids_gender(self):
        """'Training/Kids' fills pohlavi=dětské."""
        group = _Group(category="Training/Kids")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dětské"
        assert params.get("sport") == "fitness"

    def test_b2b_category_empty_string_no_crash(self):
        """Empty group.category produces no params and does not crash."""
        group = _Group(category="")
        params = _build_mapper_params(group, _Translated())
        assert "sport" not in params
        assert "pohlavi" not in params

    # --- Shoe-size heuristic (step 6) ---

    def test_shoe_size_heuristic_sets_detske(self):
        """All numeric sizes 20–35 → pohlavi=dětské (children's shoe sizes)."""
        sizes = [str(s) for s in range(21, 36)]  # 21–35
        group = _Group(variations=[_Variation(s) for s in sizes])
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dětské"

    def test_shoe_size_heuristic_not_triggered_for_adult_sizes(self):
        """Sizes including 36+ are adult — heuristic must NOT fire."""
        sizes = ["34", "35", "36", "37", "38", "39", "40"]
        group = _Group(variations=[_Variation(s) for s in sizes])
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") != "dětské"

    def test_shoe_size_heuristic_requires_min_3_sizes(self):
        """Only 2 sizes (e.g. 33, 35) is ambiguous — heuristic must NOT fire."""
        group = _Group(variations=[_Variation("33"), _Variation("35")])
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") != "dětské"

    def test_shoe_size_heuristic_does_not_override_existing_gender(self):
        """Explicit Gender attr always wins over size heuristic."""
        sizes = [str(s) for s in range(21, 30)]
        group = _Group(variations=[_Variation(s) for s in sizes])
        translated = _Translated(attrs_cs={"pohlavi": ["pánské"]})
        params = _build_mapper_params(group, translated)
        assert params.get("pohlavi") == "pánské"

    def test_shoe_size_heuristic_skips_non_numeric_sizes(self):
        """Non-numeric sizes (e.g. 'One size', 'XL') don't trigger heuristic."""
        group = _Group(variations=[_Variation("One size"), _Variation("S"), _Variation("M")])
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") != "dětské"


# ---------------------------------------------------------------------------
# _BAD_SPORT_VALUES / _B2B_CAT_TO_SPORT / _B2B_GENDER_MAP — data integrity
# ---------------------------------------------------------------------------

class TestNewDicts:
    def test_bad_sport_values_lowercase(self):
        """All bad sport values must be lowercase (comparison is on .lower())."""
        for v in _BAD_SPORT_VALUES:
            assert v == v.lower(), f"{v!r} not lowercase"

    def test_b2b_cat_to_sport_values_lowercase(self):
        """Non-None values in _B2B_CAT_TO_SPORT must be lowercase Czech."""
        for k, v in _B2B_CAT_TO_SPORT.items():
            if v is not None:
                assert v == v.lower(), f"key={k!r} value={v!r} not lowercase"

    def test_b2b_gender_map_values_lowercase(self):
        for k, v in _B2B_GENDER_MAP.items():
            assert v == v.lower(), f"key={k!r} value={v!r} not lowercase"

    def test_lifestyle_maps_to_none(self):
        """Lifestyle B2B category must map to None (no sport routing)."""
        assert _B2B_CAT_TO_SPORT.get("lifestyle") is None

    def test_football_maps_to_fotbal(self):
        assert _B2B_CAT_TO_SPORT["football"] == "fotbal"

    def test_women_maps_to_damske(self):
        assert _B2B_GENDER_MAP["women"] == "dámské"


# ---------------------------------------------------------------------------
# _resolve_ids — ID lookup and path aliases
# ---------------------------------------------------------------------------

class TestResolveIds:
    def test_known_path_returns_correct_id(self):
        """A fully configured path returns its WC ID."""
        ids = _resolve_ids(["Sporty > Fotbal > Kopačky > Lisovky"])
        assert 45 in ids

    def test_zero_id_path_excluded(self):
        """A path with ID=0 in category_ids.json is excluded from result."""
        # "Sporty > Fitness > Fitness obuv" has ID 0 → should not appear in ids
        ids = _resolve_ids(["Sporty > Fitness > Fitness obuv"])
        assert 0 not in ids

    def test_path_alias_resolves(self):
        """'Tenisové míče a doplňky' is aliased to 'Tenisové doplňky' (ID 58)."""
        ids = _resolve_ids(["Sporty > Tenis > Tenisové míče a doplňky"])
        assert 58 in ids

    def test_unknown_path_returns_empty(self):
        """A completely unknown path (no known parent either) returns no IDs."""
        # Must use a root name that does not exist in category_ids.json
        ids = _resolve_ids(["Neexistující > Kategorie > Podkategorie"])
        assert ids == []

    def test_multiple_paths(self):
        """Two known paths return two IDs."""
        ids = _resolve_ids([
            "Sporty > Fotbal",              # 43
            "Muži > Pánské boty",           # 430
        ])
        assert 43 in ids
        assert 430 in ids

    def test_deduplication(self):
        """Same path twice yields one ID."""
        ids = _resolve_ids(["Sporty > Fotbal", "Sporty > Fotbal"])
        assert ids.count(43) == 1

    def test_returns_list(self):
        ids = _resolve_ids(["Sporty"])
        assert isinstance(ids, list)


# ---------------------------------------------------------------------------
# _SPORT_ROUTING_MAP — completeness checks
# ---------------------------------------------------------------------------

class TestSportRoutingMap:
    def test_all_targets_lowercase(self):
        """Routing targets should be lowercase — CategoryMapper matches on lower."""
        for src, target in _SPORT_ROUTING_MAP.items():
            assert target == target.lower(), f"{src!r} → {target!r} not lowercase"

    def test_all_sources_lowercase(self):
        for src in _SPORT_ROUTING_MAP:
            assert src == src.lower(), f"Source {src!r} not lowercase"


# ---------------------------------------------------------------------------
# _PATH_ALIASES — correctness
# ---------------------------------------------------------------------------

class TestPathAliases:
    def test_alias_targets_exist_in_category_ids(self):
        """Every alias target must exist in WOO_CATEGORY_IDS."""
        from config.config import WOO_CATEGORY_IDS
        for src, target in _PATH_ALIASES.items():
            assert target in WOO_CATEGORY_IDS, (
                f"Alias target {target!r} not found in category_ids.json"
            )


# ---------------------------------------------------------------------------
# Step 7 — English name keyword fallback
# ---------------------------------------------------------------------------

class TestNameKeywordFallback:
    def test_kids_pants_from_name(self):
        """'kids\\' pants' in English name sets pohlavi=dětské and typ=kalhoty."""
        group = _Group(name="adidas Tiro 26 League Sweat kids' pants black JY9674")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dětské"
        assert params.get("typ") == "kalhoty"

    def test_mens_pants_from_name(self):
        """'Men\\'s Pants' in English name sets pohlavi=pánské and typ=kalhoty."""
        group = _Group(name="adidas Tiro 26 League Presentation Men's Pants Navy Blue JZ9045")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "pánské"
        assert params.get("typ") == "kalhoty"

    def test_hoodie_for_kids_from_name(self):
        """'Hoodie for Kids' sets pohlavi=dětské and typ=mikina."""
        group = _Group(name="adidas Tiro 26 League Sweat Full Zip Hoodie for Kids Navy Blue KF3322")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dětské"
        assert params.get("typ") == "mikina"

    def test_womens_jacket_from_name(self):
        """'Women\\'s jacket' sets pohlavi=dámské and typ=bunda."""
        group = _Group(name="Women's jacket 4F F0705 beige 4FRAW25")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dámské"
        assert params.get("typ") == "bunda"

    def test_name_keyword_does_not_override_attrs(self):
        """Explicit attrs_cs always wins over name keyword — step 7 only fills gaps."""
        group = _Group(name="adidas Tiro 26 League kids' pants JY9674")
        translated = _Translated(attrs_cs={"pohlavi": ["pánské"]})
        params = _build_mapper_params(group, translated)
        assert params.get("pohlavi") == "pánské"  # attrs win

    def test_women_keyword_not_false_matched_inside_women(self):
        """'women' must not be matched as 'men' — specific patterns checked first."""
        group = _Group(name="adidas Run Women's Tights XYZ")
        params = _build_mapper_params(group, _Translated())
        assert params.get("pohlavi") == "dámské"

    def test_name_keyword_lists_no_duplicates(self):
        """No keyword appears twice in _NAME_GENDER_KEYWORDS or _NAME_TYPE_KEYWORDS."""
        gender_kws = [kw for kw, _ in _NAME_GENDER_KEYWORDS]
        type_kws = [kw for kw, _ in _NAME_TYPE_KEYWORDS]
        assert len(gender_kws) == len(set(gender_kws))
        assert len(type_kws) == len(set(type_kws))
