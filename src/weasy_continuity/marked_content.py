"""Exact byte-range marked-content edits, with text-only repeat qualification."""

from __future__ import annotations
import copy
import hashlib
import io
from pypdf.generic import read_object, TextStringObject, ByteStringObject
from .errors import ContinuityError

SPACE = b"\x00\t\n\x0c\r "
DELIMITERS = SPACE + b"()<>[]{}/%"


def operations(data, pdf):
    """Parse PDF operands without finding fake operators inside strings/comments.

    Retain the exact operand/operator byte range. Inline images are deliberately
    refused: their opaque payload requires a different lexical grammar.
    """
    stream = io.BytesIO(data)
    operands = []
    start = None
    while stream.tell() < len(data):
        pos = stream.tell()
        char = stream.read(1)
        if char in SPACE:
            continue
        if char == b"%":
            stream.readline()
            continue
        if start is None:
            start = pos
        stream.seek(pos)
        if (
            char in b"/<[(+-.0123456789"
            or data[pos : pos + 4] in (b"true", b"null")
            or data[pos : pos + 5] == b"false"
        ):
            operands.append(read_object(stream, pdf))
            continue
        end = pos
        while end < len(data) and data[end] not in DELIMITERS:
            end += 1
        if end == pos:
            raise ContinuityError("content_syntax", "Unsupported PDF content token.")
        operator = data[pos:end]
        stream.seek(end)
        if operator == b"BI":
            raise ContinuityError(
                "unsupported_inline_image",
                "Inline image content is outside the qualified marked-content parser.",
            )
        yield start, end, operands, operator
        operands, start = [], None
    if operands:
        raise ContinuityError("content_syntax", "Content stream has trailing operands.")


def stable(value):
    if isinstance(value, (TextStringObject, ByteStringObject)):
        return (
            "bytes",
            value.original_bytes.hex()
            if isinstance(value, TextStringObject)
            else bytes(value).hex(),
        )
    if isinstance(value, list):
        return tuple(stable(v) for v in value)
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    return str(value)


def page_blocks(page, pdf):
    """MCID blocks and glyph signatures. Qualification supports text-only blocks.

    Text positions may differ by translation between header copies; all relative
    glyph positions, glyph bytes, text matrices, fonts, colors and opacity match.
    """
    result, stack, graphics = {}, [], []
    state = {
        "font": None,
        "size": None,
        "color": None,
        "gs": None,
        "ctm": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        "text": {},
    }
    current = None
    for start, end, args, op in operations(page.get_contents().get_data(), pdf):
        if op == b"q":
            graphics.append(copy.deepcopy(state))
        elif op == b"Q":
            if graphics:
                state = graphics.pop()
        elif op == b"cm":
            a, b, c, d, e, f = map(float, args)
            aa, bb, cc, dd, ee, ff = state["ctm"]
            state["ctm"] = (
                a * aa + b * cc,
                a * bb + b * dd,
                c * aa + d * cc,
                c * bb + d * dd,
                e * aa + f * cc + ee,
                e * bb + f * dd + ff,
            )
        elif op == b"Tf":
            font = page["/Resources"]["/Font"].raw_get(str(args[0]))
            state["font"] = (
                (font.idnum, font.generation)
                if hasattr(font, "idnum")
                else str(args[0])
            )
            state["size"] = stable(args[1])
        elif op in (b"rg", b"g", b"k", b"sc", b"scn", b"cs"):
            state["color"] = (op.decode(), stable(args))
        elif op == b"gs":
            state["gs"] = stable(args)
        elif op in (b"Tc", b"Tw", b"Tz", b"TL", b"Tr", b"Ts"):
            state["text"][op.decode()] = stable(args)
        if op in (b"BDC", b"BMC"):
            if current is not None:
                current["unsupported"] = True
            stack.append(current)
            current = None
            if (
                op == b"BDC"
                and len(args) == 2
                and isinstance(args[1], dict)
                and "/MCID" in args[1]
            ):
                mcid = int(args[1]["/MCID"])
                if mcid < 0 or mcid in result:
                    raise ContinuityError(
                        "invalid_mcid", "Negative or repeated page MCID."
                    )
                current = {
                    "range": (start, end),
                    "glyphs": [],
                    "unsupported": False,
                    "mat": None,
                }
                result[mcid] = current
        elif op == b"EMC":
            current = stack.pop() if stack else None
        elif current is not None:
            if op == b"Tm":
                current["mat"] = tuple(float(x) for x in args)
            elif op in (b"TJ", b"Tj"):
                if current["mat"] is None or state["font"] is None:
                    current["unsupported"] = True
                current["glyphs"].append(
                    (
                        current["mat"],
                        state["font"],
                        state["size"],
                        state["color"],
                        state["gs"],
                        tuple(sorted(state["text"].items())),
                        tuple(round(x, 6) for x in state["ctm"]),
                        stable(args),
                    )
                )
            elif op not in (
                b"gs",
                b"rg",
                b"g",
                b"k",
                b"BT",
                b"ET",
                b"Tf",
                b"Tc",
                b"Tw",
                b"Tz",
                b"TL",
                b"Tr",
                b"Ts",
            ):
                current["unsupported"] = True
    return result


def glyph_signature(blocks):
    glyphs = []
    for block in blocks:
        if block["unsupported"]:
            raise ContinuityError(
                "ambiguous_repeated_header",
                "Repeated table header/footer contains unsupported marked-content operations.",
            )
        glyphs.extend(block["glyphs"])
    if not glyphs:
        return ()

    def positioned(glyph):
        mat, font, size, color, gs, text, ctm, payload = glyph
        a, b, c, d, e, f = ctm
        x, y = mat[4], mat[5]
        return (
            tuple(round(v, 5) for v in mat[:4]),
            x * a + y * c + e,
            x * b + y * d + f,
            font,
            size,
            color,
            gs,
            text,
            ctm[:4],
            payload,
        )

    positioned_glyphs = [positioned(g) for g in glyphs]
    _, origin_x, origin_y, *_ = positioned_glyphs[0]
    return tuple(
        (matrix, round(x - origin_x, 4), round(y - origin_y, 4), *rest)
        for matrix, x, y, *rest in positioned_glyphs
    )


def artifact_stream(data, pdf, retire):
    replacements = []
    found = set()
    for start, end, args, op in operations(data, pdf):
        if (
            op == b"BDC"
            and len(args) == 2
            and isinstance(args[1], dict)
            and "/MCID" in args[1]
        ):
            mcid = int(args[1]["/MCID"])
            if mcid in retire:
                if mcid in found:
                    raise ContinuityError(
                        "invalid_mcid", "Repeated MCID cannot be retired safely."
                    )
                found.add(mcid)
                replacements.append(
                    (
                        start,
                        end,
                        f"/Artifact <</Type /Pagination /Subtype /{retire[mcid]}>> BDC".encode(),
                    )
                )
    if found != set(retire):
        raise ContinuityError(
            "missing_header_mcid",
            "A repeated header MCID is missing from its page stream.",
        )
    for start, end, replacement in reversed(replacements):
        data = data[:start] + replacement + data[end:]
    return data


def paint_hash(data, pdf):
    # Only marked-content operators are excluded; every other original byte,
    # including whitespace, text and painting operators remains fingerprinted.
    ranges = [
        (a, b)
        for a, b, _, op in operations(data, pdf)
        if op in (b"BDC", b"BMC", b"EMC")
    ]
    for a, b in reversed(ranges):
        data = data[:a] + data[b:]
    return hashlib.sha256(data).hexdigest()
