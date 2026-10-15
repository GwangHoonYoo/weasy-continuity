"""Acceptance tests use original synthetic inputs; public reports stay URL fixtures."""

from __future__ import annotations
import base64, hashlib, http.server, json, os, subprocess, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
NATIVE = os.environ.get("WEASY_CONTINUITY_NATIVE_LIB")


def command(*args, timeout=30, python=None, extra_env=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [python or sys.executable, "-m", "weasy_continuity", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def html(body, extra_css=""):
    return (
        '<!doctype html><html lang="en"><head><title>Owned test input</title><style>@page{size:A6;margin:10mm}td,th{border:1px solid black;padding:3px}'
        + extra_css
        + "</style></head><body>"
        + body
        + "</body></html>"
    )


def table():
    return (
        "<table><thead><tr><th>Label</th><th>Value</th></tr></thead><tbody>"
        + "".join(f"<tr><td>{i}</td><td>Repeated row</td></tr>" for i in range(24))
        + "</tbody></table>"
    )


class NativeIndependentTests(unittest.TestCase):
    def test_help_runs_without_any_site_packages(self):
        program = 'import sys;sys.path.insert(0,sys.argv[1]);from weasy_continuity.cli import main;main(["--help"])'
        p = subprocess.run(
            [sys.executable, "-S", "-c", program, str(SOURCE)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("render", p.stdout)
        self.assertNotIn("Pango", p.stderr)

    def test_aliases_and_unowned_force_targets_refuse_before_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            b = Path(temporary)
            source = b / "input.html"
            source.write_text(html("original"))
            original = source.read_bytes()
            p = command(
                "render", source, "-o", source, "--receipt", b / "r.json", "--force"
            )
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "path_collision")
            output = b / "foreign.pdf"
            output.write_bytes(b"a different file")
            p = command(
                "render", source, "-o", output, "--receipt", b / "r.json", "--force"
            )
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "unowned_output")
            self.assertEqual(output.read_bytes(), b"a different file")
            self.assertEqual(source.read_bytes(), original)

    def test_source_hardlink_target_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            b = Path(temporary)
            source = b / "input.html"
            output = b / "output.pdf"
            source.write_text(html("kept"))
            os.link(source, output)
            p = command(
                "render", source, "-o", output, "--receipt", b / "r.json", "--force"
            )
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "path_collision")

    def test_source_change_during_worker_refuses_publication(self):
        sys.path.insert(0, str(SOURCE))
        from weasy_continuity.cli import render_command
        from weasy_continuity.errors import ContinuityError
        import argparse

        with tempfile.TemporaryDirectory() as temporary:
            b = Path(temporary)
            source = b / "input.html"
            source.write_text(html("Original"))

            def concurrent_edit(*args, **kwargs):
                source.write_text(html("Edited externally while rendering"))
                return {}

            arguments = argparse.Namespace(
                source=source,
                output=b / "out.pdf",
                receipt=b / "out.json",
                native_lib=None,
                timeout=10,
                force=False,
            )
            with patch("weasy_continuity.cli.call_worker", side_effect=concurrent_edit):
                with self.assertRaises(ContinuityError) as failure:
                    render_command(arguments)
            self.assertEqual(failure.exception.code, "source_changed")
            self.assertFalse((b / "out.pdf").exists())
            self.assertFalse((b / "out.json").exists())

    def test_pdf_only_is_not_a_complete_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            b = Path(temporary)
            (b / "lonely.pdf").write_bytes(b"%PDF")
            p = command("verify", b / "missing-receipt.json")
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "invalid_receipt")

    def test_publication_rolls_back_when_second_replace_fails(self):
        sys.path.insert(0, str(SOURCE))
        from weasy_continuity.atomic import publish_files
        from weasy_continuity.errors import ContinuityError

        with tempfile.TemporaryDirectory() as temporary:
            b = Path(temporary)
            output, receipt = b / "old.pdf", b / "old.json"
            pdf_stage, receipt_stage = b / "new.pdf", b / "new.json"
            output.write_bytes(b"old PDF")
            receipt.write_bytes(b"old receipt")
            pdf_stage.write_bytes(b"new PDF")
            receipt_stage.write_bytes(b"new receipt")
            actual = os.replace

            def fail_receipt(source, target):
                if Path(source) == receipt_stage:
                    raise OSError("injected second-file failure")
                return actual(source, target)

            with patch("weasy_continuity.atomic.os.replace", side_effect=fail_receipt):
                with self.assertRaises(ContinuityError) as failure:
                    publish_files(pdf_stage, receipt_stage, output, receipt, force=True)
            self.assertEqual(failure.exception.code, "publication_failed")
            self.assertEqual(output.read_bytes(), b"old PDF")
            self.assertEqual(receipt.read_bytes(), b"old receipt")
            self.assertFalse(list(b.glob(".weasy-continuity-backup-*")))


@unittest.skipUnless(
    NATIVE, "Set WEASY_CONTINUITY_NATIVE_LIB for native acceptance tests"
)
class RendererAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def render(self, body, name="result", extra_css="", force=False, timeout=180):
        source = self.base / f"{name}.html"
        source.write_text(html(body, extra_css))
        output, receipt = self.base / f"{name}.pdf", self.base / f"{name}.json"
        args = [
            "render",
            source,
            "-o",
            output,
            "--receipt",
            receipt,
            "--native-lib",
            NATIVE,
            "--timeout",
            str(timeout),
        ]
        if force:
            args.append("--force")
        before = source.read_bytes()
        p = command(*args)
        self.assertEqual(source.read_bytes(), before)
        return p, source, output, receipt

    def test_joins_source_table_and_list_without_changing_page_payload(self):
        body = (
            table()
            + "<ol>"
            + "".join(f"<li>Entry{i}</li>" for i in range(30))
            + '</ol><a href="https://example.org/">Preserved link</a>'
        )
        p, _, output, receipt = self.render(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads(receipt.read_text())
        self.assertGreater(record["before"]["roles"]["/Table"], 1)
        self.assertGreater(record["before"]["roles"]["/L"], 1)
        self.assertEqual(record["after"]["roles"]["/Table"], 1)
        self.assertEqual(record["after"]["roles"]["/L"], 1)
        self.assertEqual(record["after"]["roles"]["/LI"], 30)
        retired = record["changes"]["retired_mcids"]
        self.assertEqual(len(retired), 2)
        self.assertEqual(
            record["before"]["mcids"] - record["after"]["mcids"], len(retired)
        )
        self.assertEqual(record["after"]["roles"]["/TH"], 2)
        self.assertTrue(all(e["subtype"] == "Header" for e in retired))
        self.assertEqual(record["after"]["annotations"], 1)
        self.assertTrue(all(record["checks"].values()))
        checked = command("verify", receipt)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        output.write_bytes(output.read_bytes() + b"tampered")
        checked = command("verify", receipt)
        self.assertEqual(
            json.loads(checked.stdout)["error"]["code"], "receipt_mismatch"
        )

    def test_distinct_identical_tables_and_nested_lists_remain_distinct(self):
        p, _, _, receipt = self.render(
            table()
            + table()
            + "<ul><li>First<ul><li>A</li><li>B</li></ul></li><li>Second</li></ul>"
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads(receipt.read_text())
        self.assertEqual(record["after"]["roles"]["/Table"], 2)
        self.assertEqual(record["after"]["roles"]["/L"], 2)
        self.assertEqual(record["after"]["roles"]["/LI"], 4)
        self.assertTrue(all(record["checks"].values()))

    def test_cross_page_paragraph_and_list_item_preserve_physical_mcids(self):
        body = (
            "<p>"
            + ("One paragraph crosses pages. " * 80)
            + "</p><p>Separate paragraph</p><ol><li>"
            + ("One item crosses pages. " * 80)
            + "</li><li>Separate item</li></ol>"
        )
        p, _, _, receipt = self.render(
            body, extra_css="p,li{font:14pt serif;line-height:1.5}"
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads(receipt.read_text())
        self.assertGreater(record["before"]["roles"]["/P"], 2)
        self.assertEqual(record["after"]["roles"]["/P"], 2)
        self.assertEqual(record["after"]["roles"]["/LI"], 2)
        self.assertEqual(record["after"]["roles"]["/L"], 1)
        self.assertEqual(record["before"]["mcids"], record["after"]["mcids"])

    def test_external_resource_is_refused_without_request_or_overwrite(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            first, source, output, receipt = self.render(
                "<p>First complete PDF</p>", name="kept"
            )
            self.assertEqual(first.returncode, 0, first.stdout)
            old_pdf, old_receipt = output.read_bytes(), receipt.read_bytes()
            p, _, _, _ = self.render(
                f'<img alt="External" src="http://127.0.0.1:{server.server_port}/image.png">',
                name="kept",
                force=True,
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)["error"]["code"], "external_resource")
            self.assertEqual(requests, [])
            self.assertEqual(output.read_bytes(), old_pdf)
            self.assertEqual(receipt.read_bytes(), old_receipt)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_ambiguous_form_parents_refuse_without_publishing(self):
        body = (
            "<form><div><p>"
            + ("Form content with one paragraph. " * 90)
            + "</p></div></form>"
        )
        p, _, output, receipt = self.render(
            body, extra_css="p{font:14pt serif;line-height:1.5}"
        )
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "ambiguous_parent")
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())

    def test_existing_idtree_refuses_before_writing(self):
        program = """
import os,sys,json
from pathlib import Path
os.environ['DYLD_LIBRARY_PATH']=sys.argv[1]
os.environ['DYLD_FALLBACK_LIBRARY_PATH']=sys.argv[1]
from pypdf import PdfWriter
from pypdf.generic import NameObject,DictionaryObject,ArrayObject
from weasy_continuity._engine import repair
from weasy_continuity.errors import ContinuityError
base=Path(sys.argv[2]);native=base/'has-idtree.pdf';out=base/'must-not-exist.pdf'
writer=PdfWriter();writer.add_blank_page(100,100)
writer.root_object[NameObject('/StructTreeRoot')]=DictionaryObject({NameObject('/Type'):NameObject('/StructTreeRoot'),NameObject('/K'):ArrayObject(),NameObject('/IDTree'):DictionaryObject({NameObject('/Names'):ArrayObject()})})
writer.write(native)
try:repair(native,out,{})
except ContinuityError as err:
 print(json.dumps({'code':err.code,'output_created':out.exists()}))
 sys.exit(2)
sys.exit(0)
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SOURCE)
        p = subprocess.run(
            [sys.executable, "-c", program, NATIVE, str(self.base)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["code"], "unsupported_idtree")
        self.assertFalse(result["output_created"])

    def test_repeated_footer_is_artifact_but_data_cells_remain_logical(self):
        body = table().replace(
            "</tbody></table>",
            "</tbody><tfoot><tr><td>Footer summary</td><td>One logical footer</td></tr></tfoot></table>",
        )
        p, _, _, receipt = self.render(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads(receipt.read_text())
        self.assertEqual(record["after"]["roles"]["/TFoot"], 1)
        self.assertEqual(record["after"]["roles"]["/TD"], 50)
        self.assertEqual(
            {e["subtype"] for e in record["changes"]["retired_mcids"]},
            {"Header", "Footer"},
        )
        self.assertTrue(all(record["checks"].values()))

    def test_same_source_header_with_different_glyphs_refuses(self):
        program = r"""
import os,sys,json
from pathlib import Path
from unittest.mock import patch
os.environ['DYLD_LIBRARY_PATH']=sys.argv[1]
os.environ['DYLD_FALLBACK_LIBRARY_PATH']=sys.argv[1]
from weasyprint import HTML
from pypdf import PdfReader
from pypdf.generic import ContentStream,NameObject,ByteStringObject,ArrayObject
from weasy_continuity._engine import capture_dom,repair
from weasy_continuity.errors import ContinuityError
base=Path(sys.argv[2]);native=base/'mutated.native.pdf';out=base/'refused.pdf'
html=HTML(string=Path(sys.argv[3]).read_text())
with capture_dom(html) as records: html.write_pdf(native,pdf_variant='pdf/ua-1')
r=PdfReader(native)
stream=ContentStream(r.pages[1].get_contents(),r)
inside=False
for operands,op in stream.operations:
 if op==b'BDC' and isinstance(operands[1],dict) and operands[1].get('/MCID')==0: inside=True
 if inside and op==b'TJ':
  operands[0]=ArrayObject([ByteStringObject(b'\x00\xff')]);break
stream.operations=stream.operations
r.pages[1][NameObject('/Contents')]=stream
r.pages[1].indirect_reference.get_object()[NameObject('/Contents')]=stream
try:
 with patch('weasy_continuity._engine.PdfReader',return_value=r): repair(native,out,records)
except ContinuityError as e:
 print(json.dumps({'code':e.code,'output_created':out.exists()}));sys.exit(2)
sys.exit(0)
"""
        source = self.base / "header.html"
        source.write_text(html(table()))
        env = dict(os.environ, PYTHONPATH=str(SOURCE))
        p = subprocess.run(
            [sys.executable, "-c", program, NATIVE, str(self.base), str(source)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["code"], "ambiguous_repeated_header")
        self.assertFalse(result["output_created"])

    def test_data_uri_image_supported(self):
        image = base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="blue"/></svg>'
        ).decode()
        p, _, _, receipt = self.render(
            f'<p>Image</p><img alt="Blue square" src="data:image/svg+xml;base64,{image}">'
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(
            json.loads(receipt.read_text())["after"]["roles"]["/Figure"], 1
        )

    def test_force_replaces_only_complete_matching_pair(self):
        p, _, output, receipt = self.render("<p>First</p>", name="replace")
        self.assertEqual(p.returncode, 0, p.stdout)
        old_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        p, _, _, _ = self.render("<p>Second value</p>", name="replace", force=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotEqual(hashlib.sha256(output.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(command("verify", receipt).returncode, 0)

    def test_timeout_leaves_no_published_pair(self):
        p, _, output, receipt = self.render("<p>Never published</p>", timeout=0.00001)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "worker_timeout")
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())

    def test_existing_output_without_force_stays_unchanged(self):
        p, source, output, receipt = self.render("<p>Original</p>", name="existing")
        self.assertEqual(p.returncode, 0, p.stdout)
        pdf_bytes, receipt_bytes = output.read_bytes(), receipt.read_bytes()
        p, _, _, _ = self.render("<p>Changed source</p>", name="existing")
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "output_exists")
        self.assertEqual(output.read_bytes(), pdf_bytes)
        self.assertEqual(receipt.read_bytes(), receipt_bytes)


if __name__ == "__main__":
    unittest.main()
