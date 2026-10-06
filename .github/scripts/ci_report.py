"""Turn a wall of CI output into a single readable summary.

Two modes, one file, because both are just "read tool output, write markdown":

    ci_report.py ruff  --log findings.txt       >> $GITHUB_STEP_SUMMARY
    ci_report.py pytest --junit junit.xml        >> $GITHUB_STEP_SUMMARY

The point is that when the build is red, the first screen should say what is
actually broken - grouped by file, with counts - instead of 400 lines that
scroll past the information you need.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ruff --output-format=concise writes:  path/to/file.py:12:5: F821 Undefined name `x`
RUFF_LINE = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>\w+)\s+(?P<msg>.*)$")

# The words that make an undefined name obvious enough to group on.
NAME_LINE = re.compile(r"[`\"']([^`\"']+)[`\"']")

MAX_ROWS = 25


def _configure_streams() -> None:
    """Write UTF-8 regardless of host.

    Emoji in a summary table are a real risk on Windows, where the default
    console codec is cp1252 and raises UnicodeEncodeError on the first ✅. The
    project's own probes do the same thing for the same reason.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_lines(path: Path) -> list[str]:
    """Read a log, accepting whatever encoding produced it.

    The dangerous failure here is not an exception - it is decoding into
    nonsense and parsing zero findings from a file that has 138 of them, which
    would print a summary claiming no lint errors at all. PowerShell's `>`
    writes UTF-16LE, git sometimes leaves a BOM, CI writes plain UTF-8, so the
    encoding is sniffed rather than assumed.
    """
    data = path.read_bytes()

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    elif b"\x00" in data[:512]:
        # No BOM but NUL bytes between characters: UTF-16 without a signature.
        encoding = "utf-16-le" if data[:1] != b"\x00" else "utf-16-be"
        text = data.decode(encoding, errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")

    return text.splitlines()


def heading(title: str) -> str:
    return f"\n## {title}\n\n"


def ruff_report(log_path: Path) -> int:
    """Render the findings. Returns how many were found."""
    if not log_path.exists():
        print(heading("Lint") + "_no lint output was produced._")
        return 0

    findings = []
    for raw in read_lines(log_path):
        line = raw.strip()
        if not line or line.startswith("Found ") or line.startswith("[*]"):
            continue
        if match := RUFF_LINE.match(line):
            findings.append({
                "file": match.group("file").replace("\\", "/"),
                "line": match.group("line"),
                "code": match.group("code"),
                "msg": match.group("msg"),
            })

    if not findings:
        lines = read_lines(log_path)
        if lines:
            # Never claim a clean lint when the log simply failed to parse.
            # The build is already failing on its own; a summary saying
            # "no findings" over a 16KB error log would send someone looking
            # in entirely the wrong place.
            print(heading("Lint") + f"⚠️ Ruff produced {len(lines)} line(s) that this "
                  "reporter could not parse. Treat the log as the source of truth.")
            return 0
        print(heading("Lint") + "✅ No findings.")
        return 0

    by_file = collections.Counter(f["file"] for f in findings)
    by_code = collections.Counter(f["code"] for f in findings)

    out = [heading(f"Lint - {len(findings)} finding(s)")]
    out.append("| File | Findings |")
    out.append("|---|---|")
    for file, count in by_file.most_common(MAX_ROWS):
        out.append(f"| `{file}` | {count} |")
    if len(by_file) > MAX_ROWS:
        out.append(f"| _{len(by_file) - MAX_ROWS} more files_ | … |")
    out.append("")

    out.append("| Rule | Count | Meaning |")
    out.append("|---|---|---|")
    meanings = {
        "F821": "undefined name - calling a function that does not exist",
        "F822": "listed in `__all__` but never defined in the module",
    }
    for code, count in by_code.most_common():
        out.append(f"| `{code}` | {count} | {meanings.get(code, '')} |")
    out.append("")

    # The actionable bit: which names are missing, so the owner can read this
    # as a work list rather than as a wall of errors.
    missing = collections.Counter()
    for finding in findings:
        if finding["code"] in ("F821", "F822") and (m := NAME_LINE.search(finding["msg"])):
            missing[m.group(1)] += 1

    if missing:
        out.append("### Missing names, most-referenced first\n")
        out.append("| Name | Referenced by |")
        out.append("|---|---|")
        for name, count in missing.most_common(MAX_ROWS):
            out.append(f"| `{name}` | {count} place(s) |")
        out.append("")

    out.append(
        "\n_Full `file:line` detail is in the log and in this run's annotations._\n"
    )
    sys.stdout.write("".join(line + "\n" for line in out))
    return len(findings)


def _short(message: str, limit: int = 180) -> str:
    text = " ".join((message or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pytest_report(junit_path: Path) -> int:
    """Render the test result. Returns how many tests failed."""
    if not junit_path.exists():
        print(heading("Tests") + "_no test report was produced._")
        return 0

    root = ET.parse(junit_path).getroot()

    # `iter` from the root: pytest writes <pytest><pytest .../></pytest> when
    # there are collection errors, and digging for a specific depth misses the
    # cases nested inside it.
    cases = list(root.iter("testcase"))
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    passed = len(cases) - len(bad) - len(skipped)

    out = [heading(f"Tests - {passed} passed, {len(bad)} failed, {len(skipped)} skipped")]

    if not bad:
        out.append("✅ All tests passed.")
        sys.stdout.write("".join(line + "\n" for line in out))
        return 0

    by_file = collections.Counter(c.get("classname", "?") for c in bad)
    out.append("| Test file | Failures |")
    out.append("|---|---|")
    for file, count in by_file.most_common(MAX_ROWS):
        out.append(f"| `{file}` | {count} |")
    out.append("")

    # Group by why they failed. When 92 tests break for one reason, the count
    # *is* the information - not 92 separate stack traces.
    by_reason = collections.Counter()
    for case in bad:
        node = case.find("failure")
        if node is None:
            node = case.find("error")
        message = node.get("message") if node is not None else ""
        message = (message or "").split("\n")[0]
        reason = _short(message) or "no message"
        by_reason[reason] += 1

    out.append("| Reason | Tests affected |")
    out.append("|---|---|")
    for reason, count in by_reason.most_common(MAX_ROWS):
        out.append(f"| `{reason}` | {count} |")
    out.append("")

    if len(bad) > 10:
        out.append(
            f"\n_{len(bad)} tests failed. Listed above by shared cause rather than "
            "one stack trace each - fix the head, and the group goes green._\n"
        )

    sys.stdout.write("".join(line + "\n" for line in out))
    return len(bad)


def main() -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    ruff = sub.add_parser("ruff")
    ruff.add_argument("--log", required=True, type=Path)

    pytest_mode = sub.add_parser("pytest")
    pytest_mode.add_argument("--junit", required=True, type=Path)

    args = parser.parse_args()
    if args.mode == "ruff":
        ruff_report(args.log)
    else:
        pytest_report(args.junit)
    return 0


if __name__ == "__main__":
    sys.exit(main())