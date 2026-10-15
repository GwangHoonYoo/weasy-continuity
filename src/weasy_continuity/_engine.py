"""Private worker-only renderer instrumentation and DOM identity repair.

Import only in the isolated worker after configuring its native library path.
The global instrumentation is restored in finally and never shared with an
embedding caller. WeasyPrint's implementation is called, not copied here.
"""

from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from contextlib import contextmanager
import weasyprint
import weasyprint.pdf.tags as wp_tags
from weasyprint.layout.absolute import AbsolutePlaceholder
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    NameObject,
    TextStringObject,
    NullObject,
    DecodedStreamObject,
)
from .errors import ContinuityError
from .marked_content import page_blocks, glyph_signature, artifact_stream

ROLES = {
    "/Table",
    "/THead",
    "/TBody",
    "/TFoot",
    "/TR",
    "/TH",
    "/TD",
    "/L",
    "/LI",
    "/LBody",
    "/Lbl",
    "/P",
    "/H1",
    "/H2",
    "/H3",
    "/H4",
    "/H5",
    "/H6",
    "/Div",
    "/Sect",
    "/BlockQuote",
}


def element_paths(root):
    result = {}

    def walk(e, path):
        result[id(e)] = path
        for i, child in enumerate(e):
            walk(child, f"{path}/{child.tag}[{i}]")

    walk(root, str(root.tag))
    return result


@contextmanager
def capture_dom(html):
    original = wp_tags._build_box_tree
    records = {}
    paths = element_paths(html.etree_element)

    def instrument(box, *args, **kwargs):
        source_box = box._box if isinstance(box, AbsolutePlaceholder) else box
        for node in original(box, *args, **kwargs):
            source = getattr(source_box, "element", None)
            role = node.get("S")
            if source is not None and role in ROLES:
                path = paths.get(id(source))
                if path is not None:
                    # html/body forwarding generators yield descendants again. Keep the
                    # innermost producer's source identity rather than an ancestor's.
                    records.setdefault(
                        node.number,
                        {"dom_path": path, "role": role, "source_tag": source.tag},
                    )
            yield node

    wp_tags._build_box_tree = instrument
    try:
        yield records
    finally:
        wp_tags._build_box_tree = original


def node_key(ref):
    return ref.idnum, ref.generation


def repair(native_pdf: Path, output: Path, records: dict):
    r = PdfReader(native_pdf)
    w = PdfWriter()
    w.clone_document_from_reader(r)
    root = w.root_object["/StructTreeRoot"]
    if "/IDTree" in root:
        raise ContinuityError(
            "unsupported_idtree",
            "Existing IDTree rewriting is outside the qualified scope.",
        )
    all_nodes = {}
    ordered = []

    def collect(ref):
        x = ref.get_object()
        if not isinstance(x, dict) or "/S" not in x:
            return
        key = node_key(ref)
        if key in all_nodes:
            raise ContinuityError(
                "invalid_structure",
                "Renderer output has duplicate structural references.",
            )
        all_nodes[key] = x
        ordered.append(key)
        for kid in x.get("/K", []):
            if hasattr(kid, "idnum"):
                collect(kid)

    for child in root["/K"]:
        collect(child)
    # pypdf assigns new object numbers while cloning. Source reader references
    # identify the old WeasyPrint numbers; construct correspondence via source
    # graph traversal in parallel rather than assuming clone-number identity.
    source_root = r.trailer["/Root"]["/StructTreeRoot"]
    source_to_clone = {}

    def correlate(a_ref, z_ref):
        a, z = a_ref.get_object(), z_ref.get_object()
        if not isinstance(a, dict) or "/S" not in a:
            return
        source_to_clone[a_ref.idnum] = node_key(z_ref)
        a_refs = [
            x
            for x in a.get("/K", [])
            if hasattr(x, "idnum")
            and isinstance(x.get_object(), dict)
            and "/S" in x.get_object()
        ]
        z_refs = [
            x
            for x in z.get("/K", [])
            if hasattr(x, "idnum")
            and isinstance(x.get_object(), dict)
            and "/S" in x.get_object()
        ]
        if len(a_refs) != len(z_refs):
            raise ContinuityError(
                "clone_mismatch",
                "The PDF clone graph diverged from the renderer output.",
            )
        for aa, zz in zip(a_refs, z_refs):
            correlate(aa, zz)

    for a, z in zip(source_root["/K"], root["/K"]):
        correlate(a, z)
    group_lookup = defaultdict(list)
    mapped = {}
    for original_number, record in records.items():
        key = source_to_clone.get(original_number)
        if key is None:
            continue  # Disconnected margin-box artifacts are out of scope.
        if str(all_nodes[key]["/S"]) != record["role"]:
            raise ContinuityError(
                "mapping_mismatch",
                "Source DOM mapping does not match the generated PDF role.",
            )
        mapped[key] = record
    for key in ordered:
        if key in mapped:
            record = mapped[key]
            group_lookup[(record["dom_path"], record["role"])].append(key)
    aliases = {key: keys[0] for keys in group_lookup.values() for key in keys}

    def canonical(key):
        return aliases.get(key, key)

    groups = [keys for keys in group_lookup.values() if len(keys) > 1]
    # A single source node must not have unrelated logical parents. Refuse instead
    # of using source identity alone to merge incompatible logical contexts.
    for keys in groups:
        parents = {canonical(node_key(all_nodes[k].raw_get("/P"))) for k in keys}
        if len(parents) != 1:
            raise ContinuityError(
                "ambiguous_parent",
                "One source element has different logical PDF parent contexts.",
                mapped[keys[0]],
            )
    # Header/footer copies are paint repetitions, not additional logical text.
    # Qualification requires the same DOM identity at every row/cell, matching
    # text-only glyph programs modulo translation, and separate physical pages.
    pages = {node_key(p.indirect_reference): i for i, p in enumerate(w.pages)}
    blocks = [page_blocks(p, w) for p in w.pages]
    retired_nodes = set()
    retire_by_page = defaultdict(dict)
    repeated_sections = []

    def section(refkey):
        identities, physical, descendants = [], [], []

        def visit(k):
            x = all_nodes[k]
            descendants.append(k)
            role = str(x["/S"])
            identities.append(("role", role))
            if role not in ROLES and role not in {"/Span", "/NonStruct"}:
                raise ContinuityError(
                    "ambiguous_repeated_header",
                    "Repeated header/footer contains an unqualified semantic role.",
                )
            if role in ROLES:
                rec = mapped.get(k)
                if rec is None:
                    raise ContinuityError(
                        "ambiguous_repeated_header",
                        "A repeated header/footer descendant has no source DOM identity.",
                    )
                identities.append((role, rec["dom_path"]))
            kids = x.get("/K", [])
            kids = kids if isinstance(kids, list) else [kids]
            for child in kids:
                if isinstance(child, int):
                    if "/Pg" not in x:
                        raise ContinuityError(
                            "ambiguous_repeated_header",
                            "A header/footer MCID has no direct page association.",
                        )
                    pi = pages[node_key(x.raw_get("/Pg"))]
                    physical.append((pi, int(child)))
                elif hasattr(child, "idnum") and node_key(child) in all_nodes:
                    visit(node_key(child))
                else:
                    raise ContinuityError(
                        "ambiguous_repeated_header",
                        "Unsupported repeated header/footer content reference.",
                    )

        visit(refkey)
        if len({pi for pi, _ in physical}) > 1:
            raise ContinuityError(
                "ambiguous_repeated_header",
                "One repeated header/footer fragment spans multiple pages.",
            )
        try:
            rendered = glyph_signature([blocks[pi][mcid] for pi, mcid in physical])
        except KeyError as exc:
            raise ContinuityError(
                "missing_header_mcid",
                "Header/footer structure references absent physical content.",
            ) from exc
        return identities, physical, descendants, rendered

    for keys in groups:
        role = mapped[keys[0]]["role"]
        if role not in {"/THead", "/TFoot"}:
            continue
        original = section(keys[0])
        used_pages = {pi for pi, _ in original[1]}
        for k in keys[1:]:
            repeat = section(k)
            repeated_pages = {pi for pi, _ in repeat[1]}
            if (
                repeat[0] != original[0]
                or repeat[3] != original[3]
                or used_pages & repeated_pages
            ):
                raise ContinuityError(
                    "ambiguous_repeated_header",
                    "Same-source header/footer copies differ in DOM shape, glyph content, relative placement or page identity.",
                    {"dom_path": mapped[k]["dom_path"], "role": role},
                )
            used_pages.update(repeated_pages)
            retired_nodes.update(repeat[2])
            subtype = "Header" if role == "/THead" else "Footer"
            for pi, mcid in repeat[1]:
                if mcid in retire_by_page[pi]:
                    raise ContinuityError(
                        "ambiguous_repeated_header",
                        "A physical header/footer MCID belongs to multiple repeated sections.",
                    )
                retire_by_page[pi][mcid] = subtype
            repeated_sections.append(
                {
                    "dom_path": mapped[k]["dom_path"],
                    "role": role,
                    "retired_mcids": [
                        {"page": pi, "mcid": mid} for pi, mid in repeat[1]
                    ],
                    "qualification": "same source section/row/cell identities; equal glyph programs and relative positions modulo translation",
                }
            )

    # Retire the exact physical MCIDs without renumbering any remaining content.
    def number_entries(tree):
        tree = tree.get_object()
        nums = tree.get("/Nums", [])
        for i in range(0, len(nums), 2):
            yield int(nums[i]), nums[i + 1].get_object()
        for child in tree.get("/Kids", []):
            yield from number_entries(child)

    parent_entries = dict(number_entries(root["/ParentTree"]))
    for pi, retire in retire_by_page.items():
        page = w.pages[pi]
        stream = DecodedStreamObject()
        stream.set_data(artifact_stream(page.get_contents().get_data(), w, retire))
        page[NameObject("/Contents")] = w._add_object(stream)
        arr = parent_entries[int(page["/StructParents"])]
        for mcid in retire:
            if mcid < 0 or mcid >= len(arr) or not hasattr(arr[mcid], "idnum"):
                raise ContinuityError(
                    "invalid_header_parenttree",
                    "Repeated header/footer has no qualified ParentTree slot.",
                )
            arr[mcid] = NullObject()

    header_aliases = {}
    for keys in groups:
        first = all_nodes[keys[0]]
        if "/ID" in first:
            for key in keys[1:]:
                if "/ID" in all_nodes[key]:
                    header_aliases[str(all_nodes[key]["/ID"])] = str(first["/ID"])
        combined = []
        for key in keys:
            if key not in retired_nodes:
                combined.extend(all_nodes[key].get("/K", []))
        first[NameObject("/K")] = ArrayObject(combined)
    # Canonicalize structural references and deduplicate only exact source aliases.
    # Retained physical MCIDs keep their original page association. Qualified
    # repeated header/footer copies are excluded from the logical structure.
    for key, x in all_nodes.items():
        if canonical(key) != key:
            continue
        kids = []
        seen = set()
        for kid in x.get("/K", []):
            if hasattr(kid, "idnum") and node_key(kid) in all_nodes:
                if node_key(kid) in retired_nodes:
                    continue
                target = canonical(node_key(kid))
                if target in seen:
                    continue
                seen.add(target)
                child = all_nodes[target]
                kid = child.indirect_reference
                child[NameObject("/P")] = x.indirect_reference
            kids.append(kid)
        x[NameObject("/K")] = ArrayObject(kids)
        attrs = x.get("/A")
        if isinstance(attrs, dict) and "/Headers" in attrs:
            attrs[NameObject("/Headers")] = ArrayObject(
                TextStringObject(header_aliases.get(str(h), str(h)))
                for h in attrs["/Headers"]
            )
    w.write(output)
    return {
        "captured_dom_nodes": len(records),
        "mapped_reachable_nodes": len(mapped),
        "merge_groups": [
            {
                "dom_path": mapped[keys[0]]["dom_path"],
                "role": mapped[keys[0]]["role"],
                "fragments": len(keys),
            }
            for keys in groups
        ],
        "header_id_alias_count": len(header_aliases),
        "repeated_sections": repeated_sections,
        "retired_mcids": [
            {"page": pi, "mcid": mid, "subtype": subtype}
            for pi, values in sorted(retire_by_page.items())
            for mid, subtype in sorted(values.items())
        ],
        "matching": "source DOM element identity + PDF role; logical-parent consistency required",
        "source_hash_or_row_count_required": False,
    }


def render(source: Path, native: Path, output: Path):
    blocked = []
    resource_fetcher = weasyprint.urls.URLFetcher()

    def fetch(url, **kwargs):
        if not url.startswith("data:"):
            blocked.append(url.split(":", 1)[0])
            raise ValueError("Only embedded data URI resources are supported.")
        return resource_fetcher(url, **kwargs)

    fetch._fail_on_errors = True
    try:
        html = weasyprint.HTML(filename=source, url_fetcher=fetch)
        with capture_dom(html) as mapping:
            html.write_pdf(native, pdf_variant="pdf/ua-1")
    except (Exception, weasyprint.urls.FatalURLFetchingError) as exc:
        if blocked:
            raise ContinuityError(
                "external_resource",
                "HTML references an external resource; v0 accepts self-contained HTML and data URIs.",
                {"blocked_schemes": sorted(set(blocked))},
            ) from exc
        raise
    if blocked:
        raise ContinuityError(
            "external_resource",
            "A referenced external resource was blocked; publication is refused.",
            {"blocked_schemes": sorted(set(blocked))},
        )
    result = repair(native, output, mapping)
    return result
