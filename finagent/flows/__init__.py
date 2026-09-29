"""Seven independently addressable business flows."""

from finagent.contracts import BusinessLine
from finagent.flows.base import Flow
from finagent.flows.line_01 import DocumentFlow
from finagent.flows.line_02 import FinancialFlow
from finagent.flows.line_03 import IndustryFlow
from finagent.flows.line_04 import ValuationFlow
from finagent.flows.line_05 import ReportReviewFlow
from finagent.flows.line_06 import MemoFlow
from finagent.flows.line_07 import QualityFlow

REGISTRY: dict[BusinessLine, Flow] = {
    BusinessLine.DOCUMENTS: DocumentFlow(),
    BusinessLine.FINANCIAL_REPORT: FinancialFlow(),
    BusinessLine.INDUSTRY_IMPACT: IndustryFlow(),
    BusinessLine.VALUATION: ValuationFlow(),
    BusinessLine.REPORT_REVIEW: ReportReviewFlow(),
    BusinessLine.INVESTMENT_MEMO: MemoFlow(),
    BusinessLine.REPORT_QUALITY: QualityFlow(),
}
