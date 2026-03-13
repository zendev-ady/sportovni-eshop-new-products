"""
attr_maps.py — Static EN→CS translation maps for B2B attribute values.

Used by translator.py (attr translation) and category_mapper.py (routing logic).
Ported from legacy-script: src/config/product_attributes.py
"""

# Maps B2B attr name (as it appears in XML) to WooCommerce Czech param name.
# Keys not listed here are silently dropped from attrs_cs.
# Empty-string value = used by pipeline logic only, never passed to WooCommerce.
ATTRIBUTE_NAME_MAP: dict[str, str] = {
    "Ball approval":            "certifikace_mice",
    "Cap adjustment":           "zapinani",
    "Capacity":                 "kapacita",
    "Capacity (L)":             "objem",
    "Cardio equipment":         "typ_stroje",
    "Club collections":         "kolekce",
    "Colour":                   "barva",
    "Destiny":                  "pouziti",
    "Gender":                   "pohlavi",
    "League collections":       "kolekce",
    "Material":                 "material",
    "Player collections":       "kolekce",
    "Producer":                 "vyrobce",
    "Representative collections": "kolekce",
    "Sleeve length":            "delka_rukavu",
    "Sport":                    "sport",
    "Strength equipment":       "typ_nacini",
    "Surface":                  "povrch",
    "Tournament Collection":    "kolekce",
    "Weight (kg)":              "hmotnost",
    # Used for routing only — excluded from attrs_cs output
    "Category":                 "",
    "Subcategory":              "",
    "Podkategoria":             "",
    "Goalkeeper equipment":     "",
    "Referee equipment":        "",
    "Product Type":             "",
    "Personalization":          "",
}

COLOUR: dict[str, str] = {
    "Beige/Cream":   "Béžová/Krémová",
    "Black":         "Černá",
    "Blue":          "Modrá",
    "Brown":         "Hnědá",
    "Golden":        "Zlatá",
    "Graphite":      "Grafitová",
    "Gray/Silver":   "Šedá/Stříbrná",
    "Green":         "Zelená",
    "Multicolour":   "Vícebarevná",
    "Navy blue":     "Námořnická modrá",
    "Orange":        "Oranžová",
    "Pink":          "Růžová",
    "Red":           "Červená",
    "Transparent":   "Průhledná",
    "Violet":        "Fialová",
    "White":         "Bílá",
    "Yellow":        "Žlutá",
}

GENDER: dict[str, str] = {
    "Kids":  "Dětské",
    "Women": "Dámské",
    "Men":   "Pánské",
}

SPORT: dict[str, str] = {
    "Badminton":                        "Badminton",
    "Basketball":                       "Basketbal",
    "Bike":                             "Cyklistika",
    "Floorball":                        "Florbal",
    "Football":                         "Fotbal",
    "Handball":                         "Házená",
    "Lifestyle":                        "Volnočasové aktivity",
    "Martial arts":                     "Bojová umění",
    "Motor sports":                     "Motorové sporty",
    "Multisport":                       "Víceúčelové sporty",
    "Nordic Walking":                   "Nordic Walking",
    "Recreational and social sports":   "Rekreační a společenské sporty",
    "Running":                          "Běh",
    "Skating":                          "Bruslení",
    "Sports medicine/Rehabilitation":   "Sportovní medicína/Rehabilitace",
    "Squash":                           "Squash",
    "Swimming":                         "Plavání",
    "Table Tennis":                     "Stolní tenis",
    "Tennis":                           "Tenis",
    "Tourism/Outdoor":                  "Turistika/Outdoor",
    "Training":                         "Trénink",
    "Volleyball":                       "Volejbal",
    "Winter sports":                    "Zimní sporty",
    "American Football":                "Americký fotbal",
}

MATERIAL: dict[str, str] = {
    "Acrylic":              "Akryl",
    "Bamboo":               "Bambus",
    "Cotton":               "Bavlna",
    "Down":                 "Peří",
    "EVA foam":             "EVA pěna",
    "Elastane":             "Elastan",
    "Elastane foam":        "Elastanová pěna",
    "Elastin":              "Elastin",
    "Felt":                 "Plsť",
    "Gum":                  "Guma",
    "Latex":                "Latex",
    "Lycra":                "Lycra",
    "Microfiber":           "Mikrovlákno",
    "Modal":                "Modal",
    "Natural leather":      "Přírodní kůže",
    "Neoprene":             "Neopren",
    "Nylon":                "Nylon",
    "PBT":                  "PBT",
    "Polar":                "Polární fleece",
    "Polietylan":           "Polyetylen",
    "Polyacrylic":          "Polyakryl",
    "Polyamide":            "Polyamid",
    "Polyester":            "Polyester",
    "Polyester fiber":      "Polyesterové vlákno",
    "Polypropylene":        "Polypropylen",
    "Polyurethane":         "Polyuretan",
    "Satin":                "Satén",
    "Silk":                 "Hedvábí",
    "Spandex":              "Spandex",
    "Synthetic fiber":      "Syntetické vlákno",
    "Synthetic leather":    "Syntetická kůže",
    "Synthetic material":   "Syntetický materiál",
    "Thermoplastic rubber": "Termoplastická guma",
    "Viscose":              "Viskóza",
    "Wool":                 "Vlna",
}

SLEEVE_LENGTH: dict[str, str] = {
    "Long-sleeved shirts":  "Dlouhý rukáv",
    "Shirts with sleeves":  "Krátký rukáv",
    "Sleeveless shirts":    "Bez rukávů",
}

DESTINY: dict[str, str] = {
    "IN/OUT":  "Venkovní/Vnitřní",
    "INDOOR":  "Vnitřní",
    "OUTDOOR": "Venkovní",
    "STREET":  "Street",
}

CARDIO_EQUIPMENT: dict[str, str] = {
    "Bikes":               "Cyklistické",
    "Massagers":           "Masážní",
    "Orbitreks":           "Orbitreky",
    "Rowing Machines":     "Veslovací",
    "Steppers":            "Steppery",
    "Steppes":             "Steppery",
    "Treadmills":          "Běžecké",
    "Twistery":            "Twistery",
    "Vibrating platforms": "Vibrační",
}

STRENGTH_EQUIPMENT: dict[str, str] = {
    "Dumbbells and weights": "Činky a závaží",
    "Atlases":               "Posilovací věže",
    "Bars":                  "Tyče",
    "Benches":               "Lavice",
    "Colic":                 "Kotouče",
    "Handrails":             "Madla",
    "Rods":                  "Tyčky",
}

# Maps B2B attr name → the value translation dict to apply.
# Keys absent here → values passed through unchanged.
_VALUE_MAPS: dict[str, dict[str, str]] = {
    "Colour":            COLOUR,
    "Gender":            GENDER,
    "Sport":             SPORT,
    "Material":          MATERIAL,
    "Sleeve length":     SLEEVE_LENGTH,
    "Destiny":           DESTINY,
    "Cardio equipment":  CARDIO_EQUIPMENT,
    "Strength equipment": STRENGTH_EQUIPMENT,
}


def get_value_map(attr_name: str) -> dict[str, str]:
    """Return the value-translation dict for *attr_name*, or empty dict if none."""
    return _VALUE_MAPS.get(attr_name, {})
