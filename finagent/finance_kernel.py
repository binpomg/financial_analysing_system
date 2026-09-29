"""Deterministic financial calculations with explicit applicability rules."""

from __future__ import annotations

from decimal import Decimal
import calendar

from finagent.contracts import Fact, Metric


def growth(current: Fact, prior: Fact) -> Metric:
    inputs = [current.id, prior.id]
    prior_year = current.period_end.year - 1
    comparable_date = current.period_end.replace(
        year=prior_year, day=min(current.period_end.day, calendar.monthrange(prior_year, current.period_end.month)[1]),
    )
    if (current.entity_id != prior.entity_id or current.field != prior.field or
            prior.period_end != comparable_date):
        return Metric(name=f"{current.field}_yoy", value=None, unit="percent",
                      formula="(current / prior - 1) * 100", input_fact_ids=inputs,
                      status="inapplicable", reason="year-on-year requires the same entity/field and prior-year period")
    if (current.unit, current.scope, current.period_kind) != (
        prior.unit, prior.scope, prior.period_kind
    ):
        return Metric(
            name=f"{current.field}_yoy",
            value=None,
            unit="percent",
            formula="(current / prior - 1) * 100",
            input_fact_ids=inputs,
            status="inapplicable",
            reason="unit, scope or period kind differs",
        )
    if prior.value <= 0:
        return Metric(
            name=f"{current.field}_yoy",
            value=None,
            unit="percent",
            formula="(current / prior - 1) * 100",
            input_fact_ids=inputs,
            status="inapplicable",
            reason="prior value is zero or negative; report absolute change instead",
        )
    return Metric(
        name=f"{current.field}_yoy",
        value=(current.value / prior.value - Decimal(1)) * Decimal(100),
        unit="percent",
        formula="(current / prior - 1) * 100",
        input_fact_ids=inputs,
        status="calculated",
    )


def ratio(name: str, numerator: Fact, denominator: Fact) -> Metric:
    inputs = [numerator.id, denominator.id]
    if (numerator.entity_id, numerator.unit, numerator.scope, numerator.period_end, numerator.period_kind) != (
        denominator.entity_id,
        denominator.unit,
        denominator.scope,
        denominator.period_end,
        denominator.period_kind,
    ):
        return Metric(
            name=name,
            value=None,
            unit="percent",
            formula=f"{numerator.field} / {denominator.field} * 100",
            input_fact_ids=inputs,
            status="inapplicable",
            reason="unit, scope or period differs",
        )
    if denominator.value == 0:
        return Metric(
            name=name,
            value=None,
            unit="percent",
            formula=f"{numerator.field} / {denominator.field} * 100",
            input_fact_ids=inputs,
            status="inapplicable",
            reason="denominator is zero",
        )
    return Metric(
        name=name,
        value=numerator.value / denominator.value * Decimal(100),
        unit="percent",
        formula=f"{numerator.field} / {denominator.field} * 100",
        input_fact_ids=inputs,
        status="calculated",
    )


def quotient(
    name: str,
    numerator: Decimal,
    denominator: Decimal,
    *,
    unit: str,
    formula: str,
    input_fact_ids: list[str],
    input_document_ids: list[str],
    confidence: str,
    confidence_reason: str,
) -> Metric:
    """Calculate a typed quotient when the business rule defines the denominator."""
    if denominator == 0:
        return Metric(
            name=name,
            value=None,
            unit=unit,
            formula=formula,
            input_fact_ids=input_fact_ids,
            input_document_ids=input_document_ids,
            status="inapplicable",
            reason="denominator is zero",
            confidence=confidence,
            confidence_reason=confidence_reason,
        )
    return Metric(
        name=name,
        value=numerator / denominator,
        unit=unit,
        formula=formula,
        input_fact_ids=input_fact_ids,
        input_document_ids=input_document_ids,
        status="calculated",
        confidence=confidence,
        confidence_reason=confidence_reason,
    )


def fcff(
    ebit: Decimal,
    tax_rate: Decimal,
    depreciation_amortization: Decimal,
    capex: Decimal,
    delta_nwc: Decimal,
) -> Decimal:
    """FCFF = EBIT * (1 - tax rate) + D&A - capex - change in NWC."""
    return ebit * (Decimal(1) - tax_rate) + depreciation_amortization - capex - delta_nwc


def dcf_value(
    forecast_fcff: list[Decimal],
    wacc: Decimal,
    terminal_growth: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return enterprise value and terminal value using end-of-year discounting."""
    if not forecast_fcff:
        raise ValueError("forecast cash flow is required")
    if not all(value.is_finite() for value in [*forecast_fcff, wacc, terminal_growth]):
        raise ValueError("DCF inputs must be finite")
    if wacc <= 0 or terminal_growth <= -1:
        raise ValueError("WACC must be positive and growth must exceed -100 percent")
    if wacc <= terminal_growth:
        raise ValueError("WACC must exceed terminal growth for a perpetual-growth DCF")
    terminal_value = forecast_fcff[-1] * (Decimal(1) + terminal_growth) / (wacc - terminal_growth)
    enterprise_value = Decimal(0)
    for index, cash_flow in enumerate(forecast_fcff, start=1):
        enterprise_value += cash_flow / ((Decimal(1) + wacc) ** index)
    enterprise_value += terminal_value / ((Decimal(1) + wacc) ** len(forecast_fcff))
    return enterprise_value, terminal_value
