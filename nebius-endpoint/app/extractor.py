import json
import math
import re
import logging
import base64
from typing import NamedTuple, Optional

import httpx
import boto3
from botocore.config import Config as BotoConfig
from fastapi import HTTPException
from app.config import Config
from app.models import (DocumentInput, RecognizeRequest, RecognizeResponse, RecognizeOptions,
                        FieldResult, BoundingBox, PacketDocumentResult, PacketResponse)
from app.pdf_converter import pdf_to_single_page_image, pdf_to_images, get_pdf_page_count
from app.mock_vllm import mock_vllm_response, mock_logprobs
from app.router import clamp_confidence, get_routing

logger = logging.getLogger("app.extractor")


class VLLMResult(NamedTuple):
    text: str
    logprobs: Optional[list]  # choices[0].logprobs.content: [{token, logprob}, ...]


def blueprint_to_guided_schema(blueprint: dict) -> dict:
    """Blueprint fields[] → JSON Schema for vLLM guided_json (Req 14).

    Flat scalar values only — confidence comes from logprobs, not the model.
    """
    names = [f["name"] for f in blueprint.get("fields", [])]
    return {
        "type": "object",
        "properties": {n: {"type": ["string", "null"]} for n in names},
        "required": names,
        "additionalProperties": False,
    }


def verification_guided_schema(field_names: list) -> dict:
    """JSON Schema for the double_check verification pass."""
    return {
        "type": "object",
        "properties": {
            n: {
                "type": "object",
                "properties": {
                    "confirmed": {"type": "boolean"},
                    "corrected_value": {"type": ["string", "null"]},
                },
                "required": ["confirmed", "corrected_value"],
                "additionalProperties": False,
            }
            for n in field_names
        },
        "required": list(field_names),
        "additionalProperties": False,
    }


CLASSIFY_GUIDED_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "blueprint_id": {"type": ["string", "null"]},
        "confidence": {"type": "integer"},
    },
    "required": ["document_type", "blueprint_id", "confidence"],
    "additionalProperties": False,
}

# Meta-schema of the Rich Blueprint sections block (Req 14.5)
BLUEPRINT_META_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "inferenceType": {"type": "string", "enum": ["explicit", "inferred"]},
                        "instruction": {"type": "string"},
                        "required": {"type": "boolean"},
                    },
                    "required": ["inferenceType", "instruction", "required"],
                    "additionalProperties": False,
                },
            },
        },
    },
    "required": ["sections"],
    "additionalProperties": False,
}


def logprob_confidence(value, logprobs_content, field_name: str = "") -> tuple:
    """Per-field confidence from token logprobs (Req 13).

    Returns (confidence: int|None, source: str). source is 'logprobs' when the
    value span was located in the generated text, 'response_mean' when only a
    whole-response mean was possible, 'model_reported' when no logprobs exist.
    """
    if not logprobs_content:
        return None, "model_reported"
    try:
        tokens = [e["token"] if isinstance(e, dict) else getattr(e, "token", "") for e in logprobs_content]
        lps = [e["logprob"] if isinstance(e, dict) else getattr(e, "logprob", 0.0) for e in logprobs_content]
        full = "".join(tokens)
        sel = []
        if value is not None and str(value):
            needle = str(value)
            # Anchor the search after the field name key when possible —
            # the same value can appear under several fields.
            anchor = full.find(f'"{field_name}"') if field_name else -1
            start = full.find(needle, anchor if anchor >= 0 else 0)
            if start < 0:
                start = full.find(needle)
            if start >= 0:
                end = start + len(needle)
                pos = 0
                for tok, lp in zip(tokens, lps):
                    tok_start, tok_end = pos, pos + len(tok)
                    if tok_end > start and tok_start < end:
                        sel.append(lp)
                    pos = tok_end
                    if tok_start >= end:
                        break
        if sel:
            return clamp_confidence(100 * math.exp(sum(sel) / len(sel))), "logprobs"
        if lps:
            return clamp_confidence(100 * math.exp(sum(lps) / len(lps))), "response_mean"
    except Exception as exc:  # never let confidence math break extraction
        logger.warning("logprob_confidence failed: %s", exc)
    return None, "model_reported"


def _get_s3_client():
    """Return a boto3 S3 client pointed at NOS, or raise HTTP 503 if not configured."""
    if not Config.S3_ACCESS_KEY or not Config.S3_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Object storage not configured")
    return boto3.client(
        "s3",
        endpoint_url=Config.S3_ENDPOINT,
        aws_access_key_id=Config.S3_ACCESS_KEY,
        aws_secret_access_key=Config.S3_SECRET_KEY,
        region_name=Config.S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )


async def extract_document(http_client, request: RecognizeRequest, blueprint: Optional[dict]) -> RecognizeResponse:
    image_content, document_part = await preprocess_document(request.document)
    if request.mode == "raw":
        return await extract_raw(http_client, image_content, document_part, request.options)
    elif request.mode == "auto":
        return await extract_auto(http_client, image_content, document_part, request.options, None)
    elif request.mode == "double_check":
        return await extract_with_double_check(http_client, image_content, document_part, blueprint, request.options)
    else:
        return await extract_with_blueprint(http_client, image_content, document_part, blueprint, request.options)


async def _fetch_document_bytes(document: DocumentInput) -> bytes:
    """Resolve any document.type to raw bytes."""
    if document.type == "nebius_object":
        s3 = _get_s3_client()  # raises 503 if NOS not configured
        try:
            response = s3.get_object(Bucket=Config.S3_BUCKET, Key=document.value)
            return response["Body"].read()
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to fetch nebius_object %s: %s", document.value, exc)
            raise HTTPException(status_code=503, detail=f"Failed to fetch object from NOS: {exc}") from exc
    if document.type == "presigned_url":
        async with httpx.AsyncClient(timeout=Config.FETCH_TIMEOUT) as client:
            resp = await client.get(document.value)
            resp.raise_for_status()
            return resp.content
    return base64.b64decode(document.value)


async def preprocess_document(document: DocumentInput) -> tuple:
    page = document.page or 1

    # --- Fetch raw bytes for types that need them up front ---
    raw_bytes: Optional[bytes] = None

    if document.type == "nebius_object":
        raw_bytes = await _fetch_document_bytes(document)

    # --- PDF handling ---
    if document.mime_type == "application/pdf":
        if document.type == "nebius_object":
            pdf_bytes = raw_bytes
        else:
            pdf_bytes = await _fetch_document_bytes(document)

        total_pages = get_pdf_page_count(pdf_bytes)
        if page > total_pages:
            raise ValueError(f"Page {page} requested but PDF has only {total_pages} pages")
        img_b64 = pdf_to_single_page_image(pdf_bytes, page=page)
        image_content = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        document_part = f"page_{page}_of_{total_pages}" if total_pages > 1 else "single"

    # --- Image / other content ---
    elif document.type == "presigned_url":
        image_content = {"type": "image_url", "image_url": {"url": document.value}}
        document_part = "single"

    elif document.type == "nebius_object":
        # Encode fetched bytes as base64 data URL
        mime = document.mime_type or "image/jpeg"
        img_b64 = base64.b64encode(raw_bytes).decode()
        image_content = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        document_part = "single"

    else:
        # base64
        mime = document.mime_type or "image/jpeg"
        image_content = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{document.value}"}}
        document_part = "single"

    return image_content, document_part


async def extract_raw(http_client, image_content, document_part, options):
    system = "You are a document analysis system. Describe everything you see in this document image. Extract all visible text, identify the document type, and list all fields with their values."
    result = await call_vllm(http_client, system, "Describe this document completely.", image_content)
    return RecognizeResponse(mode="raw", document_part=document_part, raw_text=result.text)


async def classify_page(http_client, image_content, blueprint_store) -> dict:
    """One classification pass: which known blueprint matches this image (Req 13.7)."""
    known_ids = []
    if blueprint_store:
        known_ids = [bp["id"] for bp in blueprint_store.list_all()]
    ids_str = ", ".join(known_ids) if known_ids else "passport, invoice, contract, bank_statement, driver_license"
    classify_prompt = f'Analyze this document. Return JSON: {{"document_type": "...", "blueprint_id": "...", "confidence": 0-100}}. Known IDs: {ids_str}. If none match, set blueprint_id to null.'
    result = await call_vllm(http_client, "You are a document classification system. Return only JSON.",
                             classify_prompt, image_content, guided_schema=CLASSIFY_GUIDED_SCHEMA)
    classification = parse_json_response(result.text)
    detected_id = classification.get("blueprint_id")
    # Req 13.7 — classification confidence from logprobs of the predicted blueprint_id
    if detected_id:
        lp_conf, lp_source = logprob_confidence(detected_id, result.logprobs, "blueprint_id")
        if lp_conf is not None:
            classification["confidence"] = lp_conf
            classification["confidence_source"] = "mock" if Config.MOCK_VLLM else lp_source
    return classification


async def extract_auto(http_client, image_content, document_part, options, blueprint_store):
    classification = await classify_page(http_client, image_content, blueprint_store)
    detected_id = classification.get("blueprint_id")
    if detected_id and blueprint_store:
        bp = blueprint_store.get(detected_id)
        if bp:
            result_resp = await extract_with_blueprint(http_client, image_content, document_part, bp, options)
            result_resp.mode = "auto"
            result_resp.classification = classification
            return result_resp
    return RecognizeResponse(mode="auto", document_part=document_part, classification=classification,
                            routing="escalate_to_operator", raw_text=f"Blueprint not found: {classification.get('document_type', 'unknown')}")


async def extract_with_blueprint(http_client, image_content, document_part, blueprint, options):
    system = build_system_prompt(blueprint, options)
    user = blueprint.get("extraction_prompt", "Extract all fields from this document and return JSON.")
    # Guided decoding uses a flat scalar schema — incompatible with bbox objects (Req 14.3)
    guided = None if options.include_bounding_boxes else blueprint_to_guided_schema(blueprint)
    result = await call_vllm(http_client, system, user, image_content, guided_schema=guided)
    fields = parse_model_response(result.text, blueprint, options, logprobs_content=result.logprobs)
    doc_conf = None
    routing = None
    if options.include_confidence and options.confidence_mode in ("document", "both"):
        doc_conf = calculate_document_confidence(fields)
        routing = determine_routing(doc_conf)
    if not options.include_confidence or options.confidence_mode == "document":
        for f in fields.values():
            f.confidence = None
            f.confidence_source = None
    return RecognizeResponse(mode="blueprint", blueprint_id=blueprint["id"], document_confidence=doc_conf,
                            routing=routing, document_part=document_part, fields=fields)


async def extract_with_double_check(http_client, image_content, document_part, blueprint, options):
    if blueprint:
        system = build_system_prompt(blueprint, options)
        user = blueprint.get("extraction_prompt", "Extract all fields.")
        guided = None if options.include_bounding_boxes else blueprint_to_guided_schema(blueprint)
    else:
        system = "You are a document recognition system. Extract all visible fields and return JSON."
        user = "Extract all fields from this document."
        guided = None
    result = await call_vllm(http_client, system, user, image_content, guided_schema=guided)
    fields = (parse_model_response(result.text, blueprint, options, logprobs_content=result.logprobs)
              if blueprint else parse_raw_fields(result.text, logprobs_content=result.logprobs))
    verify_prompt = build_verification_prompt(fields)
    verify_guided = verification_guided_schema([n for n, f in fields.items() if f.value])
    verify_resp = await call_vllm(http_client, "You are a document verification system. For each field, confirm if visible. Return JSON: {field: {confirmed: bool, corrected_value: str|null}}",
                                  verify_prompt, image_content, guided_schema=verify_guided)
    fields = apply_verification(fields, verify_resp.text)
    doc_conf = calculate_document_confidence(fields) if options.include_confidence else None
    routing = determine_routing(doc_conf) if doc_conf is not None else None
    return RecognizeResponse(mode="double_check", blueprint_id=blueprint["id"] if blueprint else None,
                            document_confidence=doc_conf, routing=routing, document_part=document_part, fields=fields)


_ROUTING_SEVERITY = {"auto_classified": 0, "review_required": 1, "escalate_to_operator": 2}


def most_conservative_routing(routings) -> Optional[str]:
    """Top-level packet routing = worst per-document routing (Req 16.3)."""
    known = [r for r in routings if r in _ROUTING_SEVERITY]
    if not known:
        return None
    return max(known, key=lambda r: _ROUTING_SEVERITY[r])


def group_consecutive_pages(page_blueprints: list) -> list:
    """Group consecutive pages with the same classified blueprint_id (Req 16.1).

    Input: one blueprint_id (or None) per page, in page order.
    Output: [{"blueprint_id": ..., "pages": [1-based page numbers]}] — a partition
    of the input pages (P13).
    """
    groups = []
    for page_num, bp_id in enumerate(page_blueprints, 1):
        if groups and groups[-1]["blueprint_id"] == bp_id:
            groups[-1]["pages"].append(page_num)
        else:
            groups.append({"blueprint_id": bp_id, "pages": [page_num]})
    return groups


async def extract_packet(http_client, document: DocumentInput, options, blueprint_store) -> PacketResponse:
    """mode=packet: classify every PDF page, group consecutive same-type pages
    into logical documents, extract each (Req 16)."""
    if document.mime_type != "application/pdf":
        # Req 16.5 — non-PDF behaves as auto: a packet of one
        image_content, document_part = await preprocess_document(document)
        result = await extract_auto(http_client, image_content, document_part, options, blueprint_store)
        entry = PacketDocumentResult(pages=[1], blueprint_id=result.blueprint_id,
                                     classification=result.classification, fields=result.fields,
                                     document_confidence=result.document_confidence,
                                     routing=result.routing or "escalate_to_operator",
                                     raw_text=result.raw_text)
        return PacketResponse(routing=entry.routing, documents=[entry])

    pdf_bytes = await _fetch_document_bytes(document)
    total_pages = get_pdf_page_count(pdf_bytes)
    if total_pages > Config.PDF_MAX_PAGES:
        raise HTTPException(422, detail=f"Packet has {total_pages} pages, limit is {Config.PDF_MAX_PAGES}")
    pages_b64 = pdf_to_images(pdf_bytes)

    page_images = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                   for b64 in pages_b64]
    classifications = []
    for ic in page_images:
        cls = await classify_page(http_client, ic, blueprint_store)
        bp_id = cls.get("blueprint_id")
        if bp_id and (not blueprint_store or not blueprint_store.get(bp_id)):
            bp_id = None  # classified to an unknown blueprint → unclassified (Req 16.4)
        classifications.append((bp_id, cls))

    documents = []
    for group in group_consecutive_pages([c[0] for c in classifications]):
        first_page = group["pages"][0]
        ic = page_images[first_page - 1]
        cls = classifications[first_page - 1][1]
        part = f"pages_{group['pages'][0]}_{group['pages'][-1]}" if len(group["pages"]) > 1 else f"page_{first_page}"
        if group["blueprint_id"]:
            bp = blueprint_store.get(group["blueprint_id"])
            res = await extract_with_blueprint(http_client, ic, part, bp, options)
            documents.append(PacketDocumentResult(pages=group["pages"], blueprint_id=bp["id"],
                                                  classification=cls, fields=res.fields,
                                                  document_confidence=res.document_confidence,
                                                  routing=res.routing))
        else:
            res = await extract_raw(http_client, ic, part, options)
            documents.append(PacketDocumentResult(pages=group["pages"], blueprint_id=None,
                                                  classification=cls, raw_text=res.raw_text,
                                                  routing="escalate_to_operator"))

    return PacketResponse(routing=most_conservative_routing([d.routing for d in documents]),
                          documents=documents)


def build_system_prompt(blueprint, options):
    fields_desc = []
    for f in blueprint.get("fields", []):
        d = f"- {f['name']}: {f.get('description', '')}"
        if f.get("instruction"):
            d += f" ({f['instruction']})"
        fields_desc.append(d)
    hint = f"\nDocument type hint: {options.document_type_hint}" if options.document_type_hint else ""
    nl = chr(10)
    if options.include_bounding_boxes:
        # Legacy nested format — bbox objects are incompatible with the flat guided schema
        shape = 'Return JSON with fields. For each: provide "value" (text or null), "confidence" (0-100), and "bounding_box": {"x","y","width","height"} normalized 0-1.'
    else:
        # Flat scalar format for guided decoding (Req 14.3); confidence comes from logprobs
        shape = 'Return a JSON object mapping each field name to its extracted value as a string, or null if not visible.'
    return f"You are a document recognition system. Extract structured data from the document image.{hint}\n{shape}\n\nFields:\n{nl.join(fields_desc)}\n\nReturn ONLY valid JSON, no markdown fences."


def build_verification_prompt(fields):
    items = [f'- "{n}": "{f.value}"' for n, f in fields.items() if f.value]
    nl = chr(10)
    return f"Previously extracted:\n{nl.join(items)}\n\nVerify each field. Return JSON: {{\"field\": {{\"confirmed\": bool, \"corrected_value\": str|null}}}}"


def apply_verification(fields, verify_response):
    verification = parse_json_response(verify_response)
    for name, result in verification.items():
        if name in fields and isinstance(result, dict):
            if not result.get("confirmed", True):
                fields[name].confidence = 0
                if result.get("corrected_value"):
                    fields[name].value = result["corrected_value"]
                    fields[name].confidence = 60
            else:
                if fields[name].confidence is not None:
                    fields[name].confidence = min(100, fields[name].confidence + 10)
    return fields


async def call_vllm(http_client, system_prompt, user_prompt, image_content, guided_schema=None) -> VLLMResult:
    if Config.MOCK_VLLM:
        text = mock_vllm_response(system_prompt, user_prompt)
        return VLLMResult(text=text, logprobs=mock_logprobs(text))
    payload = {"model": Config.VLLM_MODEL_NAME, "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [image_content, {"type": "text", "text": user_prompt}]}
    ], "max_tokens": 4096, "temperature": 0.0, "logprobs": True}
    if guided_schema is not None:
        payload["guided_json"] = guided_schema
    response = await http_client.post("/v1/chat/completions", json=payload)
    if response.status_code != 200 and guided_schema is not None:
        # Req 14.4 — the request must not fail solely because guided decoding is unavailable
        logger.warning("guided_json_fallback: vLLM %s, retrying without guided_json", response.status_code)
        payload.pop("guided_json")
        response = await http_client.post("/v1/chat/completions", json=payload)
    if response.status_code != 200:
        raise Exception(f"vLLM error {response.status_code}: {response.text[:200]}")
    choice = response.json()["choices"][0]
    lp = (choice.get("logprobs") or {}).get("content") or None
    return VLLMResult(text=choice["message"]["content"], logprobs=lp)


def parse_json_response(raw):
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}


def _field_confidence(value, name, logprobs_content, reported=None):
    """Resolve (confidence, source) for one extracted field (Req 13).

    Logprobs win; model-reported (or flat 80) only when logprobs are absent.
    """
    conf, source = logprob_confidence(value, logprobs_content, name)
    if conf is not None:
        return conf, ("mock" if Config.MOCK_VLLM else source)
    if reported is not None:
        return clamp_confidence(reported), "model_reported"
    return 80, "model_reported"


def parse_model_response(raw, blueprint, options, logprobs_content=None):
    parsed = parse_json_response(raw)
    fields = {}
    no_lp_source = "mock" if Config.MOCK_VLLM else ("logprobs" if logprobs_content else "model_reported")
    for fd in blueprint.get("fields", []):
        name = fd["name"]
        if name in parsed and parsed[name] is not None:
            val = parsed[name]
            if isinstance(val, dict):
                bbox = None
                if options.include_bounding_boxes and "bounding_box" in val:
                    bb = val["bounding_box"]
                    try:
                        bbox = BoundingBox(x=float(bb.get("x",0)), y=float(bb.get("y",0)), width=float(bb.get("width",0)), height=float(bb.get("height",0)))
                    except (ValueError, TypeError):
                        bbox = None
                conf, source = _field_confidence(val.get("value"), name, logprobs_content, reported=val.get("confidence"))
                fields[name] = FieldResult(value=val.get("value"),
                                           confidence=conf if options.include_confidence else None,
                                           confidence_source=source if options.include_confidence else None,
                                           bounding_box=bbox)
            else:
                conf, source = _field_confidence(str(val), name, logprobs_content)
                fields[name] = FieldResult(value=str(val),
                                           confidence=conf if options.include_confidence else None,
                                           confidence_source=source if options.include_confidence else None)
        else:
            fields[name] = FieldResult(value=None, confidence=0 if options.include_confidence else None,
                                       confidence_source=no_lp_source if options.include_confidence else None)
    return fields


def parse_raw_fields(raw, logprobs_content=None):
    parsed = parse_json_response(raw)
    fields = {}
    for k, v in parsed.items():
        value = v.get("value", str(v)) if isinstance(v, dict) else str(v)
        reported = v.get("confidence") if isinstance(v, dict) else None
        conf, source = _field_confidence(value, k, logprobs_content, reported=reported)
        fields[k] = FieldResult(value=value, confidence=conf, confidence_source=source)
    return fields


def calculate_document_confidence(fields):
    confs = [f.confidence for f in fields.values() if f.confidence is not None]
    return clamp_confidence(sum(confs) / len(confs)) if confs else 0


def determine_routing(confidence):
    return get_routing(confidence)


async def generate_blueprint_from_document(
    http_client,
    document: DocumentInput,
    blueprint_id: str,
    name: str,
    description: str,
) -> dict:
    """Two-pass VLM workflow: raw description → structured Rich Blueprint JSON.

    Pass 1 — free-text description of all visible fields and sections.
    Pass 2 — structured Rich Blueprint Format JSON from that description.

    Returns a full blueprint dict with status="draft".
    """
    from datetime import datetime, timezone

    image_content, _ = await preprocess_document(document)

    # ------------------------------------------------------------------
    # Pass 1: raw document description
    # ------------------------------------------------------------------
    system_pass1 = (
        "You are a document analysis expert. Carefully examine this document image and "
        "describe every visible field, label, value, section, and identifier you can see. "
        "Be thorough and specific."
    )
    user_pass1 = (
        "Describe all fields and sections in this document. "
        "List every label and what type of value it contains (text, date, number, code, etc.)."
    )
    raw_description = (await call_vllm(http_client, system_pass1, user_pass1, image_content)).text

    # ------------------------------------------------------------------
    # Pass 2: structured blueprint generation
    # ------------------------------------------------------------------
    system_pass2 = (
        'You are a document schema designer. Based on the document description provided, '
        'generate a Rich Blueprint JSON schema.\n\n'
        'The output MUST be valid JSON in exactly this format:\n'
        '{\n'
        '  "sections": {\n'
        '    "SECTION_NAME": {\n'
        '      "field_name": {\n'
        '        "inferenceType": "explicit" | "inferred",\n'
        '        "instruction": "extraction instruction, e.g. YYYY-MM-DD or uppercase as printed",\n'
        '        "required": true | false\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        'Rules:\n'
        '- Use inferenceType "explicit" for fields read verbatim (names, numbers, codes)\n'
        '- Use inferenceType "inferred" for fields that require interpretation '
        '(dates → YYYY-MM-DD, sex → M or F)\n'
        '- Group fields into logical SECTION_NAMEs '
        '(e.g. DOCUMENT_METADATA, PERSONAL_INFO, DOCUMENT_DETAILS)\n'
        '- Return ONLY valid JSON, no markdown fences, no explanation'
    )
    user_pass2 = (
        f"Document description:\n{raw_description}\n\n"
        "Generate the Rich Blueprint JSON schema for this document type."
    )
    # Req 14.5 — guided decoding with the Rich Blueprint meta-schema
    schema_result = await call_vllm(http_client, system_pass2, user_pass2, image_content,
                                    guided_schema=BLUEPRINT_META_SCHEMA)
    sections = parse_json_response(schema_result.text)

    now = datetime.now(timezone.utc).isoformat()
    blueprint = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "id": blueprint_id,
        "name": name,
        "version": 1,
        "status": "draft",
        "description": description,
        "extraction_prompt": "Extract all fields from this document as defined in the schema.",
        "document_parts": ["single"],
        # Handle both {"sections": {...}} and top-level sections dict from the model
        "sections": sections.get("sections", sections),
        "created_at": now,
        "updated_at": now,
    }
    return blueprint
