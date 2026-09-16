import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch import schemas
from finresearch.feedback_review import VERIFICATION_SCHEMA, review_run_feedback
from finresearch.runner import run_case, resume_audit
from finresearch.storage import Store
from test.test_system import FakeProvider
from test.test_feedback_review import VerificationProvider


class MaterialIdsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.units = [
            {"document_id": "doc-A", "unit_id": "p1", "text": "收入100万元"},
            {"document_id": "doc-A", "unit_id": "p2", "text": "成本60万元"},
            {"document_id": "doc-B", "unit_id": "p3", "text": "净利润20万元"},
        ]
        self.coverage = [{"document_id": u["document_id"], "unit_id": u["unit_id"], "status": "read", "note": "阅读全文"} for u in self.units]
        self.evidence = {"document_id": "doc-B", "unit_id": "p3", "quote": "净利润20万元"}
        self.execution = {"coverage": copy.deepcopy(self.coverage), "result": {
            "status": "completed", "summary": "正常自由文本，不能被ID枚举约束", "records": [{
                "record_type": "observed", "entity": "公司B", "period": "2025", "field": "净利润",
                "raw_value": "20", "value": "20", "unit": "万元", "currency": "CNY", "evidence": [copy.deepcopy(self.evidence)]}],
            "analysis": [{"heading": "普通标题", "text": "原文事实", "evidence": [copy.deepcopy(self.evidence)]}],
            "missing_data": [], "warnings": []}}
        self.audit = {"coverage": copy.deepcopy(self.coverage), "audit": {"verdict": "revise", "summary": "核查说明", "issues": [{
            "severity": "minor", "category": "字段", "description": "程序示例", "correction": "核对字段", "evidence": [copy.deepcopy(self.evidence)]}],
            "dimensions": [{"name": "证据", "judgment": "fail", "reason": "示例依据"}]}}
        self.verification = {"coverage": copy.deepcopy(self.coverage), "decisions": [{"issue_index": 0, "verdict": "confirmed",
            "reason": "程序核实示例", "result_pointers": ["/records/0/value"], "evidence": [copy.deepcopy(self.evidence)]}]}

    def test_templates_and_shared_text_are_unchanged(self):
        templates = [schemas.EXECUTION, schemas.AUDIT, VERIFICATION_SCHEMA]
        before = copy.deepcopy(templates)
        for template in templates:
            bound = schemas.bind_material_ids(template, self.units)
            self.assertIsNot(bound, template)
            def inspect(node):
                if isinstance(node, dict):
                    for name, field in node.get("properties", {}).items():
                        if name == "document_id":
                            self.assertEqual(field, {"$ref": "#/$defs/material_document_id"})
                        elif name == "unit_id":
                            self.assertEqual(field, {"$ref": "#/$defs/material_unit_id"})
                        elif name in {"summary", "quote", "note", "text", "reason", "entity", "value", "field", "description"}:
                            self.assertNotIn("enum", field)
                    for value in node.values():
                        inspect(value)
                elif isinstance(node, list):
                    for value in node:
                        inspect(value)
            inspect(bound)
            self.assertEqual(bound["$defs"]["material_document_id"]["enum"], ["doc-A", "doc-B"])
            self.assertEqual(bound["$defs"]["material_unit_id"]["enum"], ["p1", "p2", "p3"])
        self.assertEqual(templates, before)
        self.assertEqual(schemas.TEXT, {"type": "string"})

    def test_two_documents_and_multiple_pages_validate_all_three_interfaces(self):
        for template, payload in [(schemas.EXECUTION, self.execution), (schemas.AUDIT, self.audit), (VERIFICATION_SCHEMA, self.verification)]:
            with self.subTest(interface=list(template["properties"])):
                bound = schemas.bind_material_ids(template, self.units)
                schemas.validate(payload, bound)
                schemas.validate_coverage(payload["coverage"], self.units)
                self.assertEqual(schemas.validate_evidence(payload, self.units), [])

    def test_invented_and_swapped_ids_fail_structure_validation(self):
        for template, original in [(schemas.EXECUTION, self.execution), (schemas.AUDIT, self.audit), (VERIFICATION_SCHEMA, self.verification)]:
            bound = schemas.bind_material_ids(template, self.units)
            for key, invalid in [("document_id", "invented-doc"), ("unit_id", "invented-page"),
                                 ("document_id", "p1"), ("unit_id", "doc-A")]:
                with self.subTest(interface=list(template["properties"]), field=key, invalid=invalid):
                    payload = copy.deepcopy(original)
                    payload["coverage"][0][key] = invalid
                    with self.assertRaisesRegex(ValueError, "结构不合规"):
                        schemas.validate(payload, bound)
                    payload = copy.deepcopy(original)
                    if "result" in payload:
                        evidence = payload["result"]["records"][0]["evidence"][0]
                    elif "audit" in payload:
                        evidence = payload["audit"]["issues"][0]["evidence"][0]
                    else:
                        evidence = payload["decisions"][0]["evidence"][0]
                    evidence[key] = invalid
                    with self.assertRaisesRegex(ValueError, "结构不合规"):
                        schemas.validate(payload, bound)

    def test_wrong_document_page_pair_still_requires_evidence_validation(self):
        payload = copy.deepcopy(self.execution)
        payload["result"]["records"][0]["evidence"][0].update(document_id="doc-B", unit_id="p2", quote="成本60万元")
        bound = schemas.bind_material_ids(schemas.EXECUTION, self.units)
        schemas.validate(payload, bound)
        with self.assertRaisesRegex(ValueError, "证据定位无效"):
            schemas.validate_evidence(payload, self.units)

    def test_allowed_ids_do_not_replace_full_coverage_or_quote_checks(self):
        bound = schemas.bind_material_ids(schemas.EXECUTION, self.units)
        payload = copy.deepcopy(self.execution)
        payload["coverage"].pop()
        schemas.validate(payload, bound)
        with self.assertRaisesRegex(ValueError, "覆盖"):
            schemas.validate_coverage(payload["coverage"], self.units)
        payload = copy.deepcopy(self.execution)
        payload["result"]["records"][0]["evidence"][0]["quote"] = "不存在的披露"
        schemas.validate(payload, bound)
        with self.assertRaisesRegex(ValueError, "不匹配"):
            schemas.validate_evidence(payload, self.units)

    def test_each_binding_is_independent_and_invalid_units_fail(self):
        first = schemas.bind_material_ids(schemas.EXECUTION, self.units)
        second = schemas.bind_material_ids(schemas.EXECUTION, [{"document_id": "different", "unit_id": "only"}])
        first_field = first["$defs"]["material_unit_id"]
        second_field = second["$defs"]["material_unit_id"]
        second_field["enum"].append("other")
        self.assertEqual(first_field["enum"], ["p1", "p2", "p3"])
        for units in ([], [{"document_id": "A", "unit_id": ""}], [{"document_id": "A"}]):
            with self.subTest(units=units), self.assertRaises(ValueError):
                schemas.bind_material_ids(schemas.EXECUTION, units)

    def test_four_hundred_pages_share_enums_without_repeated_schema_growth(self):
        units = [{"document_id": "document", "unit_id": "p%05d" % i} for i in range(1, 401)]
        def enum_values(value):
            if isinstance(value, dict):
                return len(value.get("enum", [])) + sum(enum_values(child) for child in value.values())
            if isinstance(value, list):
                return sum(enum_values(child) for child in value)
            return 0
        for template in (schemas.EXECUTION, schemas.AUDIT, VERIFICATION_SCHEMA):
            with self.subTest(interface=list(template["properties"])):
                bound = schemas.bind_material_ids(template, units)
                self.assertEqual(len(bound["$defs"]["material_unit_id"]["enum"]), 400)
                self.assertEqual(enum_values(bound), enum_values(template) + 401)
                self.assertLess(enum_values(bound), 1000)
                self.assertNotIn("enum", bound["properties"]["coverage"]["items"]["properties"]["unit_id"])

    def test_existing_definitions_are_preserved_and_reserved_names_fail_explicitly(self):
        template = copy.deepcopy(schemas.EXECUTION)
        template["$defs"] = {"other": {"type": "integer"}}
        bound = schemas.bind_material_ids(template, self.units)
        self.assertEqual(bound["$defs"]["other"], {"type": "integer"})
        self.assertEqual(template["$defs"], {"other": {"type": "integer"}})
        with self.assertRaisesRegex(ValueError, "未绑定的模板"):
            schemas.bind_material_ids(bound, self.units)


class MaterialIdsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.documents = []
        for name in ("first", "second"):
            source = self.root / (name + ".txt")
            source.write_text(name + "：演练公司2025年营业收入100万元。", encoding="utf-8")
            self.documents.append(self.store.import_file(source, published_at="2026-01-01T00:00:00+08:00"))
        self.case = self.store.create_case([d["id"] for d in self.documents], "提取", "2026-01-02T00:00:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, provider):
        calls = []
        original = provider.call
        def call(role, instructions, prompt, schema, images=None, trace_dir=None):
            calls.append((role, schema))
            return original(role, instructions, prompt, schema, images, trace_dir)
        provider.call = call
        return calls

    def test_normal_execution_and_resumed_audit_use_bound_schema_for_request_and_validation(self):
        provider = FakeProvider()
        sent = self.capture(provider)
        validate = schemas.validate
        checked = []
        def checked_validate(value, schema):
            checked.append(schema)
            return validate(value, schema)
        with patch("finresearch.schemas.validate", side_effect=checked_validate):
            run = run_case(self.store, self.case["id"], provider=provider)
            resumed = resume_audit(self.store, run["id"], provider=provider)
        self.assertEqual(run["status"], "audited", run.get("error"))
        self.assertEqual(resumed["status"], "audited", resumed.get("error"))
        self.assertEqual([role for role, _ in sent], ["business", "audit", "audit"])
        for role, bound in sent:
            field = bound["properties"]["coverage"]["items"]["properties"]["document_id"]
            self.assertEqual(field, {"$ref": "#/$defs/material_document_id"})
            self.assertEqual(set(bound["$defs"]["material_document_id"]["enum"]), {d["id"] for d in self.documents})
            self.assertTrue(any(schema is bound for schema in checked))
            self.assertIsNot(bound, schemas.EXECUTION if role == "business" else schemas.AUDIT)
        self.assertEqual(sent[1][1], sent[2][1])

    def test_verification_uses_bound_schema_for_request_and_validation(self):
        def revise(role, output):
            if role == "business":
                output["result"]["records"][0]["value"] = "200"
            else:
                unit = output["coverage"][0]
                output["audit"].update(verdict="revise", issues=[{"severity": "major", "category": "数值", "description": "100被写成200",
                    "evidence": [{"document_id": unit["document_id"], "unit_id": unit["unit_id"], "quote": "营业收入100万元"}], "correction": "保留100"}])
                output["audit"]["dimensions"][0]["judgment"] = "fail"
        run = run_case(self.store, self.case["id"], provider=FakeProvider(revise))
        self.assertEqual(run["status"], "audited", run.get("error"))
        provider = VerificationProvider()
        sent = self.capture(provider)
        validate = schemas.validate
        checked = []
        def checked_validate(value, schema):
            checked.append(schema)
            return validate(value, schema)
        with patch("finresearch.schemas.validate", side_effect=checked_validate):
            record = review_run_feedback(self.store, run["id"], provider=provider)
        self.assertEqual(record["status"], "completed", record.get("error"))
        self.assertEqual(len(sent), 1)
        bound = sent[0][1]
        self.assertIsNot(bound, VERIFICATION_SCHEMA)
        self.assertTrue(any(schema is bound for schema in checked))
        evidence = bound["properties"]["decisions"]["items"]["properties"]["evidence"]["items"]["properties"]
        self.assertEqual(evidence["document_id"], {"$ref": "#/$defs/material_document_id"})
        self.assertEqual(set(bound["$defs"]["material_document_id"]["enum"]), {d["id"] for d in self.documents})
        self.assertNotIn("enum", evidence["quote"])


if __name__ == "__main__":
    unittest.main()
