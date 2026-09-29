"""Local OCR execution that preserves transcripts as review-only evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from finagent.contracts import EvidenceSpan, ExtractionCandidate, OcrTextSpan, OcrTranscript, utc_now
from finagent.storage import EvidenceStore


class OcrEngineUnavailable(RuntimeError):
    """Raised when the configured local OCR engine cannot run."""


@dataclass(frozen=True)
class OcrEngineOutput:
    engine: str
    engine_version: str
    spans: list[OcrTextSpan]


class OcrEngine(Protocol):
    def transcribe(self, raw: bytes, language: str) -> OcrEngineOutput: ...


class TesseractOcrEngine:
    """Optional local adapter; no text is sent outside the process."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable

    def transcribe(self, raw: bytes, language: str) -> OcrEngineOutput:
        try:
            from PIL import Image
            import pytesseract
        except ImportError as exc:
            raise OcrEngineUnavailable(
                "local OCR requires the optional 'ocr' dependency group"
            ) from exc
        try:
            from io import BytesIO

            if self.executable:
                pytesseract.pytesseract.tesseract_cmd = self.executable
            with Image.open(BytesIO(raw)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise OcrEngineUnavailable(
                        "multi-page raster OCR must be queued as one job per page"
                    )
                data = pytesseract.image_to_data(
                    image.copy(), lang=language, output_type=pytesseract.Output.DICT
                )
                version = str(pytesseract.get_tesseract_version())
        except OcrEngineUnavailable:
            raise
        except Exception as exc:
            raise OcrEngineUnavailable(f"Tesseract OCR failed: {type(exc).__name__}: {exc}") from exc
        return OcrEngineOutput(
            engine="tesseract",
            engine_version=version,
            spans=_tesseract_lines(data),
        )


def process_ocr_job(
    store: EvidenceStore,
    job_id: str,
    *,
    language: str = "chi_sim+eng",
    engine: OcrEngine | None = None,
    tesseract_executable: str | None = None,
) -> dict[str, object]:
    """Execute one queued OCR job and emit only an immutable transcript and candidates."""

    job = store.claim_ocr_job(job_id)
    started_at = job.started_at or utc_now()
    try:
        output = (engine or TesseractOcrEngine(tesseract_executable)).transcribe(
            store.read_document(job.document_id), language
        )
        finished_at = utc_now()
        transcript = OcrTranscript(
            schema="finagent.ocr-transcript.v1",
            id=hashlib.sha256(
                f"ocr-v1:{job.id}:{job.attempt_count}:{output.engine}:{output.engine_version}".encode("utf-8")
            ).hexdigest(),
            job_id=job.id,
            document_id=job.document_id,
            page_number=job.page_number,
            engine=output.engine,
            engine_version=output.engine_version,
            language=language,
            started_at=started_at,
            finished_at=finished_at,
            text="\n".join(span.text for span in output.spans),
            mean_confidence=_mean_confidence(output.spans),
            spans=output.spans,
        )
        store.put_ocr_transcript(transcript)
        candidates = candidates_from_ocr_transcript(transcript)
        store.put_candidates(candidates)
        completed = store.complete_ocr_job(job.id)
    except Exception as exc:
        failed = store.fail_ocr_job(job.id, f"{type(exc).__name__}: {exc}")
        return {
            "job_id": failed.id,
            "status": failed.status,
            "error": failed.error,
            "candidate_count": 0,
        }
    return {
        "job_id": completed.id,
        "status": completed.status,
        "transcript_id": transcript.id,
        "candidate_count": len(candidates),
        "mean_confidence": transcript.mean_confidence,
    }


def candidates_from_ocr_transcript(transcript: OcrTranscript) -> list[ExtractionCandidate]:
    """Turn OCR numeric lines into low-confidence, unmapped review candidates."""

    candidates: list[ExtractionCandidate] = []
    for span_index, span in enumerate(transcript.spans, start=1):
        for number_index, match in enumerate(_NUMBER.finditer(span.text), start=1):
            raw_value = match.group(0)
            label = _line_label(span.text, match.group(0))
            locator = f"ocr:{transcript.id}/span:{span_index}/number:{number_index}"
            candidate_id = hashlib.sha256(
                f"{transcript.document_id}:{locator}".encode("utf-8")
            ).hexdigest()
            candidates.append(
                ExtractionCandidate(
                    id=candidate_id,
                    document_id=transcript.document_id,
                    label=label,
                    raw_value=raw_value,
                    evidence=EvidenceSpan(
                        document_id=transcript.document_id,
                        locator=locator,
                        excerpt=span.text,
                        page_number=transcript.page_number,
                        block_type="ocr_line_numeric_candidate",
                        verification_status="unverified_candidate",
                    ),
                    parser=f"ocr:{transcript.engine}:{transcript.engine_version}",
                    confidence="low",
                    confidence_reason=(
                        "OCR output is an unmapped candidate; verify the original image, "
                        "table structure, unit, period and reporting scope before approval."
                    ),
                )
            )
    return candidates


_NUMBER = re.compile(r"(?<![\w.])(?:\(?-?[0-9][0-9,]*(?:\.[0-9]+)?\)?)(?![\w.])")


def _line_label(text: str, number: str) -> str:
    label = text.replace(number, "", 1).strip(" :：|-\t")
    return label or "unmapped OCR numeric token"


def _mean_confidence(spans: list[OcrTextSpan]) -> float | None:
    values = [span.confidence for span in spans if span.confidence is not None]
    return round(sum(values) / len(values), 3) if values else None


def _tesseract_lines(data: dict[str, list[object]]) -> list[OcrTextSpan]:
    grouped: dict[tuple[object, object, object, object], list[int]] = {}
    for index, value in enumerate(data.get("text", [])):
        if str(value).strip():
            key = tuple(data[name][index] for name in ("page_num", "block_num", "par_num", "line_num"))
            grouped.setdefault(key, []).append(index)
    spans: list[OcrTextSpan] = []
    for indexes in grouped.values():
        text = " ".join(str(data["text"][index]).strip() for index in indexes)
        confidences = [_confidence(data.get("conf", [])[index]) for index in indexes]
        valid_confidences = [value for value in confidences if value is not None]
        left = min(int(data["left"][index]) for index in indexes)
        top = min(int(data["top"][index]) for index in indexes)
        right = max(int(data["left"][index]) + int(data["width"][index]) for index in indexes)
        bottom = max(int(data["top"][index]) + int(data["height"][index]) for index in indexes)
        spans.append(
            OcrTextSpan(
                text=text,
                confidence=(sum(valid_confidences) / len(valid_confidences)) if valid_confidences else None,
                bounding_box=(left, top, right - left, bottom - top),
            )
        )
    return spans


def _confidence(value: object) -> float | None:
    try:
        confidence = float(str(value))
    except ValueError:
        return None
    return confidence if confidence >= 0 else None
