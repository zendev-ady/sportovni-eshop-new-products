"""
create_missing_categories.py — Jednorázový skript pro vytvoření chybějících WC kategorií.

Vytvoří všechny kategorie s ID=0 z category_ids.json, v správném pořadí (rodiče dříve).
Po úspěchu zapíše nová ID zpět do category_ids.json.

Spuštění:
    cd b2b_to_woocommerce
    python scripts/create_missing_categories.py
"""

import json
import sys
import os
import time

# Fix Windows terminal encoding
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.api_keys import WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET
from woocommerce import API as WooAPI

WOO_URL = "https://darkblue-toad-760041.hostingersite.com"

api = WooAPI(
    url=WOO_URL,
    consumer_key=WOO_CONSUMER_KEY,
    consumer_secret=WOO_CONSUMER_SECRET,
    wp_api=True,
    version="wc/v3",
    timeout=30,
)

CATEGORY_IDS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "category_ids.json"
)

# ---------------------------------------------------------------------------
# Categories to create — ordered: parents before children.
# "parent_path" must already have a non-zero ID in category_ids.json
# (or be created earlier in this list).
# ---------------------------------------------------------------------------
CATEGORIES_TO_CREATE = [
    # --- Muži ---
    {
        "path": "Muži > Pánské oblečení > Pánské mikiny",
        "name": "Pánské mikiny",
        "parent_path": "Muži > Pánské oblečení",
        "description": "Pánské sportovní mikiny a hoodies pro trénink i volný čas. "
                       "Vyberte si z nabídky značek adidas, Nike, Under Armour a dalších — "
                       "funkční materiály, pohodlný střih a styl do každého počasí.",
        "slug": "panske-mikiny",
    },
    {
        "path": "Muži > Pánské boty > Pánské pantofle",
        "name": "Pánské pantofle",
        "parent_path": "Muži > Pánské boty",
        "description": "Pánské sportovní pantofle a přezůvky pro pohodlí po tréninku i doma. "
                       "Lehké, prodyšné a snadno nazouvatelné modely od předních sportovních značek.",
        "slug": "panske-pantofle",
    },
    {
        "path": "Muži > Pánské boty > Pánské sandály",
        "name": "Pánské sandály",
        "parent_path": "Muži > Pánské boty",
        "description": "Pánské sportovní sandály pro outdoor, turistiku i volný čas. "
                       "Robustní konstrukce, nastavitelné pásky a protiskluzová podrážka.",
        "slug": "panske-sandaly",
    },

    # --- Ženy ---
    {
        "path": "Ženy > Dámské boty > Dámské tenisky",
        "name": "Dámské tenisky",
        "parent_path": "Ženy > Dámské boty",
        "description": "Dámské tenisky a sneakers pro sport i každodenní nošení. "
                       "Pohodlná a stylová obuv od předních světových značek — adidas, Nike, Puma a další.",
        "slug": "damske-tenisky",
    },
    {
        "path": "Ženy > Dámské boty > Dámské pantofle",
        "name": "Dámské pantofle",
        "parent_path": "Ženy > Dámské boty",
        "description": "Dámské sportovní pantofle a přezůvky pro relaxaci po tréninku i domácí nošení. "
                       "Pohodlné modely z prodyšných materiálů s anatomickým tvarováním stélky.",
        "slug": "damske-pantofle",
    },
    {
        "path": "Ženy > Dámské boty > Dámské sandály",
        "name": "Dámské sandály",
        "parent_path": "Ženy > Dámské boty",
        "description": "Dámské sportovní sandály pro outdoor, turistiku i každodenní nošení. "
                       "Lehká konstrukce, nastavitelné pásky a pohodlná stélka pro celodenní nošení.",
        "slug": "damske-sandaly",
    },

    # --- Děti ---
    {
        "path": "Děti > Dětské oblečení > Dětské mikiny",
        "name": "Dětské mikiny",
        "parent_path": "Děti > Dětské oblečení",
        "description": "Dětské sportovní mikiny a hoodies pro aktivní pohyb i volný čas. "
                       "Pohodlné střihy z funkčních materiálů, které vydrží i nejbouřlivější hry.",
        "slug": "detske-mikiny",
    },
    {
        "path": "Děti > Dětské oblečení > Dětské zimní oblečení",
        "name": "Dětské zimní oblečení",
        "parent_path": "Děti > Dětské oblečení",
        "description": "Dětské zimní sportovní oblečení — bundy, vesty a tepláky pro chladné dny. "
                       "Hřejivé a větruodolné modely, které děti udrží v teple při zimních aktivitách.",
        "slug": "detske-zimni-obleceni",
    },
    {
        "path": "Děti > Dětské boty > Dětské outdoorové boty",
        "name": "Dětské outdoorové boty",
        "parent_path": "Děti > Dětské boty",
        "description": "Dětské outdoorové a trekové boty pro turistiku a pohyb v přírodě. "
                       "Vodoodolné materiály, pevná podrážka a anatomická stélka pro zdravý vývoj chodidel.",
        "slug": "detske-outdoorove-boty",
    },
    {
        "path": "Děti > Dětské boty > Dětské pantofle",
        "name": "Dětské pantofle",
        "parent_path": "Děti > Dětské boty",
        "description": "Dětské sportovní pantofle a přezůvky pro pohodlí po sportu i doma. "
                       "Lehké, prodyšné a snadno nazouvatelné modely s protiskluzovou podrážkou.",
        "slug": "detske-pantofle",
    },
    {
        "path": "Děti > Dětské boty > Dětské sandále",
        "name": "Dětské sandále",
        "parent_path": "Děti > Dětské boty",
        "description": "Dětské sportovní sandále pro léto, outdoor i každodenní nošení. "
                       "Nastavitelné pásky, prodyšné materiály a pevná podrážka pro aktivní děti.",
        "slug": "detske-sandale",
    },
    # Dětské doplňky — parent je "Děti" (431), musí být dříve než jeho děti
    {
        "path": "Děti > Dětské doplňky",
        "name": "Dětské doplňky",
        "parent_path": "Děti",
        "description": "Dětské sportovní doplňky — batohy, čepice, rukavice a příslušenství pro aktivní děti. "
                       "Vše, co potřebují malí sportovci k tréninku i dobrodružství.",
        "slug": "detske-doplnky",
    },
    {
        "path": "Děti > Dětské doplňky > Dětské batohy",
        "name": "Dětské batohy",
        "parent_path": "Děti > Dětské doplňky",
        "description": "Dětské sportovní batohy a ruksaky pro školu, turistiku i sport. "
                       "Ergonomické nošení, odolné materiály a dostatek prostoru pro výbavu mladého sportovce.",
        "slug": "detske-batohy",
    },
    {
        "path": "Děti > Dětské doplňky > Dětské čepice",
        "name": "Dětské čepice",
        "parent_path": "Děti > Dětské doplňky",
        "description": "Dětské sportovní čepice, kšiltovky a kulichy pro každé počasí. "
                       "Funkční materiály, které ochrání hlavu dítěte při sportu i venkovních aktivitách.",
        "slug": "detske-cepice",
    },

    # --- Sporty > Padel ---
    {
        "path": "Sporty > Padel",
        "name": "Padel",
        "parent_path": "Sporty",
        "description": "Vše pro padel — rakety, míče, tašky a příslušenství. "
                       "Jeden z nejrychleji rostoucích sportů v Česku, který spojuje tenis a squash. "
                       "Vybavte se od předních světových značek.",
        "slug": "padel",
    },
    {
        "path": "Sporty > Padel > Padelové rakety",
        "name": "Padelové rakety",
        "parent_path": "Sporty > Padel",
        "description": "Padelové rakety pro začátečníky i zkušené hráče. "
                       "Rozdílné tvary, váhy a materiály pro každý styl hry — vyberte si svoji ideální raketu.",
        "slug": "padelove-rakety",
    },
    {
        "path": "Sporty > Padel > Padelové míče a doplňky",
        "name": "Padelové míče a doplňky",
        "parent_path": "Sporty > Padel",
        "description": "Padelové míče a doplňky pro hru i trénink. "
                       "Certifikované míče pro různé typy povrchů, gripy, chrániče a další příslušenství.",
        "slug": "padelove-mice-a-doplnky",
    },
    {
        "path": "Sporty > Padel > Padelové tašky",
        "name": "Padelové tašky",
        "parent_path": "Sporty > Padel",
        "description": "Tašky a batohy speciálně navržené pro padel. "
                       "Pevné držáky na rakety, oddělené kapsy na míče a pohodlné nošení na kurt i zpět.",
        "slug": "padelove-tasky",
    },

    # --- Sporty > Bojové sporty ---
    {
        "path": "Sporty > Bojové sporty > Karate",
        "name": "Karate",
        "parent_path": "Sporty > Bojové sporty",
        "description": "Vybavení pro karate — kimona (karategi), chrániče, opasky a příslušenství. "
                       "Kvalitní výstroj pro trénink i závodní soutěže všech věkových kategorií.",
        "slug": "karate",
    },
    {
        "path": "Sporty > Bojové sporty > Judo",
        "name": "Judo",
        "parent_path": "Sporty > Bojové sporty",
        "description": "Vybavení pro judo — judogi, pasy a příslušenství pro trénink i závody. "
                       "Odolné materiály navržené pro intenzivní kontaktní sport.",
        "slug": "judo",
    },

    # --- Sporty > Fitness ---
    {
        "path": "Sporty > Fitness > Fitness obuv",
        "name": "Fitness obuv",
        "parent_path": "Sporty > Fitness",
        "description": "Sportovní obuv pro fitness, posilovnu a crosstraining. "
                       "Stabilní podrážka, bočnicová opora a prodyšné materiály pro maximální výkon při cvičení.",
        "slug": "fitness-obuv",
    },
    {
        "path": "Sporty > Fitness > Jóga",
        "name": "Jóga",
        "parent_path": "Sporty > Fitness",
        "description": "Vybavení pro jógu — podložky, bloky, pásy a příslušenství. "
                       "Kvalitní pomůcky pro začátečníky i pokročilé praktikanty jógy.",
        "slug": "joga",
    },
    {
        "path": "Sporty > Fitness > Pilates",
        "name": "Pilates",
        "parent_path": "Sporty > Fitness",
        "description": "Vybavení pro pilates — podložky, válce a pomůcky pro cvičení. "
                       "Posilujte střed těla a zlepšujte flexibilitu s kvalitním vybavením.",
        "slug": "pilates",
    },
    {
        "path": "Sporty > Fitness > Cvičící vybavení",
        "name": "Cvičící vybavení",
        "parent_path": "Sporty > Fitness",
        "description": "Fitness pomůcky a vybavení pro domácí i halový trénink. "
                       "Odporové gumy, kettlebelly, švihadla, medicinbaly a vše pro efektivní cvičení.",
        "slug": "cvicici-vybaveni",
    },

    # --- Zimní oblečení ---
    {
        "path": "Zimní oblečení > Lyžařské oblečení",
        "name": "Lyžařské oblečení",
        "parent_path": "Zimní oblečení",
        "description": "Lyžařské bundy, kalhoty a funkční prádlo pro zimní sporty na sjezdovkách i v terénu. "
                       "Voděodolné a větruodolné materiály s dostatečnou tepelnou izolací pro celodenní lyžování.",
        "slug": "lyzarske-obleceni",
    },
]


def load_ids() -> dict:
    with open(CATEGORY_IDS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_ids(data: dict) -> None:
    with open(CATEGORY_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  [OK] category_ids.json uložen")


def create_category(name: str, parent_id: int, description: str, slug: str) -> int | None:
    payload = {
        "name": name,
        "parent": parent_id,
        "description": description,
        "slug": slug,
    }
    resp = api.post("products/categories", payload)
    data = resp.json()
    if "id" in data:
        return data["id"]
    print(f"  ❌ Chyba: {data}")
    return None


def main():
    ids = load_ids()
    created = 0
    skipped = 0

    for cat in CATEGORIES_TO_CREATE:
        path = cat["path"]

        # Skip if already has a real ID
        if ids.get(path, 0) != 0:
            print(f"  [-] Přeskočeno (má ID {ids[path]}): {path}")
            skipped += 1
            continue

        # Resolve parent ID
        parent_path = cat["parent_path"]
        parent_id = ids.get(parent_path, 0)
        if not parent_id:
            print(f"  [!] Rodič {parent_path!r} nemá ID -- přeskočeno: {path}")
            skipped += 1
            continue

        print(f"  [+] Vytvářím: {path} (parent_id={parent_id}) ...", end=" ", flush=True)
        new_id = create_category(
            name=cat["name"],
            parent_id=parent_id,
            description=cat["description"],
            slug=cat["slug"],
        )
        if new_id:
            ids[path] = new_id
            save_ids(ids)
            print(f"ID={new_id} ✅")
            created += 1
        else:
            print("SELHALO")

        time.sleep(0.3)  # krátká pauza, ať nezahltíme API

    print(f"\nHotovo -- vytvořeno: {created}, přeskočeno: {skipped}")


if __name__ == "__main__":
    main()
