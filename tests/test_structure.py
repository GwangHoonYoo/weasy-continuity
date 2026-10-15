"""Adversarial MCID and exact marked-content byte-range tests."""

import tempfile
import unittest
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import (
    NameObject as N,
    NumberObject as I,
    DictionaryObject as D,
    ArrayObject as A,
    DecodedStreamObject,
)
from weasy_continuity.verification import inspect
from weasy_continuity.marked_content import artifact_stream, paint_hash


class StructuralRefusalTests(unittest.TestCase):
    def fixture(self, target, logical, physical, parent_entries):
        w = PdfWriter()
        page = w.add_blank_page(100, 100)
        page[N("/StructParents")] = I(0)
        stream = DecodedStreamObject()
        stream.set_data(
            ("\n".join(f"/P <</MCID {i}>> BDC\nEMC" for i in physical)).encode()
        )
        page[N("/Contents")] = w._add_object(stream)
        root = D({N("/Type"): N("/StructTreeRoot")})
        rr = w._add_object(root)
        doc = D({N("/Type"): N("/StructElem"), N("/S"): N("/Document"), N("/P"): rr})
        dr = w._add_object(doc)
        span = D(
            {
                N("/Type"): N("/StructElem"),
                N("/S"): N("/P"),
                N("/P"): dr,
                N("/Pg"): page.indirect_reference,
                N("/K"): A([I(i) for i in logical]),
            }
        )
        sr = w._add_object(span)
        doc[N("/K")] = A([sr])
        root[N("/K")] = A([dr])
        root[N("/ParentTree")] = w._add_object(
            D({N("/Nums"): A([I(0), A([sr for _ in range(parent_entries)])])})
        )
        w.root_object[N("/StructTreeRoot")] = rr
        w.write(target)
        return inspect(target)

    def test_every_physical_mcid_has_exactly_one_nonnegative_logical_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary) / "test.pdf"
            self.assertEqual(self.fixture(p, [0], [0], 1)["errors"], [])
            for name, logical, physical, parents, expected in [
                ("ghost", [0, 1], [0], 2, "physical_logical_mcid_mismatch"),
                ("duplicate", [0, 0], [0], 1, "duplicate_logical_mcid"),
                ("negative", [-1], [-1], 1, "negative_physical_mcid"),
                ("fractional", [0], [0.5], 1, "noninteger_physical_mcid"),
                ("orphan-parenttree", [0], [0], 2, "parenttree_ghost_mcid"),
            ]:
                with self.subTest(name=name):
                    errors = self.fixture(p, logical, physical, parents)["errors"]
                    self.assertIn(expected, [e[0] for e in errors])

    def test_only_real_bdc_operand_range_is_rewritten(self):
        data = b"% /Span <</MCID 1>> BDC\n/Span <</MCID 1>> BDC\nBT (literal /Span <</MCID 1>> BDC) Tj ET\nEMC\n"
        changed = artifact_stream(data, None, {1: "Header"})
        self.assertEqual(
            changed,
            b"% /Span <</MCID 1>> BDC\n/Artifact <</Type /Pagination /Subtype /Header>> BDC\nBT (literal /Span <</MCID 1>> BDC) Tj ET\nEMC\n",
        )
        self.assertEqual(paint_hash(data, None), paint_hash(changed, None))
        self.assertNotEqual(
            paint_hash(data, None),
            paint_hash(changed.replace(b"literal", b"changed"), None),
        )
