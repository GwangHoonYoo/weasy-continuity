"""Developer effect check on two hashed public originals, not a generic repair algorithm.

Requires PyMuPDF and opendataloader-pdf==2.5.13 plus Java17+, in addition
to the installed CLI. Raw reader outputs and measured results stay in output-dir.
"""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, os, re, subprocess, sys
from pathlib import Path


def run(argv, **kwargs):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=240, **kwargs)
    if p.returncode:
        raise RuntimeError(json.dumps({'argv': argv, 'exit': p.returncode, 'stdout': p.stdout[-4000:], 'stderr': p.stderr[-4000:]}))
    return p.stdout


def normalized(text):
    import html
    return re.sub(r'\s+', '', re.sub(r'<[^>]*>', ' ', html.unescape(text))).replace('*', '')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', required=True, type=Path)
    p.add_argument('--output-dir', required=True, type=Path)
    p.add_argument('--native-lib', type=Path)
    p.add_argument('--java-home', type=Path)
    p.add_argument('--verapdf', type=Path)
    args = p.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if args.native_lib:
        os.environ['DYLD_LIBRARY_PATH'] = str(args.native_lib.resolve())
        os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = str(args.native_lib.resolve())
    if args.java_home:
        os.environ['JAVA_HOME'] = str(args.java_home.resolve())
        os.environ['PATH'] = str(args.java_home.resolve() / 'bin') + os.pathsep + os.environ['PATH']
    import pymupdf, opendataloader_pdf
    from weasyprint import HTML
    from weasy_continuity.verification import inspect
    assert importlib.metadata.version('opendataloader-pdf') == '2.5.13'
    manifest = json.loads(Path(__file__).with_name('public-cases.json').read_text())
    results = []
    for case_id in ['2903', '2873']:
        case = next(x for x in manifest if x['case_id'] == case_id)
        source = (args.source_dir / ('issue' + case_id + '.html')).resolve()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case['sha256'], 'Public source hash mismatch'
        directory = args.output_dir / case_id
        directory.mkdir()
        native, repaired, receipt = [directory / s for s in ['native.pdf', 'repaired.pdf', 'receipt.json']]
        HTML(filename=str(source)).write_pdf(str(native), pdf_variant='pdf/ua-1')
        argv = [sys.executable, '-m', 'weasy_continuity', 'render', str(source), '-o', str(repaired), '--receipt', str(receipt)]
        if args.native_lib:
            argv += ['--native-lib', str(args.native_lib.resolve())]
        cli = json.loads(run(argv))
        verify = json.loads(run([sys.executable, '-m', 'weasy_continuity', 'verify', str(receipt)]))
        assert cli['status'] == 'complete' and verify['status'] == 'verified'
        pages = []
        with pymupdf.open(native) as a, pymupdf.open(repaired) as b:
            assert len(a) == len(b)
            for n, (x, y) in enumerate(zip(a, b), 1):
                xpix, ypix = [q.get_pixmap(matrix=pymupdf.Matrix(2, 2)) for q in [x, y]]
                pages.append({'page': n, 'pixels_144dpi_equal': (xpix.width, xpix.height, xpix.samples) == (ypix.width, ypix.height, ypix.samples), 'visual_text_equal': x.get_text() == y.get_text(), 'links_equal': [{k:v for k,v in link.items() if k != 'xref'} for link in x.get_links()] == [{k:v for k,v in link.items() if k != 'xref'} for link in y.get_links()]})
        assert all(all(x[k] for k in ['pixels_144dpi_equal', 'visual_text_equal', 'links_equal']) for x in pages)
        before, after = inspect(native), inspect(repaired)
        assert after['roles'].get('/Table') == 1 and not after['errors']
        texts = {}
        for name, pdf in [('before', native), ('after', repaired)]:
            destination = directory / name
            opendataloader_pdf.convert(str(pdf), output_dir=str(destination), format='markdown', use_struct_tree=True, quiet=True)
            markdown = destination / (pdf.stem + '.md')
            texts[name] = markdown.read_text()
        headers = [line for line in texts['after'].splitlines() if line.startswith('|')]
        expected = ['No.', 'Name'] if case_id == '2903' else ['Order Line', 'Qty.', 'Description / Service IDs', 'Monthly fee', 'Setup fee']
        actual_header = [normalized(x) for x in headers[0].strip('|').split('|')]
        assert actual_header == [normalized(x) for x in expected], {'case': case_id, 'actual_header': actual_header}
        assert sum(1 for line in headers if normalized(line) == normalized(headers[0])) == 1
        outside = [line.strip() for line in texts['after'].splitlines() if line.strip() and not line.startswith(('|', '- ', '#'))]
        stray = [line for line in outside if any(normalized(label) and normalized(label) in normalized(line) for label in expected)]
        semantic = {'column_headers': actual_header, 'table_header_occurrences': 1, 'stray_artifact_paragraphs': stray, 'whole_document_artifact_filtering_clean': not stray}
        if case_id == '2903':
            table_rows = re.findall(r'^\|(\d+)\|Row (\d+)\|$', texts['after'], re.M)
            items = re.findall(r'^- (\d+)\. Item (\d+)\s*$', texts['after'], re.M)
            assert table_rows == [(str(n), str(n)) for n in range(1, 25)], table_rows
            assert items == [(str(n), str(n)) for n in range(1, 31)], items
            assert after['roles'].get('/L') == 1
            semantic.update(data_rows=len(table_rows), list_items=len(items), list_elements=1)
        validators = {}
        if args.verapdf:
            for name, pdf in [('before', native), ('after', repaired)]:
                output = run([str(args.verapdf.resolve()), '--format', 'json', '-f', 'ua1', str(pdf)])
                (directory / (name + '.verapdf.json')).write_text(output)
                value = json.loads(output)
                reports = value['report']['jobs']
                reports = [r for job in reports for r in job.get('validationResult', [])]
                assert reports and all(x['compliant'] for x in reports), reports
                validators[name] = reports
        results.append({'case': case_id, 'source_sha256': case['sha256'], 'issue_url': case['issue_url'], 'native': before, 'repaired': after, 'page_checks': pages, 'semantic_roundtrip': semantic, 'cli_checks': cli['checks'], 'validator': validators, 'passed': True})
    output = {'passed': True, 'versions': {k: importlib.metadata.version(k) for k in ['weasyprint', 'pypdf', 'PyMuPDF', 'opendataloader-pdf', 'weasy-continuity']}, 'cases': results, 'original_author_adoption_measured': False, 'screenreader_tested': False, 'full_accessibility_certified': False}
    (args.output_dir / 'results.json').write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'passed': True, 'cases': [{'case': x['case'], 'pages': x['native']['pages'], 'before': x['native']['roles'], 'after': x['repaired']['roles'], 'semantic': x['semantic_roundtrip']} for x in results]}))


if __name__ == '__main__':
    main()
