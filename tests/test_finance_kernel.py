from datetime import date
from decimal import Decimal

from finagent.contracts import EvidenceSpan, Fact
from finagent.evaluation import _within_tolerance
from finagent.finance_kernel import growth, ratio


def fact(
    identifier: str,
    value: str,
    *,
    field: str = "revenue",
    period_end: date = date(2025, 12, 31),
    scope: str = "consolidated",
) -> Fact:
    return Fact(
        id=identifier,
        document_id="document",
        entity_id="company",
        field=field,
        value=Decimal(value),
        unit="CNY",
        period_end=period_end,
        period_kind="FY",
        scope=scope,
        published_at=date(2026, 3, 31),
        evidence=EvidenceSpan(document_id="document", locator="/0", excerpt=value),
    )


def test_negative_prior_revenue_is_not_presented_as_growth() -> None:
    metric = growth(fact("current", "20"), fact("prior", "-10", period_end=date(2024, 12, 31)))
    assert metric.status == "inapplicable"
    assert metric.value is None
    assert "negative" in metric.reason


def test_ratio_rejects_mixed_reporting_scope() -> None:
    metric = ratio(
        "net_margin", fact("profit", "10", field="profit", scope="parent"),
        fact("revenue", "100"),
    )
    assert metric.status == "inapplicable"
    assert metric.value is None


def test_reference_comparison_keeps_non_numeric_fields_exact() -> None:
    assert _within_tolerance({"currency": "CNY", "amount": 12500}, {"currency": "CNY", "amount": 12500}, Decimal("0"))
    assert not _within_tolerance({"currency": "USD"}, {"currency": "CNY"}, Decimal("0"))
