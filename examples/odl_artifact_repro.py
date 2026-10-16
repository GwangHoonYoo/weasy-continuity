"""Generate two original tagged-PDF fixtures for ODL artifact recovery.

Run: python examples/odl_artifact_repro.py --output-dir output/odl-repro --run-reader
The optional reader run requires opendataloader-pdf 2.5.13 and Java 17+.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                          DictionaryObject, NameObject, NumberObject, TextStringObject)


def generate(path, artifact_start):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica')}))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data((artifact_start + '\n'
        'BT /F1 12 Tf 40 260 Td (ARTIFACT_SENTINEL) Tj ET\nEMC\n'
        '/P <</MCID 0>> BDC\n'
        'BT /F1 12 Tf 40 200 Td (BODY_SENTINEL) Tj ET\nEMC\n').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    page[NameObject('/StructParents')] = NumberObject(0)
    tree = DictionaryObject({NameObject('/Type'): NameObject('/StructTreeRoot')})
    tree_ref = writer._add_object(tree)
    paragraph = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/StructElem'),
        NameObject('/S'): NameObject('/P'), NameObject('/P'): tree_ref,
        NameObject('/Pg'): page.indirect_reference, NameObject('/K'): NumberObject(0)}))
    tree[NameObject('/K')] = ArrayObject([paragraph])
    tree[NameObject('/ParentTree')] = writer._add_object(DictionaryObject({
        NameObject('/Nums'): ArrayObject([NumberObject(0), ArrayObject([paragraph])])}))
    tree[NameObject('/ParentTreeNextKey')] = NumberObject(1)
    writer._root_object[NameObject('/StructTreeRoot')] = tree_ref
    writer._root_object[NameObject('/MarkInfo')] = DictionaryObject({NameObject('/Marked'): BooleanObject(True)})
    writer._root_object[NameObject('/Lang')] = TextStringObject('en')
    writer.write(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--run-reader', action='store_true')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = {'pypdf': importlib.metadata.version('pypdf'), 'fixtures': []}
    for variant, start in [('bare', '/Artifact BMC'),
                           ('pagination', '/Artifact <</Type /Pagination /Subtype /Header>> BDC')]:
        path = args.output_dir / (variant + '.pdf')
        generate(path, start)
        record = {'variant': variant, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if args.run_reader:
            import opendataloader_pdf
            result['opendataloader_pdf'] = importlib.metadata.version('opendataloader-pdf')
            output = args.output_dir / variant
            output.mkdir(exist_ok=True)
            opendataloader_pdf.convert(input_path=str(path), output_dir=str(output),
                                      format='markdown,json', use_struct_tree=True, quiet=True)
            text = (output / (variant + '.md')).read_text()
            record.update(markdown=text, body_in_output='BODY_SENTINEL' in text,
                          artifact_in_output='ARTIFACT_SENTINEL' in text)
        result['fixtures'].append(record)
    (args.output_dir / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
