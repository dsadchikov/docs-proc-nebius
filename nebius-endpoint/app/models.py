from typing import Optional
from pydantic import BaseModel, Field, field_validator

_ALLOWED_DOCUMENT_TYPES = {"base64", "presigned_url", "nebius_object"}


class DocumentInput(BaseModel):
    type: str = Field(..., description="'presigned_url', 'base64', or 'nebius_object'")
    value: str = Field(..., description="URL, base64-encoded content, or NOS object key")
    mime_type: str = Field(default="image/jpeg")
    page: Optional[int] = Field(default=None, description="Page number for PDF (1-based)")

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in _ALLOWED_DOCUMENT_TYPES:
            raise ValueError(
                f"document.type must be one of {sorted(_ALLOWED_DOCUMENT_TYPES)}, got '{v}'"
            )
        return v


class RecognizeOptions(BaseModel):
    include_bounding_boxes: bool = Field(default=False)
    include_confidence: bool = Field(default=True)
    confidence_mode: str = Field(default="both", description="'document'|'fields'|'both'")
    document_type_hint: Optional[str] = Field(default=None)


class RecognizeRequest(BaseModel):
    document: DocumentInput
    mode: str = Field(default="blueprint", description="'auto'|'raw'|'blueprint'|'double_check'")
    blueprint_id: Optional[str] = Field(default=None)
    options: RecognizeOptions = Field(default_factory=RecognizeOptions)


class BoundingBox(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)


class FieldResult(BaseModel):
    value: Optional[str] = None
    confidence: Optional[int] = Field(default=None, ge=0, le=100)
    confidence_source: Optional[str] = Field(default=None, description="'logprobs'|'response_mean'|'model_reported'|'mock'")
    bounding_box: Optional[BoundingBox] = None


class RecognizeResponse(BaseModel):
    request_id: Optional[str] = None
    mode: str
    blueprint_id: Optional[str] = None
    document_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    routing: Optional[str] = None
    document_part: str = Field(default="page_1")
    fields: Optional[dict[str, FieldResult]] = None
    raw_text: Optional[str] = None
    classification: Optional[dict] = None


class PacketDocumentResult(BaseModel):
    pages: list[int]
    blueprint_id: Optional[str] = None
    classification: Optional[dict] = None
    fields: Optional[dict[str, FieldResult]] = None
    document_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    routing: Optional[str] = None
    raw_text: Optional[str] = None


class PacketResponse(BaseModel):
    request_id: Optional[str] = None
    mode: str = "packet"
    routing: Optional[str] = None
    document_part: str = "packet"
    documents: list[PacketDocumentResult]


class BlueprintField(BaseModel):
    name: str
    description: str = ""
    instruction: str = ""
    required: bool = True


class BlueprintMeta(BaseModel):
    id: str
    name: str
    version: int = 1
    status: str = "active"
    description: str = ""
    fields_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BlueprintCreate(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9_-]+$")
    name: str
    description: str = ""
    extraction_prompt: str = "Extract all fields from this document and return JSON."
    fields: list[BlueprintField]


class BlueprintUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    extraction_prompt: Optional[str] = None
    fields: Optional[list[BlueprintField]] = None


class PresignResponse(BaseModel):
    presigned_put_url: str
    nos_key: str
    expires_in: int


class BlueprintGenerateRequest(BaseModel):
    document: DocumentInput
    blueprint_id: str = Field(..., pattern=r"^[a-z0-9_-]+$")
    name: str
    description: str = ""
