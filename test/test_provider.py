"""No-network protocol tests: completion boundaries, tool loops and secret handling."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from finresearch.provider import APIProvider, NoRedirect, ProviderError, read_event_stream
from finresearch.schemas import obj, TEXT


MODEL = "gpt-6-astra"
FAKE_SECRET = "test-only-no-network-secret-73ab"
OUTPUT_SCHEMA = obj({"value": TEXT})


def settings(protocol="responses", max_tool_rounds=3):
    return {
        "api": {"protocol": protocol, "base_url_env": "TEST_FIN_BASE", "api_key_env": "TEST_FIN_KEY",
                "timeout_seconds": 10, "max_response_bytes": 100000, "max_output_tokens": 1000,
                "max_tool_rounds": max_tool_rounds},
        "models": {role: {"model": MODEL, "reasoning_effort": "xhigh"} for role in ("business", "audit", "discovery", "improvement")},
    }


def final_response(value="42", model=MODEL, status="completed"):
    return {"model": model, "status": status, "usage": {"output_tokens": 5},
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps({"value": value})}]}]}


def tool_response(expression="0.1 + 0.2", status="completed", model=MODEL, name="calculate"):
    return {"model": model, "status": status, "output": [{"type": "function_call", "call_id": "call-1",
            "name": name, "arguments": json.dumps({"expression": expression})}]}


def chat_response(value="42", finish="stop", expression=None, model=MODEL):
    message = {"role": "assistant", "content": json.dumps({"value": value})}
    if expression is not None:
        message.update(content=None, tool_calls=[{"id": "call-1", "type": "function", "function": {
            "name": "calculate", "arguments": json.dumps({"expression": expression})}}])
    return {"model": model, "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": {}}


def sse(events, done=True):
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    if done:
        data += "data: [DONE]\n\n"
    return data.encode("utf-8")


class Response(io.BytesIO):
    def __init__(self, value, streaming=False):
        super().__init__(value if streaming else json.dumps(value).encode("utf-8"))
        self.headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append({"body": json.loads(request.data), "headers": dict(request.header_items()), "url": request.full_url})
        if not self.responses:
            raise AssertionError("Unexpected extra API request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ProviderProtocolTests(unittest.TestCase):
    def invoke(self, responses, protocol="responses", role="business", trace_dir=None, config=None):
        configuration = config or settings(protocol)
        provider = APIProvider(configuration)
        transport = Transport(responses)
        with patch.object(provider, "credentials", return_value=("https://example.invalid/v1", FAKE_SECRET, protocol)), \
                patch("finresearch.provider.urllib.request.build_opener", return_value=transport):
            value = provider.call(role, "role-specific instructions", "full original document", OUTPUT_SCHEMA, trace_dir=trace_dir)
        return value, transport

    def test_responses_sse_requires_explicit_completion(self):
        stream = sse([{"type": "response.output_text.delta", "delta": '{"value":"42"}'}])
        with self.assertRaisesRegex(ProviderError, "中断"):
            self.invoke([Response(stream, streaming=True)])

    def test_responses_completed_stream_is_decoded(self):
        stream = sse([{"type": "response.completed", "response": final_response()}])
        value, _ = self.invoke([Response(stream, streaming=True)])
        self.assertEqual(value, {"value": "42"})

    def test_responses_incomplete_final_output_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "未完成"):
            self.invoke([Response(final_response(status="incomplete"))])

    def test_incomplete_response_tool_round_must_not_be_replayed(self):
        transport = Transport([Response(tool_response(status="incomplete")), Response(final_response())])
        provider = APIProvider(settings())
        with patch.object(provider, "credentials", return_value=("https://example.invalid/v1", FAKE_SECRET, "responses")), \
                patch("finresearch.provider.urllib.request.build_opener", return_value=transport):
            with self.assertRaises(ProviderError):
                provider.call("business", "instructions", "document", OUTPUT_SCHEMA)
        self.assertEqual(len(transport.requests), 1)

    def test_chat_length_tool_round_must_not_be_replayed(self):
        transport = Transport([Response(chat_response(finish="length", expression="1+1")), Response(chat_response())])
        provider = APIProvider(settings("chat_completions"))
        with patch.object(provider, "credentials", return_value=("https://example.invalid/v1", FAKE_SECRET, "chat_completions")), \
                patch("finresearch.provider.urllib.request.build_opener", return_value=transport):
            with self.assertRaises(ProviderError):
                provider.call("business", "instructions", "document", OUTPUT_SCHEMA)
        self.assertEqual(len(transport.requests), 1)

    def test_responses_calculate_loop_keeps_exact_result_and_original(self):
        with tempfile.TemporaryDirectory() as directory:
            value, transport = self.invoke([Response(tool_response()), Response(final_response("0.3"))], trace_dir=directory)
            sent = transport.requests[1]["body"]["input"]
            self.assertEqual(sent[0]["content"][0]["text"], "full original document")
            output = next(item for item in sent if item.get("type") == "function_call_output")
            self.assertEqual(json.loads(output["output"])["value"], "0.3")
            self.assertEqual(value["value"], "0.3")
            trace = json.loads((Path(directory) / "trace.json").read_text(encoding="utf-8"))
            self.assertEqual(trace["tool_calls"], 1)
            self.assertEqual(trace["status"], "completed")

    def test_chat_sse_reconstructs_fragmented_tool_arguments(self):
        events = [
            {"model": MODEL, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "calculate", "arguments": '{"expression":"0.1'}}]}, "finish_reason": None}]},
            {"model": MODEL, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '+0.2"}'}}]}, "finish_reason": "tool_calls"}]},
        ]
        value, transport = self.invoke([Response(sse(events), streaming=True), Response(chat_response("0.3"))], protocol="chat_completions")
        tool_message = transport.requests[1]["body"]["messages"][-1]
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(json.loads(tool_message["content"])["value"], "0.3")
        self.assertEqual(value["value"], "0.3")

    def test_failed_calculation_can_be_corrected_in_same_role(self):
        value, transport = self.invoke([Response(tool_response("1/0")), Response(tool_response("1/2")), Response(final_response("0.5"))])
        first_tool_result = transport.requests[1]["body"]["input"][-1]
        self.assertIn("error", json.loads(first_tool_result["output"]))
        self.assertEqual(value["value"], "0.5")

    def test_tool_budget_does_not_allow_final_success_after_exhaustion(self):
        with self.assertRaisesRegex(ProviderError, "超限"):
            self.invoke([Response(tool_response()), Response(final_response())], config=settings(max_tool_rounds=0))

    def test_reported_model_mismatch_blocks_before_tool_execution(self):
        with patch("finresearch.calculator.calculate") as calculate:
            with self.assertRaisesRegex(ProviderError, "模型名"):
                self.invoke([Response(tool_response(model="wrong-model"))])
        calculate.assert_not_called()

    def test_missing_reported_model_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "模型名"):
            self.invoke([Response(final_response(model=None))])

    def test_explicit_reported_model_alias_can_be_accepted(self):
        config = settings()
        config["models"]["business"]["accepted_reported_models"] = ["gpt-6-astra-version"]
        value, _ = self.invoke([Response(final_response(model="gpt-6-astra-version"))], config=config)
        self.assertEqual(value["value"], "42")

    def test_success_logs_never_serialize_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            _, transport = self.invoke([Response(final_response())], trace_dir=directory)
            self.assertEqual(transport.requests[0]["headers"]["Authorization"], "Bearer " + FAKE_SECRET)
            for path in Path(directory).glob("*.json"):
                self.assertNotIn(FAKE_SECRET, path.read_text(encoding="utf-8"))

    def test_http_error_body_and_credentials_do_not_reach_logs(self):
        failure = urllib.error.HTTPError("https://example.invalid/v1", 401, "Unauthorized " + FAKE_SECRET, {}, io.BytesIO(FAKE_SECRET.encode()))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderError) as caught:
                self.invoke([failure], trace_dir=directory)
            self.assertNotIn(FAKE_SECRET, str(caught.exception))
            for path in Path(directory).glob("*.json"):
                self.assertNotIn(FAKE_SECRET, path.read_text(encoding="utf-8"))

    def test_relay_echoed_credentials_are_redacted_before_response_logging(self):
        response = final_response()
        response["provider_diagnostics"] = {"authorization": "Bearer " + FAKE_SECRET}
        with tempfile.TemporaryDirectory() as directory:
            self.invoke([Response(response)], trace_dir=directory)
            for path in Path(directory).glob("*.json"):
                self.assertNotIn(FAKE_SECRET, path.read_text(encoding="utf-8"))

    def test_credentials_in_business_output_are_blocked_and_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderError) as caught:
                self.invoke([Response(final_response(FAKE_SECRET))], trace_dir=directory)
            self.assertNotIn(FAKE_SECRET, str(caught.exception))
            for path in Path(directory).glob("*.json"):
                self.assertNotIn(FAKE_SECRET, path.read_text(encoding="utf-8"))

    def test_role_context_does_not_inherit_previous_tool_loop(self):
        provider = APIProvider(settings())
        transport = Transport([Response(tool_response()), Response(final_response()), Response(final_response())])
        with patch.object(provider, "credentials", return_value=("https://example.invalid/v1", FAKE_SECRET, "responses")), \
                patch("finresearch.provider.urllib.request.build_opener", return_value=transport):
            provider.call("business", "PRIVATE BUSINESS SKILL", "execution originals", OUTPUT_SCHEMA)
            provider.call("audit", "INDEPENDENT AUDIT STANDARD", "audit originals and result", OUTPUT_SCHEMA)
        audit_request = transport.requests[2]["body"]
        self.assertNotIn("PRIVATE BUSINESS SKILL", json.dumps(audit_request))
        self.assertEqual(len(audit_request["input"]), 1)
        self.assertNotIn("function_call_output", json.dumps(audit_request["input"]))

    def test_invalid_final_json_leaves_failed_trace(self):
        response = final_response()
        response["output"][0]["content"][0]["text"] = "not json"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.invoke([Response(response)], trace_dir=directory)
            trace = json.loads((Path(directory) / "trace.json").read_text(encoding="utf-8"))
            self.assertEqual(trace["status"], "failed")

    def test_redirect_handler_never_forwards_api_request(self):
        with self.assertRaisesRegex(ProviderError, "重定向"):
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid/")

    def test_sse_size_limit_counts_all_events(self):
        stream = io.BytesIO(sse([{"type": "response.completed", "response": final_response()}]))
        with self.assertRaisesRegex(ProviderError, "超出"):
            read_event_stream(stream, "responses", 10, float("inf"))

    def test_failed_response_exposes_only_safe_diagnostic_code(self):
        response = final_response()
        response.update(status="failed", error={"code": "gateway_concurrency_limit", "message": FAKE_SECRET})
        with self.assertRaisesRegex(ProviderError, "gateway_concurrency_limit") as caught:
            self.invoke([Response(response)])
        self.assertNotIn(FAKE_SECRET, str(caught.exception))

    def test_sse_deadline_failure_is_distinct_from_size_failure(self):
        stream = io.BytesIO(sse([{"type": "response.completed", "response": final_response()}]))
        with self.assertRaisesRegex(ProviderError, "总时限"):
            read_event_stream(stream, "responses", 1000000, 0)

    def test_sse_error_keeps_redacted_service_diagnostic(self):
        events = [{"type": "error", "code": "upstream_unavailable", "message": "Diagnostic " + FAKE_SECRET}]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderError):
                self.invoke([Response(sse(events), streaming=True)], trace_dir=directory)
            saved = (Path(directory) / "stream-error.json").read_text(encoding="utf-8")
            self.assertIn("upstream_unavailable", saved)
            self.assertNotIn(FAKE_SECRET, saved)

    def test_failed_stream_partial_is_diagnostic_only_and_never_contains_reasoning(self):
        events = [{"type": "response.output_text.delta", "delta": '{"value":"' + FAKE_SECRET},
                  {"type": "response.reasoning_text.delta", "delta": "PRIVATE REASONING"},
                  {"type": "error", "code": "upstream_unavailable"}]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderError):
                self.invoke([Response(sse(events), streaming=True)], trace_dir=directory)
            path = Path(directory) / "partial-output-step-00.json"
            saved = path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(saved)["status"], "incomplete_diagnostic_only")
            self.assertNotIn(FAKE_SECRET, saved)
            self.assertNotIn("PRIVATE REASONING", saved)
            self.assertFalse((Path(directory) / "response.json").exists())


if __name__ == "__main__":
    unittest.main()
