"""可配置中转接口：不自动换模型、降推理强度或复用角色对话。"""
import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .schemas import validate, obj, TEXT
from .storage import now, write_json


class ProviderError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("API 返回重定向，拒绝转发密钥；请配置最终 API 地址")


def read_event_stream(response, protocol, max_bytes, deadline, event_callback=None):
    """读取标准 SSE；以明确 completed/stop 为成功，断流不可补成成功。"""
    consumed, buffer, last_response = 0, [], None
    text_parts, chat_model, chat_usage, finish, tool_calls = [], None, {}, None, {}
    for raw_line in response:
        consumed += len(raw_line)
        if consumed > max_bytes:
            raise ProviderError("流式响应超出配置字节上限，保留失败状态")
        if time.monotonic() > deadline:
            raise ProviderError("流式响应超出配置的单轮总时限，保留失败状态")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        if line == "" and buffer:
            data = "\n".join(buffer)
            buffer = []
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event_callback:
                event_callback(event)
            if protocol == "responses":
                kind = event.get("type", "")
                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    last_response = event.get("response", {})
                    break
                if kind == "error":
                    raise ProviderError("模型流式响应报告错误；未返回完整结果")
            else:
                chat_model = event.get("model", chat_model)
                chat_usage = event.get("usage") or chat_usage
                for choice in event.get("choices", []):
                    text_parts.append(choice.get("delta", {}).get("content") or "")
                    for call in choice.get("delta", {}).get("tool_calls", []):
                        entry = tool_calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        entry["id"] += call.get("id", "")
                        for key in ("name", "arguments"):
                            entry["function"][key] += call.get("function", {}).get(key, "")
                    finish = choice.get("finish_reason") or finish
    if protocol == "responses":
        if last_response is None:
            raise ProviderError("API 流在 completed 事件前中断，不能将局部输出当成完成")
        return last_response
    return {"model": chat_model, "usage": chat_usage, "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": "".join(text_parts), "tool_calls": list(tool_calls.values())}}]}


class APIProvider:
    def __init__(self, config):
        self.config = config

    def ready(self):
        base, key, _ = self.credentials()
        return bool(base and key)

    def credentials(self):
        settings = self.config["api"]
        base = os.environ.get(settings["base_url_env"], "")
        key = os.environ.get(settings["api_key_env"], "")
        protocol = settings["protocol"]
        if base and key:
            return base, key, protocol
        if settings.get("credential_source") == "codex_config":
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            path = Path(settings.get("codex_config_path", "~/.codex/config.toml")).expanduser()
            if path.is_file():
                parsed = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                provider = parsed.get("model_providers", {}).get(parsed.get("model_provider"), {})
                base = provider.get("base_url", "")
                key = provider.get("experimental_bearer_token", "") or os.environ.get(provider.get("env_key", ""), "")
                protocol = "chat_completions" if provider.get("wire_api") == "chat" else provider.get("wire_api", protocol)
        return base, key, protocol

    def call(self, role, instructions, prompt, schema, images=None, trace_dir=None):
        settings = self.config["api"]
        if not self.ready():
            raise ProviderError("尚未配置 " + settings["base_url_env"] + " 和 " + settings["api_key_env"] + "，未发起付费请求")
        base, api_key, protocol = self.credentials()
        base = base.rstrip("/")
        def safe_log(path, value):
            def clean(item):
                if isinstance(item, str):
                    return item.replace(api_key, "[REDACTED]")
                if isinstance(item, dict):
                    return {clean(key): clean(val) for key, val in item.items()}
                if isinstance(item, list):
                    return [clean(val) for val in item]
                return item
            write_json(path, clean(value))
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("API 基础地址必须是无密钥、无查询参数的 HTTPS 地址（通常以 /v1 结尾）")
        model = self.config["models"][role]
        tool = {"type": "function", "name": "calculate", "description": "精确计算金融算式。支持数字、小数、括号、+ - * / ** 及sum(a,b,...)、min、max、abs；不联网，不执行代码。金额单位需自行先统一，结果为Decimal字符串。财务比例、估值和审核重算须使用此工具。", "parameters": obj({"expression": TEXT}), "strict": True}
        if role in {"business", "audit"}:
            instructions += "\n本次提供 calculate 工具。涉及金额换算、比例、增长率、财务桥接和估值的计算必须实际调用工具，审核需独立重算。工具返回的是算术结果，金融口径仍需核验。"
        image_data = []
        for item in images or []:
            path = Path(item["path"])
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            image_data.append((item["label"], "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")))
        if protocol == "responses":
            content = [{"type": "input_text", "text": prompt}]
            for label, data in image_data:
                content.extend([{"type": "input_text", "text": label}, {"type": "input_image", "image_url": data, "detail": "high"}])
            payload = {"model": model["model"], "reasoning": {"effort": model["reasoning_effort"]},
                       "instructions": instructions, "input": [{"role": "user", "content": content}],
                       "text": {"format": {"type": "json_schema", "name": "financial_result", "schema": schema, "strict": True}},
                       "max_output_tokens": settings["max_output_tokens"], "store": False, "stream": True}
            endpoint = base + "/responses"
        elif protocol == "chat_completions":
            content = [{"type": "text", "text": prompt}]
            for label, data in image_data:
                content.extend([{"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": data, "detail": "high"}}])
            payload = {"model": model["model"], "reasoning_effort": model["reasoning_effort"],
                       "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": content}],
                       "response_format": {"type": "json_schema", "json_schema": {"name": "financial_result", "schema": schema, "strict": True}},
                       "max_completion_tokens": settings["max_output_tokens"], "stream": True, "stream_options": {"include_usage": True}}
            endpoint = base + "/chat/completions"
        else:
            raise ProviderError("不支持的 API 协议；只能显式选择 responses 或 chat_completions")
        if role in {"business", "audit"}:
            payload["tools"] = [tool if protocol == "responses" else {"type": "function", "function": {key: value for key, value in tool.items() if key != "type"}}]
        trace = {"started_at": now(), "role": role, "requested_model": model["model"],
                 "reasoning_effort": model["reasoning_effort"], "protocol": protocol,
                 "endpoint_host": parsed.hostname, "image_count": len(image_data)}
        if trace_dir:
            trace_dir = Path(trace_dir)
            # 仅保存本角色输入；不保存 Authorization 或密钥。
            safe_log(trace_dir / "input.json", {"instructions": instructions, "prompt": prompt,
                       "images": [{"label": i["label"], "path": str(i["path"])} for i in images or []], "schema": schema})
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            calculations, usage_steps = [], []
            for step in range(settings.get("max_tool_rounds", 12) + 1):
                request = urllib.request.Request(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                                 {"Authorization": "Bearer " + api_key, "Content-Type": "application/json", "User-Agent": "financial-research-system/0.1"})
                with opener.open(request, timeout=settings["timeout_seconds"]) as response:
                    if "text/event-stream" in response.headers.get("Content-Type", ""):
                        events = {"count": 0, "output_parts": []}
                        def progress(event):
                            events["count"] += 1
                            if event.get("type") == "response.output_text.delta":
                                events["output_parts"].append(event.get("delta", ""))
                            elif protocol == "chat_completions":
                                events["output_parts"].extend(choice.get("delta", {}).get("content") or "" for choice in event.get("choices", []))
                            if trace_dir and event.get("type") == "error":
                                safe_log(trace_dir / "stream-error.json", event)
                            if trace_dir and (events["count"] == 1 or events["count"] % 40 == 0):
                                safe_log(trace_dir / "progress.json", {"at": now(), "step": step, "event_count": events["count"], "event_type": event.get("type", "chat_delta")})
                        stream_returned = False
                        try:
                            result = read_event_stream(response, protocol, settings["max_response_bytes"], time.monotonic() + settings["timeout_seconds"], progress)
                            stream_returned = True
                        finally:
                            # 失败流的文本只供诊断，绝不送入审核或当成完整业务成果；不收集推理内容。
                            if trace_dir and not stream_returned and events["output_parts"]:
                                safe_log(trace_dir / ("partial-output-step-%02d.json" % step),
                                         {"status": "incomplete_diagnostic_only", "step": step, "text": "".join(events["output_parts"])})
                    else:
                        raw = response.read(settings["max_response_bytes"] + 1)
                        if len(raw) > settings["max_response_bytes"]:
                            raise ProviderError("API 响应超限，未截断成成功结果")
                        result = json.loads(raw.decode("utf-8"))
                if trace_dir:
                    safe_log(trace_dir / ("response-step-%02d.json" % step), result)
                reported = result.get("model")
                allowed = [model["model"]] + model.get("accepted_reported_models", [])
                if reported not in allowed:
                    raise ProviderError("服务返回模型名与配置不一致或缺失，禁止宣称指定模型验证成功")
                usage_steps.append(result.get("usage", {}))
                if protocol == "responses":
                    if result.get("status") != "completed":
                        code = str((result.get("error") or {}).get("code", ""))
                        safe_code = code if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code) and api_key not in code else "unspecified"
                        trace["api_error_code"] = safe_code
                        raise ProviderError("该轮 API 响应未完成（" + safe_code + "）；禁止执行局部工具调用或将其计作成功")
                    calls = [item for item in result.get("output", []) if item.get("type") == "function_call"]
                else:
                    choice = result.get("choices", [{}])[0]
                    calls = choice.get("message", {}).get("tool_calls", [])
                    if choice.get("finish_reason") != ("tool_calls" if calls else "stop"):
                        raise ProviderError("该轮 API 输出终态与工具请求不匹配，禁止将截断当成完成")
                if not calls:
                    break
                if step >= settings.get("max_tool_rounds", 12) or len(calls) > 100:
                    raise ProviderError("计算工具调用超限，任务保留未完成状态")
                if protocol == "responses":
                    payload["input"].extend(result.get("output", []))
                else:
                    payload["messages"].append(result["choices"][0]["message"])
                from .calculator import calculate
                for call in calls:
                    name = call.get("name") if protocol == "responses" else call["function"]["name"]
                    arguments = call.get("arguments") if protocol == "responses" else call["function"]["arguments"]
                    try:
                        if name != "calculate":
                            raise ValueError("不支持的工具")
                        args = json.loads(arguments)
                        validate(args, tool["parameters"])
                        calculation = calculate(args["expression"])
                    except Exception as error:
                        calculation = {"error": str(error)}
                    calculations.append({"tool": name, "arguments": arguments, "result": calculation})
                    if protocol == "responses":
                        payload["input"].append({"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(calculation, ensure_ascii=False)})
                    else:
                        payload["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(calculation, ensure_ascii=False)})
                if trace_dir:
                    safe_log(trace_dir / "calculations.json", calculations)
            if trace_dir:
                safe_log(trace_dir / "response.json", result)
            if protocol == "responses":
                if result.get("status") != "completed":
                    raise ProviderError("API 响应未完成（可能输出超限或被拒绝）；原始响应已保留")
                output = "".join(part.get("text", "") for entry in result.get("output", []) if entry.get("type") == "message" for part in entry.get("content", []) if part.get("type") == "output_text")
            else:
                choice = result.get("choices", [{}])[0]
                if choice.get("finish_reason") != "stop":
                    raise ProviderError("API 输出未正常结束；未将截断内容当成业务结果")
                output = choice.get("message", {}).get("content", "")
            value = json.loads(output)
            if api_key in output:
                raise ProviderError("服务输出包含认证信息，已拦截并仅保留脱敏日志")
            validate(value, schema)
            trace.update({"finished_at": now(), "status": "completed", "reported_model": result.get("model"), "usage": result.get("usage", {}), "usage_steps": usage_steps, "tool_calls": len(calculations)})
            return value
        except urllib.error.HTTPError as error:
            trace.update({"status": "failed", "http_status": error.code})
            raise ProviderError("模型接口 HTTP " + str(error.code) + "；请核对地址、账户、精确模型名和 xhigh 支持，不会自动降级") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            trace.update({"status": "failed", "error_type": type(error).__name__})
            raise ProviderError("模型网络调用失败：" + type(error).__name__ + "；结果未成功，未自动重复付费调用") from None
        except Exception as error:
            trace.update({"status": "failed", "error_type": type(error).__name__})
            if api_key in str(error):
                raise ProviderError(str(error).replace(api_key, "[REDACTED]")) from None
            raise
        finally:
            if trace_dir:
                safe_log(trace_dir / "trace.json", trace)
