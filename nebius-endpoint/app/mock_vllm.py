"""Mock vLLM responses for CPU-only testing without GPU."""
import json


def mock_logprobs(text: str, chunk: int = 4, logprob: float = -0.08) -> list:
    """Synthetic per-token logprobs so the logprob confidence path runs on CPU (Req 13.8).

    Splits the text into fixed-size chunks; -0.08 per token ≈ 92% confidence.
    """
    return [{"token": text[i:i + chunk], "logprob": logprob} for i in range(0, len(text), chunk)]


def mock_vllm_response(system_prompt: str, user_prompt: str) -> str:
    if "document_type" in user_prompt and "blueprint_id" in user_prompt:
        return json.dumps({"document_type": "Personal Identity Card", "blueprint_id": "passport", "confidence": 88})
    if "verify" in system_prompt.lower() or "confirm" in system_prompt.lower():
        return json.dumps({"surname": {"confirmed": True, "corrected_value": None}, "given_names": {"confirmed": True, "corrected_value": None}})
    if "describe everything" in system_prompt.lower():
        return "Mock: document is a personal ID card with name, DOB, number. [MOCK MODE]"
    return json.dumps({
        "document_type": {"value": "Personal Identity Card", "confidence": 90},
        "surname": {"value": "MOCK_SURNAME", "confidence": 92},
        "given_names": {"value": "MOCK_NAME", "confidence": 91},
        "date_of_birth": {"value": "1990-01-15", "confidence": 88},
        "personal_number": {"value": "49001150001", "confidence": 95},
        "sex": {"value": "F", "confidence": 93},
        "date_of_expiry": {"value": "2029-10-09", "confidence": 89},
        "card_number": {"value": "12345678", "confidence": 94},
    })
