"""JSON CLI and receipt-last publication. Help needs only the standard library."""

from __future__ import annotations
import argparse, datetime, hashlib, json, os, subprocess, sys, tempfile, uuid
from pathlib import Path
from . import __version__
from .errors import ContinuityError

LIMITATIONS = [
    "Qualified for WeasyPrint70.0, pypdf6.20.1 and the isolated Python3.12+ CLI.",
    "Self-contained HTML and embedded data URIs; external resources are refused.",
    "Source DOM identity is required; this is not an arbitrary-PDF repair interface.",
    "Ambiguous logical parents and existing IDTree rewriting are refused.",
    "Structural/content checks are not full PDF/UA certification, a screenreader trial or a pixel-render audit.",
    "Each file is atomic; the receipt is the last commit marker. Verify it after any interruption.",
]


def parser():
    p = argparse.ArgumentParser(
        prog="weasy-continuity",
        description="Keep WeasyPrint pagination and content while joining source-identical PDF structure fragments.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser(
        "doctor", help="Check pinned packages and native renderer loading."
    )
    doctor.add_argument(
        "--native-lib",
        type=Path,
        help="Pango/GObject library directory; applied only in the worker.",
    )
    render = sub.add_parser(
        "render",
        help="Render self-contained HTML, verify repair, then publish PDF and receipt.",
    )
    render.add_argument("source", type=Path)
    render.add_argument("-o", "--output", type=Path, required=True)
    render.add_argument("--receipt", type=Path, required=True)
    render.add_argument("--native-lib", type=Path)
    render.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing matching PDF/receipt pair produced by this tool.",
    )
    render.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Worker time limit in seconds (default180).",
    )
    verify = sub.add_parser(
        "verify",
        help="Read a receipt and recheck its PDF hash, structure and preservation fingerprints.",
    )
    verify.add_argument("receipt", type=Path)
    return p


def call_worker(payload, timeout=180):
    try:
        process = subprocess.run(
            [sys.executable, "-m", "weasy_continuity._worker"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContinuityError(
            "worker_timeout",
            "The isolated renderer exceeded its time limit; publication did not begin.",
        ) from exc
    try:
        result = json.loads(process.stdout)
    except (ValueError, TypeError) as exc:
        raise ContinuityError(
            "worker_protocol",
            "The isolated renderer did not return a valid JSON result.",
            {"returncode": process.returncode, "diagnostic": process.stderr[-1000:]},
        ) from exc
    if process.returncode or result.get("status") == "refused":
        error = result.get("error", {})
        raise ContinuityError(
            error.get("code", "worker_failed"),
            error.get("message", "The isolated renderer failed."),
            error.get("details", {}),
        )
    return result


def resolve_paths(source, output, receipt):
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise ContinuityError(
            "source_missing", "The HTML source file does not exist."
        ) from exc
    if not source.is_file():
        raise ContinuityError(
            "invalid_source", "The HTML source must be a regular file."
        )
    paths = []
    for destination in (output, receipt):
        if destination.is_symlink():
            raise ContinuityError(
                "unsafe_output", "Symbolic-link output targets are unsupported."
            )
        destination = destination.absolute()
        if not destination.parent.is_dir():
            raise ContinuityError(
                "output_directory_missing",
                "Create the destination directory before rendering.",
                {"directory": str(destination.parent)},
            )
        paths.append(destination.parent.resolve() / destination.name)
    output, receipt = paths
    for a, b in ((source, output), (source, receipt), (output, receipt)):
        if a == b or (a.exists() and b.exists() and os.path.samefile(a, b)):
            raise ContinuityError(
                "path_collision",
                "Source, PDF and receipt must identify different files.",
            )
    if output.suffix.lower() != ".pdf" or receipt.suffix.lower() != ".json":
        raise ContinuityError(
            "output_format", "Use .pdf for the output and .json for the receipt."
        )
    return source, output, receipt


def read_receipt(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Receipt exceeds8MiB")
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ContinuityError(
            "invalid_receipt", "A complete readable JSON receipt is required."
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("receipt_type") != "weasy-continuity.render"
        or value.get("status") != "complete"
    ):
        raise ContinuityError(
            "invalid_receipt",
            "The file is not a completed Weasy Continuity render receipt.",
        )
    return value


def check_existing(output, receipt, force):
    if not output.exists() and not receipt.exists():
        return
    if not force:
        raise ContinuityError(
            "output_exists",
            "An output already exists; choose new paths or use --force with its matching prior receipt.",
        )
    if not output.is_file() or not receipt.is_file():
        raise ContinuityError(
            "unowned_output",
            "--force requires an existing PDF and its matching complete receipt.",
        )
    from .atomic import digest

    old = read_receipt(receipt)
    try:
        described = Path(old["output"]["path"]).resolve()
        matches = described == output.resolve() and old["output"]["sha256"] == digest(
            output
        )
    except (KeyError, TypeError, ValueError, OSError):
        matches = False
    if not matches:
        raise ContinuityError(
            "unowned_output",
            "--force cannot replace an unrelated file or an incomplete PDF/receipt pair.",
        )


def render_command(args):
    if args.timeout <= 0:
        raise ContinuityError("invalid_timeout", "Worker timeout must be positive.")
    source, output, receipt = resolve_paths(args.source, args.output, args.receipt)
    from .atomic import digest, lock_targets, publish_files

    with lock_targets((output, receipt)):
        check_existing(output, receipt, args.force)
        if source.stat().st_size > 64 * 1024 * 1024:
            raise ContinuityError(
                "source_too_large", "This release accepts HTML files up to64MiB."
            )
        source_bytes = source.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        # Native base, DOM mapping and repaired candidate stay private. Only the
        # verified final PDF and receipt are eligible for publication.
        with tempfile.TemporaryDirectory(
            prefix=".weasy-continuity-", dir=output.parent
        ) as temporary:
            working = Path(temporary)
            snapshot, native, candidate = (
                working / "source.html",
                working / "native.pdf",
                working / "repaired.pdf",
            )
            snapshot.write_bytes(source_bytes)
            result = call_worker(
                {
                    "operation": "render",
                    "source": str(snapshot),
                    "native": str(native),
                    "output": str(candidate),
                    "native_lib": str(args.native_lib.resolve())
                    if args.native_lib
                    else None,
                },
                args.timeout,
            )
            if not source.is_file() or digest(source) != source_hash:
                raise ContinuityError(
                    "source_changed",
                    "The source changed during rendering; the output was not published.",
                )
            # Recheck aliases and ownership after a potentially long renderer run.
            resolve_paths(source, output, receipt)
            check_existing(output, receipt, args.force)
            record = {
                "schema_version": 1,
                "receipt_type": "weasy-continuity.render",
                "status": "complete",
                "run_id": str(uuid.uuid4()),
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "tool": {"name": "weasy-continuity", "version": __version__},
                "input": {
                    "path": str(source),
                    "sha256": source_hash,
                    "bytes": len(source_bytes),
                },
                "output": {
                    "path": str(output),
                    "sha256": digest(candidate),
                    "bytes": candidate.stat().st_size,
                },
                "renderer": result["versions"],
                "changes": result["changes"],
                "checks": result["checks"],
                "before": result["before"],
                "after": result["after"],
                "publication": {
                    "receipt_is_commit_marker": True,
                    "joint_two_file_atomicity": False,
                },
                "limitations": LIMITATIONS,
            }
            # Receipt staging is on its own destination filesystem.
            fd, staged_name = tempfile.mkstemp(
                prefix=".weasy-continuity-receipt-", suffix=".json", dir=receipt.parent
            )
            staged_receipt = Path(staged_name)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(record, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                publish_files(
                    candidate, staged_receipt, output, receipt, force=args.force
                )
            finally:
                staged_receipt.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "status": "complete",
        "output": record["output"],
        "receipt": str(receipt),
        "summary": {
            "pages": record["after"]["pages"],
            "merge_groups": len(record["changes"]["merge_groups"]),
            "roles_before": record["before"]["roles"],
            "roles_after": record["after"]["roles"],
        },
        "checks": record["checks"],
        "limitations": LIMITATIONS,
    }


def verify_command(path):
    record = read_receipt(path)
    from .atomic import digest

    try:
        output = Path(record["output"]["path"])
        expected = record["output"]["sha256"]
    except (KeyError, TypeError) as exc:
        raise ContinuityError(
            "invalid_receipt", "Receipt output path and SHA256 are required."
        ) from exc
    if not output.is_absolute():
        raise ContinuityError(
            "invalid_receipt", "Receipt output path must be absolute."
        )
    if not output.is_file():
        raise ContinuityError(
            "incomplete_publication",
            "The receipt's PDF is missing; this publication is incomplete.",
        )
    if digest(output) != expected:
        raise ContinuityError(
            "receipt_mismatch",
            "PDF SHA256 differs from the receipt; this pair is not a completed matching publication.",
        )
    from .verification import inspect, preservation_checks

    try:
        actual = inspect(output)
        before = record["before"]
        after = record["after"]
        fingerprints_match = all(
            actual.get(k) == after.get(k)
            for k in (
                "pages",
                "roles",
                "mcids",
                "physical_mcids",
                "paint_operator_hashes",
                "annotations",
                "native_content_stream_hashes",
                "text_hashes",
                "protected_page_hashes",
            )
        )
        preservation = all(
            preservation_checks(
                before, actual, record["changes"]["retired_mcids"]
            ).values()
        )
        checks_attested = record["checks"] and all(
            v is True for v in record["checks"].values()
        )
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise ContinuityError(
            "invalid_receipt",
            "Receipt verification fingerprints are incomplete or unreadable.",
        ) from exc
    if (
        actual["errors"]
        or not fingerprints_match
        or not preservation
        or not checks_attested
    ):
        raise ContinuityError(
            "receipt_verification_failed",
            "Current PDF structure or preservation fingerprints differ from the completed receipt.",
            {
                "errors": actual["errors"],
                "fingerprints_match": fingerprints_match,
                "preservation": preservation,
            },
        )
    return {
        "schema_version": 1,
        "status": "verified",
        "receipt": str(path.resolve()),
        "output": record["output"],
        "supported_checks_passed": True,
        "full_accessibility_certified": False,
        "limitations": LIMITATIONS,
    }


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = call_worker(
                {
                    "operation": "doctor",
                    "native_lib": str(args.native_lib.resolve())
                    if args.native_lib
                    else None,
                },
                30,
            )
        elif args.command == "render":
            result = render_command(args)
        else:
            result = verify_command(args.receipt)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ContinuityError as exc:
        print(
            json.dumps(
                {"schema_version": 1, "status": "refused", "error": exc.as_dict()},
                ensure_ascii=False,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "refused",
                    "error": {
                        "code": "io_failed",
                        "message": "The operation failed before a completed publication.",
                        "details": {
                            "type": type(exc).__name__,
                            "reason": str(exc)[:500],
                        },
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
