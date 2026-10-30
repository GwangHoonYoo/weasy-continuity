# ODL artifact recovery reproduction

During the real WeasyPrint issue #2903 verification, OpenDataLoader PDF 2.5.13
read the repaired logical table with one header, but emitted physical repeated
header artifacts as additional prose. The original case and qualified product
scope are documented in [validation.md](validation.md).

The independently authored fixture generator isolates this consumer behavior.
Each one-page PDF has one logical paragraph referencing MCID 0, a matching
ParentTree entry, and separate text inside an Artifact marked-content sequence.
One variant uses bare BMC; the other identifies a pagination/header artifact.
These are diagnostic fixtures, not full PDF/UA conformance examples.

With Python 3.12, pypdf 6.20.1, OpenDataLoader PDF 2.5.13 and Java 17 available:

```sh
python examples/odl_artifact_repro.py --output-dir output/odl-repro --run-reader
```

Both variants produced this Markdown during the recorded run:

```text
ARTIFACT_SENTINEL

BODY_SENTINEL
```

The logical reading order references only BODY_SENTINEL. This isolates artifact
text recovery in structure-tree mode; it does not establish the behavior of
screen readers or other PDF consumers. The generator writes original PDFs and
a JSON result with input hashes and actual extracted text for reproduction.

The issue report and follow-up are tracked separately from product adoption.

## Official 2.5.17 reader verification

With Python 3.12, pypdf 6.20.1, Java 17 and the official reader:

```sh
python -m pip install opendataloader-pdf==2.5.17 pypdf==6.20.1
python examples/odl_artifact_repro.py --output-dir output/odl-2517 --run-reader
```

Both original fixture hashes match the reported inputs. Each now produces only `BODY_SENTINEL`, retaining body text while omitting the artifact. The previous 2.5.16 execution still emitted both sentinels. [Recorded results](odl-2.5.17-results.json) also cover the same retained real-input repaired PDFs, with ordered rows/list items and table headers preserved. Scope is these inputs and structure-tree mode; other consumers and screenreader behavior require separate verification. The fix was shipped upstream in [2.5.17](https://github.com/opendataloader-project/opendataloader-pdf/releases/tag/v2.5.17), following [report 801](https://github.com/opendataloader-project/opendataloader-pdf/issues/801) and [PR 844](https://github.com/opendataloader-project/opendataloader-pdf/pull/844).
