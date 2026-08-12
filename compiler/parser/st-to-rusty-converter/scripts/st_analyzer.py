#!/usr/bin/env python3
"""
ST File Analyzer for ST dialect to RuSTy Conversion

This script scans ST files and generates a transformation list (JSON)
without modifying any files. The output can be reviewed by the user
before applying transformations.

Usage:
    python st_analyzer.py <directory> [--output <output.json>]
"""

import os
import re
import json
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict

from dialect_rules import BLOCK_ENDINGS, DIALECT_PROFILES, active_rules, dialect_choices


def read_text_lossy(file_path: Path) -> str:
    """Read ST source using common encodings without modifying the file."""
    for encoding in ("utf-8-sig", "utf-8", "iso-8859-1", "cp1252"):
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def convert_encoding_to_utf8(st_files: List[Path], verbose: bool = False) -> None:
    """
    Convert all ST files from ISO-8859-1 to UTF-8 encoding.
    Using Python's native open() to ensure cross-platform compatibility (macOS/Linux).
    """
    print("Converting ST files from ISO-8859-1 to UTF-8...")
    converted_count = 0
    error_count = 0

    for file_path in st_files:
        temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        try:
            # 1. 使用 errors='replace' 防止因个别非 ISO 字符导致读取中断
            with open(file_path, "r", encoding="iso-8859-1", errors="replace") as f:
                content = f.read()

            # 2. 以 UTF-8 编码写入临时文件
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)

            # 3. 原子性替换原文件
            temp_path.replace(file_path)
            converted_count += 1
            if verbose:
                print(f"  ✓ Converted: {file_path.name}")

        except Exception as e:
            error_count += 1
            print(f"  ✗ Error converting {file_path.name}: {e}")
            if temp_path.exists():
                temp_path.unlink()  # 使用 pathlib 的删除方式

    print(f"Encoding conversion complete: {converted_count} converted, {error_count} errors\n")


@dataclass
class TransformationItem:
    """Represents a single transformation to be applied."""
    file_path: str
    line_number: int
    rule_id: int
    rule_name: str
    original_text: str
    replacement_text: str
    context: str  # Surrounding code for context
    transform_type: str = "replace"  # "replace" or "append"
    column_start: int = 0  # zero-based, for comment/string-safe replacement
    column_end: int = 0
    safety: str = "syntax_alias"


def mask_non_code(content: str) -> str:
    """Replace comments and string contents with spaces while preserving offsets."""
    out = list(content)
    index = 0
    state = "code"
    while index < len(content):
        pair = content[index : index + 2]
        char = content[index]
        if state == "code" and pair == "(*":
            out[index] = out[index + 1] = " "
            index += 2
            state = "comment"
            continue
        if state == "comment":
            if pair == "*)":
                out[index] = out[index + 1] = " "
                index += 2
                state = "code"
            else:
                if char != "\n":
                    out[index] = " "
                index += 1
            continue
        if state == "code" and pair == "//":
            while index < len(content) and content[index] != "\n":
                out[index] = " "
                index += 1
            continue
        if state == "code" and char in {"'", '"'}:
            quote = char
            out[index] = " "
            index += 1
            while index < len(content):
                if content[index] == "\n":
                    break
                current = content[index]
                out[index] = " "
                index += 1
                if current == quote:
                    break
            continue
        index += 1
    return "".join(out)


def find_st_files(directory: str) -> List[Path]:
    """Recursively find all .st files in the directory."""
    candidate = Path(directory)
    if candidate.is_file():
        return [candidate] if candidate.suffix.lower() == ".st" else []
    st_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith('.st'):
                st_files.append(Path(root) / file)
    return sorted(st_files)


def get_context(lines: List[str], line_idx: int, context_size: int = 2) -> str:
    """Get surrounding lines for context."""
    start = max(0, line_idx - context_size)
    end = min(len(lines), line_idx + context_size + 1)
    return '\n'.join(lines[start:end])


def check_unsupported_pou(
    file_path: Path,
    content: str,
    lines: List[str],
    rules: Dict[int, Dict[str, Any]],
) -> List[TransformationItem]:
    """Report METHOD as unsupported; object-method semantics cannot be guessed."""
    transformations = []
    if 12 not in rules:
        return transformations

    code_lines = mask_non_code(content).split('\n')
    # Check for METHOD keyword (convert to FUNCTION)
    method_pattern = rf'^(\s*)METHOD\b'
    for line_idx, line in enumerate(code_lines):
        match = re.search(method_pattern, line, re.IGNORECASE)
        if match:
            transformations.append(TransformationItem(
                file_path=str(file_path),
                line_number=line_idx + 1,
                rule_id=12,
                rule_name="Unsupported METHOD POU",
                original_text="METHOD",
                replacement_text="",
                context=get_context(lines, line_idx),
                transform_type="unsupported",
                column_start=match.start() + len(match.group(1)),
                column_end=match.end(),
                safety="unsupported",
            ))

    return transformations


def check_missing_end_keyword(file_path: Path, content: str, lines: List[str]) -> Optional[TransformationItem]:
    """Check if the file is missing its END_* keyword (Rule 13).

    Note: This runs AFTER Rule 12 converts METHOD to FUNCTION.
    """

    # Determine block type from file header
    # Order matters: FUNCTION_BLOCK before FUNCTION
    # Note: METHOD will be converted to FUNCTION by Rule 12, so we check for FUNCTION
    block_type = None
    for keyword in ["FUNCTION_BLOCK", "METHOD", "FUNCTION", "PROGRAM"]:
        pattern = rf'^\s*{keyword}\b'
        if re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
            # METHOD will become FUNCTION after Rule 12
            block_type = "FUNCTION" if keyword == "METHOD" else keyword
            break

    if not block_type:
        return None

    # Check if END_* exists anywhere in the file
    # For METHOD files, check for END_FUNCTION (after conversion) or END_METHOD (before conversion)
    end_keyword = BLOCK_ENDINGS[block_type]
    end_pattern = rf'\b({end_keyword}|END_METHOD)\b'

    if re.search(end_pattern, content, re.IGNORECASE):
        return None  # Already has END_* (or END_METHOD which will be converted)

    # Missing END_* - create transformation
    return TransformationItem(
        file_path=str(file_path),
        line_number=len(lines),  # Last line
        rule_id=13,
        rule_name=f"Add missing {end_keyword}",
        original_text="<EOF>",
        replacement_text=end_keyword,
        context=f"File starts with {block_type} but missing {end_keyword}",
        transform_type="append",
        safety="review_required",
    )


def analyze_file(file_path: Path, rules: Dict[int, Dict[str, Any]]) -> List[TransformationItem]:
    """Analyze a single ST file for required transformations."""
    transformations = []

    try:
        content = read_text_lossy(file_path)
        lines = content.split('\n')
        code_content = mask_non_code(content)
        code_lines = code_content.split('\n')
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}")
        return []

    # Check pattern-based rules (1-11)
    for rule_id, rule in rules.items():
        # Skip non-pattern rules (like Rule 12)
        if rule.get("pattern") is None:
            continue

        pattern = re.compile(rule["pattern"], re.IGNORECASE)

        for line_idx, line in enumerate(code_lines):
            for match in pattern.finditer(line):
                original = match.group(0)
                replacement = rule["replacement"](match)

                # Skip if no actual change
                if original == replacement:
                    continue

                # For Rule 7 (array init), check context
                if rule.get("context_required"):
                    context = get_context(lines, line_idx, 5)
                    if 'ARRAY' not in context.upper():
                        continue

                item = TransformationItem(
                    file_path=str(file_path),
                    line_number=line_idx + 1,
                    rule_id=rule_id,
                    rule_name=rule["name"],
                    original_text=original,
                    replacement_text=replacement,
                    context=get_context(lines, line_idx),
                    column_start=match.start(),
                    column_end=match.end(),
                    safety=rule.get("safety", "syntax_alias"),
                )
                transformations.append(item)

    # Check for unsupported POUs like METHOD (Rule 12) - must run before Rule 13
    pou_transforms = check_unsupported_pou(file_path, content, lines, rules)
    transformations.extend(pou_transforms)

    # Check for missing END_* keyword (Rule 13)
    missing_end = check_missing_end_keyword(file_path, content, lines)
    if missing_end and 13 in rules:
        transformations.append(missing_end)

    return transformations


def generate_checklist(directory: str, st_files: List[Path]) -> Dict[str, List[str]]:
    """Generate a directory-structured checklist."""
    checklist = {}
    base = Path(directory)
    if base.is_file():
        base = base.parent

    for file_path in st_files:
        try:
            relative = file_path.relative_to(base)
            parent = str(relative.parent) if relative.parent != Path('.') else '.'
            if parent not in checklist:
                checklist[parent] = []
            checklist[parent].append(relative.name)
        except ValueError:
            checklist['.'] = checklist.get('.', [])
            checklist['.'].append(file_path.name)

    return checklist


def main():
    parser = argparse.ArgumentParser(
        description='Analyze ST files for Codesys to RuSTy conversion'
    )
    parser.add_argument('directory', help='ST file or directory containing ST files')
    parser.add_argument(
        '--output', '-o',
        default='transformations.json',
        help='Output JSON file for transformation list'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Print detailed analysis'
    )
    parser.add_argument(
        '--dialect',
        choices=dialect_choices(),
        default='codesys',
        help='Dialect profile to normalize (default: codesys)'
    )
    parser.add_argument(
        '--convert-encoding',
        action='store_true',
        help='Rewrite ST files to UTF-8 before analysis; disabled by default'
    )
    args = parser.parse_args()

    directory = os.path.abspath(args.directory)

    if not os.path.exists(directory) or (os.path.isfile(directory) and not directory.lower().endswith('.st')):
        print(f"Error: {directory} is not a valid ST file or directory")
        return 1

    print(f"Scanning input: {directory}")
    print(f"Dialect profile: {args.dialect} - {DIALECT_PROFILES[args.dialect]}")
    print("=" * 60)

    # Find all ST files
    st_files = find_st_files(directory)
    print(f"Found {len(st_files)} ST file(s)\n")

    if not st_files:
        print("No ST files found.")
        return 0

    rules = active_rules(args.dialect)

    if args.convert_encoding:
        convert_encoding_to_utf8(st_files, verbose=args.verbose)

    # Generate checklist
    checklist = generate_checklist(directory, st_files)
    print("## File Checklist\n")
    for folder, files in sorted(checklist.items()):
        print(f"### {folder}/")
        for f in files:
            print(f"  - [ ] {f}")
    print()

    # Analyze each file
    all_transformations = []
    files_with_issues = 0
    rule_counts = {i: 0 for i in rules.keys()}

    for file_path in st_files:
        transformations = analyze_file(file_path, rules)

        if transformations:
            files_with_issues += 1
            all_transformations.extend(transformations)

            for t in transformations:
                rule_counts[t.rule_id] += 1

            if args.verbose:
                print(f"\n### {file_path.name}")
                for t in transformations:
                    print(f"  Line {t.line_number}: {t.rule_name}")
                    print(f"    - Original: {t.original_text}")
                    print(f"    + Replace:  {t.replacement_text}")

    # Summary
    print("\n" + "=" * 60)
    print("## Analysis Summary\n")
    print(f"- **Total Files Scanned**: {len(st_files)}")
    print(f"- **Files Needing Changes**: {files_with_issues}")
    print(f"- **Total Transformations**: {len(all_transformations)}")

    print("\n### Issues by Rule\n")
    print("| Rule | Description | Count |")
    print("|------|-------------|-------|")
    for rule_id, count in sorted(rule_counts.items()):
        if count > 0:
            print(f"| {rule_id} | {rules[rule_id]['name']} | {count} |")

    # Save to JSON
    output_data = {
        "source_directory": directory,
        "dialect": args.dialect,
        "total_files": len(st_files),
        "files_with_issues": files_with_issues,
        "transformations": [asdict(t) for t in all_transformations],
        "unsupported_constructs": [
            asdict(t) for t in all_transformations if t.safety == "unsupported"
        ],
        "rule_summary": {
            str(k): {"name": rules[k]["name"], "count": v}
            for k, v in rule_counts.items() if v > 0
        }
    }

    output_path = args.output
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Transformation list saved to: {output_path}")
    print("\nReview the JSON file, then run st_converter.py to apply changes.")

    return 0


if __name__ == '__main__':
    exit(main())
