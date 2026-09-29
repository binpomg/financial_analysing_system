"""Safe, optional document parsing used before any financial fact is accepted."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias
from xml.etree import ElementTree

from pydantic import Field

from finagent.contracts import (
    EvidenceSpan,
    ExtractionCandidate,
    StrictModel,
    XbrlTaxonomyMap,
)


class DocumentParseSummary(StrictModel):
    parser: str
    status: Literal[
        "parsed_text_only",
        "parsed_candidates",
        "ocr_required",
        "dependency_unavailable",
        "invalid_document",
        "parser_failed",
    ]
    page_count: int | None = Field(default=None, ge=0)
    text_page_count: int = Field(default=0, ge=0)
    text_characters: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    issues: list[str] = Field(default_factory=list)


def parse_pdf_summary(raw: bytes) -> DocumentParseSummary:
    """Extract only text-layer coverage; never infer a financial field from text."""
    if not raw.startswith(b"%PDF-"):
        return DocumentParseSummary(
            parser="none",
            status="invalid_document",
            issues=["declared PDF has no PDF file signature"],
        )
    try:
        from pypdf import PdfReader
    except ImportError:
        return DocumentParseSummary(
            parser="pypdf",
            status="dependency_unavailable",
            issues=["install the optional document dependency group to parse PDF text"],
        )
    try:
        reader = PdfReader(BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        return DocumentParseSummary(
            parser="pypdf",
            status="parser_failed",
            issues=[f"{type(exc).__name__}: {exc}"],
        )
    text_pages = sum(bool(page.strip()) for page in pages)
    text_characters = sum(len(page) for page in pages)
    return DocumentParseSummary(
        parser="pypdf",
        status="parsed_text_only" if text_pages else "ocr_required",
        page_count=len(pages),
        text_page_count=text_pages,
        text_characters=text_characters,
        issues=(
            ["PDF text extraction is candidate material only; table reconstruction and field verification remain required."]
            if text_pages
            else ["no text layer detected; route each required page to an OCR review queue"]
        ),
    )


def parse_pdf_candidates(
    raw: bytes, document_id: str
) -> tuple[DocumentParseSummary, list[ExtractionCandidate]]:
    """Extract reconstructed numeric table cells when pdfplumber is installed."""
    text_summary = parse_pdf_summary(raw)
    try:
        import pdfplumber
    except ImportError:
        return text_summary, []
    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            page_count = len(pdf.pages)
            tables: list[PdfExtractedTable] = []
            selected_pages = _pdf_financial_page_numbers(raw, page_count)
            for page_number in selected_pages:
                page = pdf.pages[page_number - 1]
                for table_index, table in enumerate(page.extract_tables(), start=1):
                    tables.append(
                        PdfExtractedTable(
                            page_number=page_number,
                            table_index=table_index,
                            rows=table,
                        )
                    )
            candidates, reconstruction_issues = reconstruct_pdf_table_candidates(
                document_id, tables
            )
    except Exception as exc:
        return (
            DocumentParseSummary(
                parser="pdfplumber",
                status="parser_failed",
                issues=[f"{type(exc).__name__}: {exc}"],
            ),
            [],
        )
    if candidates:
        if len(selected_pages) < page_count:
            reconstruction_issues.append(
                f"table extraction was limited to {len(selected_pages)} financial-statement candidate page(s) and adjacent pages"
            )
        return (
            DocumentParseSummary(
                parser="pdfplumber",
                status="parsed_candidates",
                page_count=page_count,
                candidate_count=len(candidates),
                issues=reconstruction_issues,
            ),
            candidates,
        )
    if tables:
        if len(selected_pages) < page_count:
            reconstruction_issues.append(
                f"table extraction was limited to {len(selected_pages)} financial-statement candidate page(s) and adjacent pages"
            )
        return (
            DocumentParseSummary(
                parser="pdfplumber:table-reconstruction-v1",
                status="parsed_candidates",
                page_count=page_count,
                candidate_count=0,
                issues=reconstruction_issues,
            ),
            [],
        )
    return text_summary, []


def _pdf_financial_page_numbers(raw: bytes, page_count: int) -> list[int]:
    """Select text-identified statement pages before expensive table extraction."""
    try:
        from pypdf import PdfReader

        pages = [page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages]
    except Exception:
        return list(range(1, page_count + 1))
    markers = (
        "主要会计数据",
        "资产负债表",
        "利润表",
        "现金流量表",
        "balance sheet",
        "income statement",
        "cash flow statement",
    )
    matched = {
        index + 1
        for index, text in enumerate(pages)
        if any(marker in text.lower() for marker in markers)
    }
    if not matched:
        return list(range(1, page_count + 1))
    expanded = {
        neighbor
        for page_number in matched
        for neighbor in (page_number - 1, page_number, page_number + 1)
        if 1 <= neighbor <= page_count
    }
    return sorted(expanded)


PdfCell: TypeAlias = str | None


@dataclass(frozen=True)
class PdfExtractedTable:
    """Raw table cells from one PDF page before financial interpretation."""

    page_number: int
    table_index: int
    rows: list[list[PdfCell]]


@dataclass(frozen=True)
class _PdfTableHeader:
    cells: list[str | None]
    page_number: int
    table_index: int
    row_index: int


def reconstruct_pdf_table_candidates(
    document_id: str, tables: list[PdfExtractedTable]
) -> tuple[list[ExtractionCandidate], list[str]]:
    """Rebuild simple continued tables while retaining every extraction uncertainty."""

    candidates: list[ExtractionCandidate] = []
    issues = [
        "PDF reconstructed table cells are review candidates; verify the original page, table "
        "structure, unit, period and reporting scope before creating facts."
    ]
    prior_header: _PdfTableHeader | None = None
    prior_unit_hint: str | None = None
    prior_scope_hint: str | None = None
    continued_tables = 0
    skipped_tables = 0
    missing_unit_tables = 0
    ambiguous_scope_tables = 0
    for table in tables:
        rows = [[_clean_pdf_cell(cell) for cell in row] for row in table.rows]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue
        header_row = _pdf_header_row(rows)
        if header_row is not None:
            header = _PdfTableHeader(
                cells=rows[header_row],
                page_number=table.page_number,
                table_index=table.table_index,
                row_index=header_row + 1,
            )
            data_start = header_row + 1
            is_continuation = False
        elif prior_header is not None and _same_column_shape(rows, prior_header.cells):
            header = prior_header
            data_start = _pdf_data_start(rows)
            continued_tables += 1
            is_continuation = True
        else:
            skipped_tables += 1
            continue
        prior_header = header
        table_text = " ".join(cell for row in rows for cell in row if cell)
        unit_hint = _pdf_unit_hint(table_text) or (
            prior_unit_hint if is_continuation else None
        )
        scope_hint = _pdf_scope_hint(table_text) or (
            prior_scope_hint if is_continuation else None
        )
        prior_unit_hint = unit_hint
        prior_scope_hint = scope_hint
        if unit_hint is None:
            missing_unit_tables += 1
        if scope_hint is None and any(token in table_text for token in ("合并", "母公司", "本公司")):
            ambiguous_scope_tables += 1
        active_header = header
        for row_index, row in enumerate(rows[data_start:], start=data_start + 1):
            section_header = _pdf_section_header(row, active_header.cells)
            if section_header is not None:
                active_header = _PdfTableHeader(
                    cells=section_header,
                    page_number=table.page_number,
                    table_index=table.table_index,
                    row_index=row_index,
                )
                continue
            label = _pdf_row_label(row)
            if label is None:
                continue
            for column_index, cell in enumerate(row, start=1):
                if not _is_numeric(cell):
                    continue
                header_cell = (
                    active_header.cells[column_index - 1]
                    if column_index <= len(active_header.cells)
                    else None
                )
                if not header_cell or _is_pdf_label_header(header_cell):
                    continue
                continuation = (
                    f"/header:page:{active_header.page_number}/table:{active_header.table_index}/row:{active_header.row_index}"
                    if active_header.page_number != table.page_number or active_header.table_index != table.table_index
                    else ""
                )
                locator = (
                    f"page:{table.page_number}/table:{table.table_index}/row:{row_index}/"
                    f"column:{column_index}{continuation}"
                )
                excerpt = (
                    f"row={' | '.join(cell for cell in row if cell)}; "
                    f"column_header={header_cell}; unit={unit_hint or 'unresolved'}; "
                    f"scope={scope_hint or 'unresolved'}"
                )
                candidates.append(
                    _candidate(
                        document_id=document_id,
                        label=f"{label} | {header_cell}",
                        raw_value=cell or "",
                        locator=locator,
                        excerpt=excerpt,
                        parser="pdfplumber:table-reconstruction-v2",
                        unit_hint=unit_hint,
                        period_hint=header_cell,
                        scope_hint=scope_hint,
                        block_type="pdf_reconstructed_table_cell",
                        confidence_reason=(
                            "PDF table reconstruction inherited headers only from an adjacent table "
                            "with the same column shape; verify all table, unit, period and scope "
                            "interpretations against the original filing before approval."
                        ),
                    )
                )
    if continued_tables:
        issues.append(f"{continued_tables} table(s) inherited headers from a preceding extracted table")
    if skipped_tables:
        issues.append(f"{skipped_tables} table(s) had no usable header or compatible continuation and produced no candidates")
    if missing_unit_tables:
        issues.append(f"{missing_unit_tables} table(s) had no detected unit; reviewers must supply it")
    if ambiguous_scope_tables:
        issues.append(f"{ambiguous_scope_tables} table(s) mentioned multiple reporting scopes; reviewers must resolve scope")
    return candidates, issues


def _clean_pdf_cell(cell: PdfCell) -> str | None:
    if cell is None:
        return None
    value = " ".join(str(cell).split())
    return value or None


def _pdf_header_row(rows: list[list[str | None]]) -> int | None:
    for index, row in enumerate(rows[:4]):
        cells = [cell for cell in row if cell]
        if len(cells) < 2 or any(_pdf_unit_hint(cell) for cell in cells):
            continue
        has_label_header = any(_is_pdf_label_header(cell) for cell in cells)
        has_period_columns = sum(_is_period_header(cell) for cell in cells) >= 2
        if (has_label_header or has_period_columns) and sum(
            not _is_numeric(cell) for cell in cells
        ) >= 2:
            return index
    return None


def _same_column_shape(rows: list[list[str | None]], header_cells: list[str | None]) -> bool:
    width = max(len(row) for row in rows)
    return width == len(header_cells) and any(
        _is_numeric(cell) for row in rows for cell in row
    )


def _pdf_data_start(rows: list[list[str | None]]) -> int:
    for index, row in enumerate(rows):
        if any(_is_numeric(cell) for cell in row):
            return index
    return len(rows)


def _pdf_section_header(
    row: list[str | None], prior_cells: list[str | None]
) -> list[str | None] | None:
    """Recognize a new period header embedded in a multi-section financial table."""
    if len(row) != len(prior_cells):
        return None
    values = [cell for cell in row if cell]
    if len(values) < 2 or row[0]:
        return None
    period_cells = sum(_is_period_header(cell) for cell in values)
    if period_cells < 2:
        return None
    return [prior_cells[0], *row[1:]]


def _is_period_header(value: str) -> bool:
    import re

    return bool(
        re.search(r"(?:19|20)\d{2}\s*(?:年|年度|year|fy|q[1-4])|本期|上年|期末|期初", value, re.IGNORECASE)
    )


def _pdf_row_label(row: list[str | None]) -> str | None:
    for cell in row:
        if cell and not _is_numeric(cell) and not _pdf_unit_hint(cell):
            label = cell.removeprefix("\u7eed\u8868").strip(" \uff1a:")
            if label and not _is_pdf_label_header(label) and not label.startswith("\u6ce8"):
                return label
    return None


def _is_pdf_label_header(value: str) -> bool:
    return value.strip() in {
        "\u9879\u76ee",
        "\u79d1\u76ee",
        "\u9879\u76ee\u540d\u79f0",
        "\u62a5\u8868\u9879\u76ee",
    } or value.strip().lower() in {"item", "items", "line item"}


def _pdf_unit_hint(text: str) -> str | None:
    import re

    chinese_match = re.search(
        r"\u5355\u4f4d\s*[\uff1a:]?\s*([\u4eba\u6c11\u5e01]*[\u4e07\u4ebf\u5343\u767e]?\u5143|[%\uff05])",
        text,
    )
    if chinese_match:
        return chinese_match.group(1)
    english_match = re.search(
        r"\bunit\s*:\s*([A-Za-z][A-Za-z0-9_ /.-]*)", text, re.IGNORECASE
    )
    return english_match.group(1).strip() if english_match else None


def _pdf_scope_hint(text: str) -> str | None:
    lowered = text.lower()
    consolidated = "\u5408\u5e76" in text or "consolidated" in lowered
    parent = (
        "\u6bcd\u516c\u53f8" in text
        or "\u672c\u516c\u53f8" in text
        or "parent company" in lowered
        or "separate financial" in lowered
    )
    if consolidated == parent:
        return None
    return "consolidated" if consolidated else "parent"


def parse_image_for_ocr(raw: bytes) -> DocumentParseSummary:
    """Register a raster source for OCR without producing any transcribed text."""
    try:
        from PIL import Image
    except ImportError:
        return DocumentParseSummary(
            parser="none",
            status="ocr_required",
            issues=["raster source requires OCR; image dimensions are unavailable without Pillow"],
        )
    try:
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
    except Exception as exc:
        return DocumentParseSummary(
            parser="Pillow",
            status="parser_failed",
            issues=[f"{type(exc).__name__}: {exc}"],
        )
    return DocumentParseSummary(
        parser="Pillow",
        status="ocr_required",
        issues=[f"raster image {width}x{height} queued for OCR review; no text was transcribed"],
    )


def parse_xlsx_candidates(
    raw: bytes, document_id: str
) -> tuple[DocumentParseSummary, list[ExtractionCandidate]]:
    """Create review candidates from labeled numeric cells, without field mapping."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return (
            DocumentParseSummary(
                parser="openpyxl",
                status="dependency_unavailable",
                issues=["install the optional spreadsheet dependency group to parse XLSX files"],
            ),
            [],
        )
    try:
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=False)
    except Exception as exc:
        return (
            DocumentParseSummary(
                parser="openpyxl",
                status="parser_failed",
                issues=[f"{type(exc).__name__}: {exc}"],
            ),
            [],
        )
    candidates: list[ExtractionCandidate] = []
    try:
        for worksheet in workbook.worksheets:
            headers: dict[int, str] = {}
            for row in worksheet.iter_rows():
                values = [cell.value for cell in row]
                for column, value in enumerate(values, start=1):
                    if isinstance(value, str) and value.strip() and not _is_numeric(value):
                        headers[column] = value.strip()
                label = next(
                    (
                        value.strip()
                        for value in values
                        if isinstance(value, str) and value.strip() and not _is_numeric(value)
                    ),
                    None,
                )
                if label is None:
                    continue
                for cell in row:
                    if cell.column == 1 or not _is_numeric(cell.value):
                        continue
                    header = headers.get(cell.column)
                    candidate_label = f"{label} | {header}" if header and header != label else label
                    locator = f"sheet:{worksheet.title}/cell:{cell.coordinate}"
                    candidates.append(
                        _candidate(
                            document_id=document_id,
                            label=candidate_label,
                            raw_value=str(cell.value),
                            locator=locator,
                            excerpt=f"{label} | {header or 'unknown period'} | {cell.value}",
                            parser="openpyxl",
                            period_hint=header,
                            block_type="spreadsheet_cell",
                        )
                    )
    finally:
        workbook.close()
    return (
        DocumentParseSummary(
            parser="openpyxl",
            status="parsed_candidates",
            candidate_count=len(candidates),
            issues=["XLSX values are review candidates; map field, unit, period and scope before creating facts."],
        ),
        candidates,
    )


def parse_xbrl_candidates(
    raw: bytes, document_id: str
) -> tuple[DocumentParseSummary, list[ExtractionCandidate]]:
    """Collect numeric XML/XBRL leaf values as review candidates without taxonomy mapping."""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        return (
            DocumentParseSummary(
                parser="xml.etree.ElementTree",
                status="parser_failed",
                issues=[f"ParseError: {exc}"],
            ),
            [],
        )
    candidates: list[ExtractionCandidate] = []
    for index, element in enumerate(root.iter()):
        if list(element) or not _is_numeric(element.text):
            continue
        label = element.tag.rsplit("}", 1)[-1]
        context = element.attrib.get("contextRef") or element.attrib.get("context")
        locator = f"xml:{label}[{index}]"
        candidates.append(
            _candidate(
                document_id=document_id,
                label=label,
                raw_value=(element.text or "").strip(),
                locator=locator,
                excerpt=ElementTree.tostring(element, encoding="unicode"),
                parser="xml.etree.ElementTree",
                scope_hint=context,
                block_type="xbrl_fact_candidate",
            )
        )
    return (
        DocumentParseSummary(
            parser="xml.etree.ElementTree",
            status="parsed_candidates",
            candidate_count=len(candidates),
            issues=["XML/XBRL values are review candidates; taxonomy, unit, context and reporting period require validation."],
        ),
        candidates,
    )


@dataclass(frozen=True)
class _XbrlContext:
    identifier: str | None
    period_hint: str
    period_type: Literal["duration", "instant"]


def parse_mapped_xbrl_candidates(
    raw: bytes, document_id: str, taxonomy_map: XbrlTaxonomyMap
) -> tuple[DocumentParseSummary, list[ExtractionCandidate]]:
    """Map exact XBRL concepts to review candidates after context and unit checks."""

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        return (
            DocumentParseSummary(
                parser="xml.etree.ElementTree",
                status="parser_failed",
                issues=[f"ParseError: {exc}"],
            ),
            [],
        )
    mapping = {
        (entry.namespace_uri, entry.local_name): entry for entry in taxonomy_map.entries
    }
    contexts = _xbrl_contexts(root)
    units = _xbrl_units(root)
    candidates: list[ExtractionCandidate] = []
    rejected = 0
    mapped_concepts = 0
    for index, element in enumerate(root.iter()):
        if list(element) or not _is_numeric(element.text):
            continue
        namespace_uri, local_name = _expanded_tag(element.tag)
        entry = mapping.get((namespace_uri, local_name))
        if entry is None:
            continue
        mapped_concepts += 1
        context_id = element.attrib.get("contextRef") or element.attrib.get("context")
        context = contexts.get(context_id or "")
        unit_id = element.attrib.get("unitRef")
        unit = units.get(unit_id or "")
        if context is None or (entry.unit is not None and entry.unit != unit):
            rejected += 1
            continue
        if entry.period_type is not None and entry.period_type != context.period_type:
            rejected += 1
            continue
        locator = (
            f"xbrl-map:{taxonomy_map.mapping_id}:{taxonomy_map.taxonomy_version}/"
            f"concept:{local_name}[{index}]/context:{context_id}/unit:{unit_id or 'none'}"
        )
        excerpt = (
            f"context={context_id}; entity={context.identifier or 'unknown'}; "
            f"period={context.period_hint}; unit={unit or 'missing'}; "
            + ElementTree.tostring(element, encoding="unicode")
        )
        candidates.append(
            _candidate(
                document_id=document_id,
                label=f"{entry.field} [{local_name}]",
                raw_value=(element.text or "").strip(),
                locator=locator,
                excerpt=excerpt,
                parser=(
                    f"xbrl-taxonomy:{taxonomy_map.mapping_id}:"
                    f"{taxonomy_map.taxonomy_version}"
                ),
                field_hint=entry.field,
                unit_hint=unit,
                period_hint=context.period_hint,
                period_kind_hint=entry.period_kind_hint,
                scope_hint=entry.scope,
                block_type="xbrl_taxonomy_mapped_candidate",
                confidence_reason=(
                    "The XBRL concept, context period and unit matched the versioned taxonomy map, "
                    "but the mapped value remains an unverified candidate until reviewed against the filing."
                ),
            )
        )
    issues = [
        "Taxonomy-mapped XBRL values remain review candidates; verify the filing, entity, "
        "period, scope and accounting interpretation before approval."
    ]
    if mapped_concepts == 0:
        issues.append("no numeric XBRL facts matched the supplied exact taxonomy map")
    if rejected:
        issues.append(f"{rejected} matched XBRL facts failed context, period-type or unit validation")
    return (
        DocumentParseSummary(
            parser=f"xbrl-taxonomy:{taxonomy_map.mapping_id}:{taxonomy_map.taxonomy_version}",
            status="parsed_candidates",
            candidate_count=len(candidates),
            issues=issues,
        ),
        candidates,
    )


def _xbrl_contexts(root: ElementTree.Element) -> dict[str, _XbrlContext]:
    contexts: dict[str, _XbrlContext] = {}
    for element in root.iter():
        if _expanded_tag(element.tag)[1] != "context" or not element.attrib.get("id"):
            continue
        instant = _descendant_text(element, "instant")
        start = _descendant_text(element, "startDate")
        end = _descendant_text(element, "endDate")
        if instant:
            context = _XbrlContext(
                identifier=_descendant_text(element, "identifier"),
                period_hint=instant,
                period_type="instant",
            )
        elif start and end:
            context = _XbrlContext(
                identifier=_descendant_text(element, "identifier"),
                period_hint=f"{start}/{end}",
                period_type="duration",
            )
        else:
            continue
        contexts[element.attrib["id"]] = context
    return contexts


def _xbrl_units(root: ElementTree.Element) -> dict[str, str]:
    units: dict[str, str] = {}
    for element in root.iter():
        if _expanded_tag(element.tag)[1] != "unit" or not element.attrib.get("id"):
            continue
        measures = [
            (item.text or "").strip()
            for item in element.iter()
            if _expanded_tag(item.tag)[1] == "measure" and (item.text or "").strip()
        ]
        if measures:
            units[element.attrib["id"]] = " * ".join(measures)
    return units


def _descendant_text(element: ElementTree.Element, local_name: str) -> str | None:
    value = next(
        (
            (item.text or "").strip()
            for item in element.iter()
            if _expanded_tag(item.tag)[1] == local_name and (item.text or "").strip()
        ),
        None,
    )
    return value


def _expanded_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        namespace_uri, local_name = tag[1:].split("}", 1)
        return namespace_uri, local_name
    return "", tag


def _candidate(
    *,
    document_id: str,
    label: str,
    raw_value: str,
    locator: str,
    excerpt: str,
    parser: str,
    field_hint: str | None = None,
    unit_hint: str | None = None,
    period_hint: str | None = None,
    period_kind_hint: str | None = None,
    scope_hint: str | None = None,
    block_type: str,
    confidence_reason: str = "Parser output has not been mapped to the financial taxonomy or reviewed against the original filing.",
) -> ExtractionCandidate:
    identifier = hashlib.sha256(f"{document_id}:{locator}".encode("utf-8")).hexdigest()
    return ExtractionCandidate(
        id=identifier,
        document_id=document_id,
        label=label,
        raw_value=raw_value,
        field_hint=field_hint,
        unit_hint=unit_hint,
        period_hint=period_hint,
        period_kind_hint=period_kind_hint,
        scope_hint=scope_hint,
        evidence=EvidenceSpan(
            document_id=document_id,
            locator=locator,
            excerpt=excerpt,
            block_type=block_type,
            verification_status="unverified_candidate",
        ),
        parser=parser,
        confidence_reason=confidence_reason,
    )


def _is_numeric(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return False
    return True
