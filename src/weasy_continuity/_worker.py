"""Private process entry point. Rendering hooks exist only in this process."""

from __future__ import annotations
import contextlib, importlib.metadata, io, json, os, sys
from pathlib import Path
from .errors import ContinuityError

EXPECTED = {"weasyprint": "70.0", "pypdf": "6.20.1"}


def environment(native_lib):
    if sys.version_info < (3, 12):
        raise ContinuityError("python_version", "Python3.12 or newer is required.")
    if native_lib:
        library = Path(native_lib).resolve()
        if not library.is_dir():
            raise ContinuityError(
                "native_path_missing", "The native library directory does not exist."
            )
        # Set after the interpreter starts; macOS launchers can discard DYLD vars.
        os.environ["DYLD_LIBRARY_PATH"] = str(library)
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = str(library)
    versions = {}
    for name, expected in EXPECTED.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ContinuityError(
                "dependency_missing", f"Install {name}=={expected}."
            ) from exc
        versions[name] = actual
        if actual != expected:
            raise ContinuityError(
                "unsupported_version",
                f"This release requires {name}=={expected}.",
                {"installed": actual},
            )
    try:
        import weasyprint

        if weasyprint.__version__ != EXPECTED["weasyprint"]:
            raise ContinuityError(
                "unsupported_version",
                "Loaded renderer version differs from the pinned package.",
            )
    except (ImportError, OSError) as exc:
        raise ContinuityError(
            "native_dependency",
            "WeasyPrint could not load native Pango/GObject libraries. Run doctor with --native-lib PATH.",
            {"reason": str(exc)[:500]},
        ) from exc
    return {"python": sys.version.split()[0], **versions}


def run(payload):
    versions = environment(payload.get("native_lib"))
    if payload["operation"] == "doctor":
        return {
            "schema_version": 1,
            "status": "ready",
            "versions": versions,
            "native_library_path": payload.get("native_lib"),
            "qualified_interface": "isolated CLI; self-contained HTML/data URIs",
        }
    from ._engine import render
    from .verification import compare

    source, native, output = map(
        Path, (payload["source"], payload["native"], payload["output"])
    )
    changes = render(source, native, output)
    verification = compare(native, output, changes)
    return {
        "status": "validated",
        "versions": versions,
        "changes": changes,
        **verification,
    }


def main():
    try:
        payload = json.load(sys.stdin)
        # Dependency importers may print diagnostics. Preserve JSON-only stdout.
        with contextlib.redirect_stdout(io.StringIO()) as diagnostics:
            result = run(payload)
        extra = diagnostics.getvalue().strip()
        if extra:
            print(extra[:3000], file=sys.stderr)
        print(json.dumps(result))
        return 0
    except ContinuityError as exc:
        print(
            json.dumps(
                {"schema_version": 1, "status": "refused", "error": exc.as_dict()}
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
                        "code": "worker_failed",
                        "message": "Rendering or repair failed before publication.",
                        "details": {
                            "type": type(exc).__name__,
                            "reason": str(exc)[:500],
                        },
                    },
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
