"""Read-only supported structural checks; no renderer or native library import."""

from __future__ import annotations
from collections import Counter
from pathlib import Path
import json, hashlib
from pypdf import PdfReader
from pypdf.generic import ContentStream, IndirectObject, StreamObject
from .errors import ContinuityError
from .marked_content import artifact_stream, paint_hash


def protected_page_hash(page):
    """Canonical reference-number-independent fingerprint of page resources.

    Content streams are hashed separately. Ignore document-level structure edits;
    retain geometry, resource bytes and annotations, including their native data.
    """
    seen = {}

    def canonical(value):
        if isinstance(value, IndirectObject):
            identity = (value.idnum, value.generation)
            if identity in seen:
                return {"$ref": seen[identity]}
            number = len(seen)
            seen[identity] = number
            return {"$id": number, "value": canonical(value.get_object())}
        if isinstance(value, StreamObject):
            return {
                "stream_sha256": hashlib.sha256(value.get_data()).hexdigest(),
                "dict": {
                    str(k): canonical(v)
                    for k, v in sorted(value.items(), key=lambda x: str(x[0]))
                    if str(k) not in {"/Length", "/Filter", "/DecodeParms"}
                },
            }
        if isinstance(value, dict):
            return {
                str(k): canonical(v)
                for k, v in sorted(value.items(), key=lambda x: str(x[0]))
            }
        if isinstance(value, list):
            return [canonical(x) for x in value]
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)

    selected = {
        k: page.raw_get(k)
        for k in ("/Resources", "/MediaBox", "/CropBox", "/Rotate", "/Annots", "/Tabs")
        if k in page
    }
    encoded = json.dumps(
        canonical(selected), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compare(native: Path, repaired: Path, changes=None):
    try:
        before, after = inspect(native), inspect(repaired)
    except ContinuityError:
        raise
    except Exception as exc:
        raise ContinuityError(
            "inspection_failed",
            "The generated PDF cannot be inspected within the qualified scope.",
            {"reason": str(exc)[:500]},
        ) from exc
    retired = (changes or {}).get("retired_mcids", [])
    checks = preservation_checks(before, after, retired)
    a_reader, z_reader = PdfReader(native), PdfReader(repaired)
    edits = {}
    for entry in retired:
        edits.setdefault(entry["page"], {})[entry["mcid"]] = entry["subtype"]
    checks["only_qualified_repeated_section_metadata_changed"] = all(
        artifact_stream(a.get_contents().get_data(), a_reader, edits.get(i, {}))
        == z.get_contents().get_data()
        for i, (a, z) in enumerate(zip(a_reader.pages, z_reader.pages))
    )
    if not all(checks.values()):
        raise ContinuityError(
            "verification_failed",
            "The repaired PDF failed supported structural or preservation checks.",
            {"checks": checks, "errors": after["errors"]},
        )
    return {"checks": checks, "before": before, "after": after}


def preservation_checks(before, after, retired):
    removed = {(e["page"], e["mcid"]) for e in retired}
    expected = [
        [m for m in mcids if (pi, m) not in removed]
        for pi, mcids in enumerate(before["physical_mcids"])
    ]
    original = {
        (pi, m) for pi, vals in enumerate(before["physical_mcids"]) for m in vals
    }
    return {
        "native_structural_consistency": not before["errors"],
        "structural_consistency": not after["errors"],
        "page_count_preserved": before["pages"] == after["pages"],
        "paint_and_text_operator_bytes_preserved": before["paint_operator_hashes"]
        == after["paint_operator_hashes"],
        "page_resources_geometry_annotations_preserved": before["protected_page_hashes"]
        == after["protected_page_hashes"],
        "qualified_mcid_retirement_exact": len(removed) == len(retired)
        and removed <= original
        and expected == after["physical_mcids"]
        and before["mcids"] - after["mcids"] == len(removed),
        "extracted_text_preserved": before["text_hashes"] == after["text_hashes"],
        "annotation_count_preserved": before["annotations"] == after["annotations"],
    }


def key(ref):
    return ref.idnum, ref.generation


def inspect(path):
    reader = PdfReader(path)
    root = reader.trailer["/Root"]["/StructTreeRoot"]
    nodes = {}
    counts = Counter()
    errors = []
    ids = {}
    headers = []
    roles_order = []
    referenced_mcids = []
    object_refs = []

    def walk(ref, parent):
        x = ref.get_object()
        if not isinstance(x, dict) or "/S" not in x:
            return
        k = key(ref)
        if k in nodes:
            errors.append(["duplicate_or_cycle", k])
            return
        nodes[k] = x
        counts[str(x["/S"])] += 1
        roles_order.append(str(x["/S"]))
        p = x.raw_get("/P")
        if not hasattr(p, "idnum") or key(p) != key(parent):
            errors.append(["struct_parent_mismatch", k])
        if "/ID" in x:
            identifier = str(x["/ID"])
            if identifier in ids:
                errors.append(["duplicate_id", identifier])
            ids[identifier] = k
        attrs = x.get("/A", {})
        attrs = attrs if isinstance(attrs, list) else [attrs]
        for a in attrs:
            if isinstance(a, dict):
                headers.extend(str(s) for s in a.get("/Headers", []))
        kids = x.get("/K", [])
        kids = kids if isinstance(kids, list) else [kids]
        for child in kids:
            if isinstance(child, int):
                pg = x.raw_get("/Pg") if "/Pg" in x else None
                referenced_mcids.append(
                    (key(pg) if hasattr(pg, "idnum") else None, int(child), k)
                )
            elif hasattr(child, "get_object"):
                y = child.get_object()
                if isinstance(y, dict) and "/S" in y:
                    walk(child, ref)
                elif isinstance(y, dict) and y.get("/Type") == "/MCR":
                    pg = y.raw_get("/Pg") if "/Pg" in y else x.raw_get("/Pg")
                    referenced_mcids.append((key(pg), int(y["/MCID"]), k))
                elif isinstance(y, dict) and y.get("/Type") == "/OBJR":
                    object_refs.append((key(y.raw_get("/Obj")), k))

    for child in root["/K"]:
        walk(child, root.indirect_reference)
    for name in headers:
        if name not in ids:
            errors.append(["missing_header_id", name])
    pt = {}

    def numbertree(x):
        x = x.get_object()
        nums = x.get("/Nums", [])
        for i in range(0, len(nums), 2):
            n = int(nums[i])
            if n in pt:
                errors.append(["duplicate_parenttree_key", n])
            pt[n] = nums[i + 1].get_object()
        for child in x.get("/Kids", []):
            numbertree(child)

    numbertree(root["/ParentTree"])
    page_keys = {key(p.indirect_reference): p for p in reader.pages}
    mcid_count = 0
    physical_pairs = Counter()
    physical_by_page = []
    for page in reader.pages:
        pg = key(page.indirect_reference)
        struct_key = int(page["/StructParents"])
        mapping = pt.get(struct_key)
        if not isinstance(mapping, list):
            errors.append(["missing_page_mapping", pg])
            continue
        physical = []
        for operands, operation in ContentStream(
            page.get_contents(), reader
        ).operations:
            if (
                operation == b"BDC"
                and len(operands) > 1
                and isinstance(operands[1], dict)
                and "/MCID" in operands[1]
            ):
                value = operands[1]["/MCID"]
                if not isinstance(value, int) or isinstance(value, bool):
                    errors.append(["noninteger_physical_mcid", pg, str(value)])
                    continue
                physical.append(int(value))
        physical_by_page.append(physical)
        physical_pairs.update((pg, m) for m in physical)
        if len(physical) != len(set(physical)):
            errors.append(["repeated_page_mcid", pg])
        for mcid in physical:
            mcid_count += 1
            if mcid < 0:
                errors.append(["negative_physical_mcid", pg, mcid])
                continue
            if mcid >= len(mapping) or not hasattr(mapping[mcid], "idnum"):
                errors.append(["missing_mcid_parent", pg, mcid])
                continue
            target = key(mapping[mcid])
            if target not in nodes:
                errors.append(["unreachable_parenttree_target", pg, mcid])
                continue
            if (pg, mcid, target) not in referenced_mcids:
                errors.append(["mcid_reverse_mismatch", pg, mcid, target])
        for mid, target in enumerate(mapping):
            if hasattr(target, "idnum") and (pg, mid) not in physical_pairs:
                errors.append(["parenttree_ghost_mcid", pg, mid])
    logical_pairs = Counter((pg, mid) for pg, mid, _ in referenced_mcids)
    for pair, count in logical_pairs.items():
        if pair[1] < 0:
            errors.append(["negative_logical_mcid", pair])
        if count != 1:
            errors.append(["duplicate_logical_mcid", pair, count])
    if physical_pairs != logical_pairs:
        errors.append(
            [
                "physical_logical_mcid_mismatch",
                {"physical": len(physical_pairs), "logical": len(logical_pairs)},
            ]
        )
    for pg, mcid, target in referenced_mcids:
        if pg not in page_keys:
            errors.append(["dangling_page", target])
            continue
        arr = pt.get(int(page_keys[pg]["/StructParents"]))
        if (
            not isinstance(arr, list)
            or mcid < 0
            or mcid >= len(arr)
            or not hasattr(arr[mcid], "idnum")
            or key(arr[mcid]) != target
        ):
            errors.append(["struct_mcid_forward_mismatch", target, mcid])
    annotation_lookup = {}
    for page in reader.pages:
        for ref in page.get("/Annots", []):
            annotation_lookup[key(ref)] = ref.get_object()
    for annot_key, owner in object_refs:
        annot = annotation_lookup.get(annot_key)
        if annot is None:
            errors.append(["objr_annotation_missing", annot_key])
            continue
        if "/StructParent" not in annot:
            errors.append(["annotation_structparent_missing", annot_key])
            continue
        parent = pt.get(int(annot["/StructParent"]))
        if not isinstance(parent, dict) or key(parent.indirect_reference) != owner:
            errors.append(["annotation_parenttree_mismatch", annot_key])
    idtree_count = 0
    if "/IDTree" in root:

        def nametree(x):
            nonlocal idtree_count
            x = x.get_object()
            names = x.get("/Names", [])
            for i in range(0, len(names), 2):
                idtree_count += 1
                if str(names[i]) not in ids or key(names[i + 1]) != ids[str(names[i])]:
                    errors.append(["idtree_mismatch", str(names[i])])
            for child in x.get("/Kids", []):
                nametree(child)

        nametree(root["/IDTree"])
    return {
        "pages": len(reader.pages),
        "roles": dict(counts),
        "mcids": mcid_count,
        "physical_mcids": physical_by_page,
        "paint_operator_hashes": [
            paint_hash(p.get_contents().get_data(), reader) for p in reader.pages
        ],
        "header_ids": len(ids),
        "header_references": len(headers),
        "idtree_present": "/IDTree" in root,
        "idtree_count": idtree_count,
        "annotations": sum(len(p.get("/Annots", [])) for p in reader.pages),
        "objr_count": len(object_refs),
        "errors": errors,
        "native_content_stream_hashes": [
            hashlib.sha256(p.get_contents().get_data()).hexdigest()
            for p in reader.pages
        ],
        "text_hashes": [
            hashlib.sha256((p.extract_text() or "").encode()).hexdigest()
            for p in reader.pages
        ],
        "protected_page_hashes": [protected_page_hash(p) for p in reader.pages],
    }
