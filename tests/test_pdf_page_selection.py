from __future__ import annotations

from finagent.document_parser import _pdf_financial_page_numbers


def test_financial_page_selection_keeps_adjacent_pages(monkeypatch) -> None:
    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class Reader:
        pages = [Page("cover"), Page("主要会计数据"), Page("continued"), Page("notes")]

    monkeypatch.setattr("pypdf.PdfReader", lambda _: Reader())
    assert _pdf_financial_page_numbers(b"%PDF-test", 4) == [1, 2, 3]
