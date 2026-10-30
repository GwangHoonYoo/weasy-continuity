# weasy-continuity

Keep a multipage WeasyPrint table or list as one logical PDF element while preserving its pagination and visible content.

WeasyPrint 70 can split one source table or list into separate structure elements at page breaks ([original report and sample](https://github.com/Kozea/WeasyPrint/issues/2903)). This experimental CLI records source DOM identity during rendering, joins the corresponding fragments, and checks the resulting PDF before publishing it. Its intended users are document pipelines and coding agents that need to retain WeasyPrint's layout.

**Status: experimental.** Two public inputs pass the measured layout-preservation and logical-table checks. The supported interface is the CLI, with Python 3.12, WeasyPrint 70.0 and pypdf 6.20.1 on the qualified macOS environment.

| Public input | Native WeasyPrint 70 | Repaired logical structure | Preserved output |
| --- | --- | --- | --- |
| #2903 | 2 tables, 2 lists | 1 table with 24 data rows, 1 list with 30 items | 3 pages, 144 dpi pixels, visible text and links |
| #2873 | 2 table fragments | 1 table, 5 column headers | 2 pages, 144 dpi pixels, visible text and links |

OpenDataLoader 2.5.17 reads each table's column headers once and excludes the repeated artifact text on these two verified PDFs. The earlier 2.5.13 result and exact input hashes remain in the [versioned validation scope](docs/validation.md). Full screenreader behavior and other documents require separate testing.

## Quick start from a checkout

Requires Python 3.12+ and working [WeasyPrint native dependencies, including Pango](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation). From this repository:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
weasy-continuity doctor
mkdir -p output
weasy-continuity render examples/report.html -o output/report.pdf --receipt output/report.json
weasy-continuity verify output/report.json
```

`render` prints one JSON object containing `status`, a human-readable `summary`, before/after role counts, and preservation checks. A successful `verify` returns `status: "verified"`. PDF and receipt paths must be different, and their directories must already exist. The bundled report is a synthetic demonstration; public reporter inputs are described below.

If Pango is installed in a separate library directory, supply the same path to both rendering commands:

```sh
weasy-continuity doctor --native-lib /absolute/path/to/native/lib
weasy-continuity render examples/report.html -o output/native-report.pdf --receipt output/native-report.json --native-lib /absolute/path/to/native/lib
```

Use an existing directory containing the Pango/GObject shared libraries. The option is applied inside the worker, which also handles macOS environments where launcher processes discard `DYLD_*` variables. `verify` needs the Python package dependencies but does not load Pango.

## Reproduce public cases

The fetch helper downloads author-provided HTML attachments and checks their recorded SHA256. Source URLs and provenance are in [public-cases.json](examples/public-cases.json). Original attachments remain at their source; the repository includes the manifest and downloader.

```sh
python examples/fetch_public_cases.py --case 2903 --case 2873 --output-dir output/public
weasy-continuity render output/public/issue2903.html -o output/issue2903.pdf --receipt output/issue2903.json
weasy-continuity verify output/issue2903.json
```

Case #2903 is the actual table/list fragmentation report. Case #2873 is a different author's real multipage table, originally submitted for a border defect; it provides a second input, rather than a second report of this tagging problem. See [validation](docs/validation.md) for exact measurements, semantic reader results and their limits.

## Agent use

```text
Input: a self-contained HTML file generated for WeasyPrint 70.
1. Run weasy-continuity doctor.
2. Run render with new PDF and receipt paths.
3. Require exit code 0, status=complete, and all supported checks=true.
4. Run verify against that receipt and require status=verified.
5. Explain summary.roles_before/roles_after, remaining limitations, and the PDF path.
```

Commands emit JSON to stdout. Refusals return exit code 2 with `error.code`, `error.message` and optional `error.details`. Run `weasy-continuity --help` or a subcommand's `--help` for the exact options. The renderer runs in a separate process; `render --timeout SECONDS` limits its duration.

The receipt records the source and output hashes, renderer versions, structural changes, qualified repeated-header/footer MCIDs converted to artifacts, supported checks, and before/after fingerprints. Drawing/text operation bytes and page resources stay unchanged; the declared artifact markers replace the repeated fragments' semantic markers. The receipt is the completion marker for a matching PDF/receipt pair. Each file is replaced atomically; interruption between the two replacements can leave an incomplete pair, which `verify` detects. Keep the matching receipt with its PDF. `--force` accepts an existing matching pair produced by this tool. Verification checks file consistency and recorded fingerprints; publisher authentication requires a separate signature mechanism.

## Supported inputs and alternatives

- Self-contained HTML with inline styles and embedded data URIs. External images, stylesheets, fonts and local file fetches produce `external_resource`; embed required resources before rendering.
- Source HTML is required because matching uses DOM identity. The repair depends on WeasyPrint 70's internal rendering hooks and exact pinned package versions.
- Ambiguous logical parents and existing PDF ID trees produce typed refusals. Actual complex-form inputs may also be refused for their external resources.
- Supported structural checks and real-input roundtrips have a defined scope. Full PDF/UA certification, screenreader experience, and compatibility with every PDF consumer require separate validation.

When changing the renderer is acceptable, Chrome or [Fulgur](https://github.com/fulgur-rs/fulgur) can already generate a continuous tagged table/list from the #2903 HTML. They produced different pagination in our local comparison. An upstream WeasyPrint fix would be preferable to maintaining a separate compatibility layer. This project is independently maintained by GwangHoonYoo; upstream endorsement or affiliation has not been established.

## Development and reports

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

Rendering tests require the same native dependencies. For a separately installed native runtime, set `WEASY_CONTINUITY_NATIVE_LIB` to its library directory before running tests.

Report a failing self-contained HTML example, package versions, CLI JSON, and the expected table/list reading behavior in this repository's issue tracker. Include only source documents suitable for public sharing. Changes should include a regression fixture and their observed rendering and semantic effects.

MIT license for this project's code. Third-party dependencies retain their own licenses; see [NOTICE](NOTICE).
