# Validation scope

Two public originals pass the current real-input effect checks. Reports distinguish native structure, repaired structure, actual downstream reading, and visual preservation. A PDF/UA validator pass alone is insufficient evidence of semantic continuity.

## Public inputs

| Case | Author's reported problem | Use in this project |
| --- | --- | --- |
| [WeasyPrint #2903](https://github.com/Kozea/WeasyPrint/issues/2903) | One table/list becomes separate structure elements at page boundaries | Primary real problem and source input |
| [WeasyPrint #2873](https://github.com/Kozea/WeasyPrint/issues/2873) | Multipage table borders | Independent real source input with native fragmented table structure |
| [WeasyPrint #2874](https://github.com/Kozea/WeasyPrint/issues/2874) | Nested list structure | Upstream-fixed regression control |
| [WeasyPrint #2876](https://github.com/Kozea/WeasyPrint/issues/2876) | Header borders | Already-continuous table control, same author as #2873 |
| [WeasyPrint #2726](https://github.com/Kozea/WeasyPrint/issues/2726) | Form content | Unsupported-input control, including an external font dependency |

Attachments are fetched explicitly and checked against `examples/public-cases.json`. Tests created within this repository are synthetic regressions, distinct from the public reporter cases.

## Why semantic roundtrip is required

The initial structure-only implementation merged repeated physical headers under the canonical header cell. It preserved content streams, pixels, text, links, and validator rule passes. OpenDataLoader 2.5.13 with `use_struct_tree=True` nevertheless extracted duplicated header labels (`No. No.` and `Name Name`). Setting `/ActualText` alone did not change that reader's result.

[PDF Association's Tagged PDF Best Practice Guide 1.0.1](https://pdfa.org/download-area/publications/Tagged-PDF-Best-Practice-Guide.pdf), section 4.2.6.2, specifies one table structure across pages and artifact marking for repeated header cells. The release gate therefore requires correct repeated-header semantics and a real downstream extraction, in addition to preservation of all drawing/text operators, pixels, visible text, page resources, geometry and links. Only declared marked-content metadata changes for repeated headers are permitted.

A successful roundtrip on the #2903 input must produce one 24-row data table with each column header once, and one 30-item list. The second public table also needs one occurrence of each header label. Full screenreader testing and original-author adoption remain additional observations.

## Measured corrected implementation

The CLI produces the expected table/list structure on both public originals. PyMuPDF comparisons at 144 dpi preserve all 3 pages of #2903 and both pages of #2873, including visible text and links. veraPDF 1.30.2 passes the same 106 PDF/UA-1 rules before and after on both inputs. The runtime also verifies the exact retired MCID set and preservation of drawing/text operation bytes, resources, page geometry and annotations.

OpenDataLoader 2.5.13 with `use_struct_tree=True` extracts one 24-data-row table and one 30-item list for #2903. Each column header appears once within the table. The second input extracts one table with the five original column headers, also once within its header row.

**Consumer limitation:** OpenDataLoader also emits some correctly artifact-marked repeated physical text as paragraphs outside the table. For #2903 this is `No. Name`; #2873 includes individual header labels and a repeated footer character. Four legal artifact-property variants retained that behavior. The product preserves standards-based artifact marking and records this actual consumer output separately from logical-table correctness. A clean whole-document Markdown export across arbitrary consumers remains outside the verified result.

## Re-run the effect check

With the product installed, add developer-only dependencies `PyMuPDF==1.28.2` and `opendataloader-pdf==2.5.13`, plus Java 17+. Fetch the two public originals as described in the README, then run:

```sh
python examples/validate_public.py --source-dir output/public --output-dir output/effect-check
```

The output directory must be new. Optional `--native-lib PATH`, `--java-home PATH` and `--verapdf PATH` select existing local runtimes. Raw before/after Markdown, PDFs, receipts and measurements remain under that directory. The script's known case expectations are test assertions; the repair algorithm uses source DOM identity rather than those case IDs, hashes or row counts.
