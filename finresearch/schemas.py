"""执行者与审核者交接的严格数据结构；语义正确性另行审核。"""


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
EVIDENCE = obj({"document_id": TEXT, "unit_id": TEXT, "quote": TEXT})
COVERAGE = arr(obj({"document_id": TEXT, "unit_id": TEXT, "status": {"type": "string", "enum": ["read", "unreadable"]}, "note": TEXT}))
RESULT = obj({
    "status": {"type": "string", "enum": ["completed", "needs_data", "not_applicable", "reading_incomplete"]},
    "summary": TEXT,
    "records": arr(obj({"record_type": TEXT, "entity": TEXT, "period": TEXT, "field": TEXT, "raw_value": TEXT, "value": TEXT, "unit": TEXT, "currency": TEXT, "evidence": arr(EVIDENCE)})),
    "analysis": arr(obj({"heading": TEXT, "text": TEXT, "evidence": arr(EVIDENCE)})),
    "missing_data": arr(TEXT), "warnings": arr(TEXT)
})
EXECUTION = obj({"coverage": COVERAGE, "result": RESULT})
AUDIT_BODY = obj({
    "verdict": {"type": "string", "enum": ["pass", "revise", "insufficient_evidence"]},
    "issues": arr(obj({"severity": {"type": "string", "enum": ["critical", "major", "minor"]}, "category": TEXT, "description": TEXT, "evidence": arr(EVIDENCE), "correction": TEXT})),
    "dimensions": arr(obj({"name": TEXT, "judgment": {"type": "string", "enum": ["pass", "fail", "uncertain"]}, "reason": TEXT})),
    "summary": TEXT
})
AUDIT = obj({"coverage": COVERAGE, "audit": AUDIT_BODY})
DISCOVERY = obj({"items": arr(obj({"url": TEXT, "title": TEXT, "document_type": TEXT, "metadata_note": TEXT}))})
CANDIDATE = obj({"hypothesis": TEXT, "expected_benefit": TEXT, "risks": arr(TEXT), "skill_markdown": TEXT})


def bind_material_ids(schema, units):
    """将原件ID绑定到独立Schema副本；跨文档配对仍由覆盖/证据校验完成。"""
    import copy
    if not units:
        raise ValueError("没有原件单元，不能绑定输出Schema")
    for unit in units:
        if any(not isinstance(unit.get(key), str) or not unit[key].strip()
               for key in ("document_id", "unit_id")):
            raise ValueError("原件单元必须包含有效document_id及unit_id")
    allowed = {key: sorted({unit[key] for unit in units}) for key in ("document_id", "unit_id")}
    bound = copy.deepcopy(schema)
    names = {"document_id": "material_document_id", "unit_id": "material_unit_id"}
    definitions = bound.setdefault("$defs", {})
    if any(name in definitions for name in names.values()):
        raise ValueError("Schema已经包含原件ID定义，请从未绑定的模板建立新副本")
    for key, name in names.items():
        definitions[name] = {"type": "string", "enum": list(allowed[key])}
    def walk(value):
        if isinstance(value, dict):
            properties = value.get("properties", {})
            for key in allowed:
                if key in properties:
                    # TEXT在summary/quote/ID等字段间共享；deepcopy保留别名，必须替换而非原位附加enum。
                    properties[key] = {"$ref": "#/$defs/" + names[key]}
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(bound)
    return bound


def validate(value, schema):
    from jsonschema import Draft202012Validator
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: str(list(error.path)))
    if errors:
        raise ValueError("模型输出结构不合规：" + "; ".join(str(list(e.path)) + " " + e.message for e in errors[:8]))


def validate_coverage(coverage, units):
    expected = {(u["document_id"], u["unit_id"]) for u in units}
    actual = [(u["document_id"], u["unit_id"]) for u in coverage]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("阅读记录未逐一覆盖材料包，或混入未知/重复页面")
    if any(item["status"] != "read" for item in coverage):
        raise ValueError("存在不可辨认内容，不能认定完整阅读")


def validate_evidence(payload, units):
    lookup = {(u["document_id"], u["unit_id"]): u for u in units}
    notes = []
    def walk(value):
        if isinstance(value, dict):
            if set(value) == {"document_id", "unit_id", "quote"}:
                unit = lookup.get((value["document_id"], value["unit_id"]))
                if unit is None or not value["quote"].strip():
                    raise ValueError("证据定位无效或引文为空")
                # 扫描图的引文可能不存在于解析文本中，必须由视觉审核确认。
                compact = lambda text: "".join(text.split())
                if compact(value["quote"]) not in compact(unit.get("text", "")):
                    if not unit.get("image_path"):
                        raise ValueError("文本证据与对应原文不匹配：" + value["unit_id"])
                    notes.append("需视觉核验引文：" + value["document_id"] + "/" + value["unit_id"])
            else:
                for item in value.values():
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(payload)
    return list(dict.fromkeys(notes))
