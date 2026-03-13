"""
tests/test_xml_parser.py — Unit tests for xml_parser.py

Run:
    cd b2b_to_woocommerce
    python -m pytest tests/test_xml_parser.py -v
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import xml_parser
from xml_parser import _parse_attrs, _parse_stock, _text, _float
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _xml_to_stream(xml_string: str) -> io.BytesIO:
    return io.BytesIO(xml_string.encode("utf-8"))


def _make_product_xml(*, product_id="001", mpn="MPN001", name="Test Product",
                      description="<p>Desc</p>", model="MODEL-X",
                      producer="Nike", weight="0.5", category="Footwear/Football",
                      wholesale_netto="49.99", attrs=None, images=None,
                      stock_attribute="Size", stock_quantity="10",
                      stock_items=None) -> str:
    attrs_xml = ""
    if attrs:
        attrs_xml = "<attrs>" + "".join(
            f'<attr name="{k}">{v}</attr>' for k, v in attrs
        ) + "</attrs>"

    images_xml = ""
    if images:
        images_xml = "<images>" + "".join(
            f"<image>{url}</image>" for url in images
        ) + "</images>"

    if stock_items is None:
        stock_items = [("UID001", "EAN001", "5", "42")]

    items_xml = "".join(
        f'<item uid="{uid}" ean="{ean}" quantity="{qty}">{size}</item>'
        for uid, ean, qty, size in stock_items
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <id><![CDATA[{product_id}]]></id>
    <name><![CDATA[{name}]]></name>
    <description><![CDATA[{description}]]></description>
    <mpn><![CDATA[{mpn}]]></mpn>
    <model><![CDATA[{model}]]></model>
    <producer><![CDATA[{producer}]]></producer>
    <weight><![CDATA[{weight}]]></weight>
    <category><![CDATA[{category}]]></category>
    <prices><wholesale_netto>{wholesale_netto}</wholesale_netto></prices>
    {attrs_xml}
    {images_xml}
    <stock attribute="{stock_attribute}" quantity="{stock_quantity}">{items_xml}</stock>
  </product>
</products>"""


# ---------------------------------------------------------------------------
# parse() — integration
# ---------------------------------------------------------------------------

class TestParse:
    def test_returns_list(self):
        xml = _make_product_xml()
        result = xml_parser._parse_stream(_xml_to_stream(xml))
        assert isinstance(result, list)

    def test_parses_one_product(self):
        xml = _make_product_xml()
        result = xml_parser._parse_stream(_xml_to_stream(xml))
        assert len(result) == 1

    def test_basic_fields(self):
        xml = _make_product_xml(product_id="42", mpn="ABC", name="Test", producer="Adidas")
        product = xml_parser._parse_stream(_xml_to_stream(xml))[0]
        assert product["id"] == "42"
        assert product["mpn"] == "ABC"
        assert product["name"] == "Test"
        assert product["producer"] == "Adidas"

    def test_wholesale_netto_parsed_as_float(self):
        xml = _make_product_xml(wholesale_netto="89.50")
        product = xml_parser._parse_stream(_xml_to_stream(xml))[0]
        assert product["wholesale_netto"] == 89.50
        assert isinstance(product["wholesale_netto"], float)

    def test_missing_id_skipped(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <mpn><![CDATA[MPN001]]></mpn>
    <prices><wholesale_netto>10.0</wholesale_netto></prices>
    <stock attribute="" quantity="0"></stock>
  </product>
</products>"""
        result = xml_parser._parse_stream(_xml_to_stream(xml))
        assert result == []

    def test_missing_mpn_skipped(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <id><![CDATA[001]]></id>
    <prices><wholesale_netto>10.0</wholesale_netto></prices>
    <stock attribute="" quantity="0"></stock>
  </product>
</products>"""
        result = xml_parser._parse_stream(_xml_to_stream(xml))
        assert result == []

    def test_multiple_products(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <id>1</id><mpn>A</mpn>
    <prices><wholesale_netto>10.0</wholesale_netto></prices>
    <stock attribute="" quantity="0"></stock>
  </product>
  <product>
    <id>2</id><mpn>B</mpn>
    <prices><wholesale_netto>20.0</wholesale_netto></prices>
    <stock attribute="" quantity="0"></stock>
  </product>
</products>"""
        result = xml_parser._parse_stream(_xml_to_stream(xml))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _parse_attrs
# ---------------------------------------------------------------------------

class TestParseAttrs:
    def _elem(self, xml_str: str) -> ET.Element:
        return ET.fromstring(xml_str)

    def test_single_attr(self):
        elem = self._elem(
            '<product><attrs><attr name="Colour">Black</attr></attrs></product>'
        )
        result = _parse_attrs(elem)
        assert result == {"Colour": ["Black"]}

    def test_comma_separated_values_split(self):
        elem = self._elem(
            '<product><attrs><attr name="Size">S, M, L</attr></attrs></product>'
        )
        result = _parse_attrs(elem)
        assert result == {"Size": ["S", "M", "L"]}

    def test_duplicate_attr_merged(self):
        """Two <attr> elements with the same name are merged into one list."""
        elem = self._elem(
            '<product><attrs>'
            '<attr name="Colour">Red</attr>'
            '<attr name="Colour">Blue</attr>'
            '</attrs></product>'
        )
        result = _parse_attrs(elem)
        assert set(result["Colour"]) == {"Red", "Blue"}

    def test_deduplication_within_same_attr(self):
        elem = self._elem(
            '<product><attrs><attr name="Size">M, M, L</attr></attrs></product>'
        )
        result = _parse_attrs(elem)
        assert result["Size"].count("M") == 1

    def test_empty_attrs_elem(self):
        elem = self._elem('<product><attrs></attrs></product>')
        assert _parse_attrs(elem) == {}

    def test_missing_attrs_elem(self):
        elem = self._elem('<product></product>')
        assert _parse_attrs(elem) == {}

    def test_empty_value_skipped(self):
        elem = self._elem(
            '<product><attrs><attr name="Colour"></attr></attrs></product>'
        )
        assert _parse_attrs(elem) == {}


# ---------------------------------------------------------------------------
# _parse_stock
# ---------------------------------------------------------------------------

class TestParseStock:
    def _elem(self, xml_str: str) -> ET.Element:
        return ET.fromstring(xml_str)

    def test_basic_stock(self):
        elem = self._elem(
            '<product>'
            '<stock attribute="Size" quantity="15">'
            '<item uid="U1" ean="E1" quantity="10">42</item>'
            '<item uid="U2" ean="E2" quantity="5">43</item>'
            '</stock>'
            '</product>'
        )
        stock = _parse_stock(elem)
        assert stock["attribute"] == "Size"
        assert stock["total_quantity"] == 15
        assert len(stock["items"]) == 2
        assert stock["items"][0]["uid"] == "U1"
        assert stock["items"][0]["size_label"] == "42"
        assert stock["items"][0]["quantity"] == 10

    def test_missing_stock_returns_defaults(self):
        elem = self._elem('<product></product>')
        stock = _parse_stock(elem)
        assert stock == {"attribute": "", "total_quantity": 0, "items": []}

    def test_item_without_size_label_gets_na(self):
        elem = self._elem(
            '<product>'
            '<stock attribute="" quantity="3">'
            '<item uid="U1" ean="E1" quantity="3"></item>'
            '</stock>'
            '</product>'
        )
        stock = _parse_stock(elem)
        assert stock["items"][0]["size_label"] == "N/A"

    def test_quantity_is_int(self):
        elem = self._elem(
            '<product>'
            '<stock attribute="" quantity="7">'
            '<item uid="U1" ean="E1" quantity="7">XL</item>'
            '</stock>'
            '</product>'
        )
        stock = _parse_stock(elem)
        assert isinstance(stock["total_quantity"], int)
        assert isinstance(stock["items"][0]["quantity"], int)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

class TestParseImages:
    def test_images_parsed(self):
        xml = _make_product_xml(images=[
            "https://example.com/img1.jpg",
            "https://example.com/img2.jpg",
        ])
        product = xml_parser._parse_stream(_xml_to_stream(xml))[0]
        assert len(product["images"]) == 2
        assert product["images"][0] == "https://example.com/img1.jpg"

    def test_no_images_returns_empty_list(self):
        xml = _make_product_xml(images=[])
        product = xml_parser._parse_stream(_xml_to_stream(xml))[0]
        assert product["images"] == []
