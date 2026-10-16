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
