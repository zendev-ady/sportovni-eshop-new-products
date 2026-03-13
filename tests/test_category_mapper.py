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
from category_mapper import _build_mapper_params, _resolve_ids, _SPORT_ROUTING_MAP, _PATH_ALIASES


# ---------------------------------------------------------------------------
# Minimal stubs — avoid importing full dataclasses (which pull in OpenAI etc.)
# ---------------------------------------------------------------------------

class _Group:
    """Minimal ProductGroup stub for testing."""
    def __init__(self, attrs=None, model="TEST", category=""):
        self.attrs = attrs or {}
        self.model = model
        self.category = category


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
        """A completely unknown path returns no IDs (and no crash)."""
        ids = _resolve_ids(["Sporty > Neexistující > Kategorie"])
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
