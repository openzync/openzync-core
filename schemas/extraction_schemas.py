"""Pydantic schemas for extraction schema CRUD operations.

Schemas define the JSON Schema contracts an organization uses for structured
extractions (``type='structured'``) or classification label sets
(``type='classification'``).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateExtractionSchemaRequest(BaseModel):
    """Request body for creating a new extraction schema.

    Attributes:
        name: Human-readable name, unique within the organization.
            Must start with a letter and contain only alphanumeric,
            underscore, hyphen, or space characters.
        json_schema: For ``type='structured'``, a valid JSON Schema document.
            For ``type='classification'``, a dict with keys ``intent``,
            ``emotion``, ``valence``, ``arousal`` each containing a list of
            allowed label strings.
        type: Schema type — ``'structured'`` (default) or ``'classification'``.
        prompt_template: Optional per-schema prompt template override.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_\- ]*$",
        examples=["invoice_extraction", "customer_intent_labels"],
    )
    json_schema: dict = Field(..., examples=[{"type": "object", "properties": {}}])
    type: str = Field(
        default="structured",
        pattern=r"^(structured|classification)$",
    )
    prompt_template: str | None = Field(default=None, max_length=10000)


class UpdateExtractionSchemaRequest(BaseModel):
    """Request body for updating an existing extraction schema.

    All fields are optional.  The ``type`` field is immutable after creation.
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_\- ]*$",
    )
    json_schema: dict | None = None
    prompt_template: str | None = Field(default=None, max_length=10000)
    is_active: bool | None = None


class ExtractionSchemaResponse(BaseModel):
    """Response model for a single extraction schema."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    name: str
    type: str
    json_schema: dict
    prompt_template: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ExtractionSchemaListResponse(BaseModel):
    """Response model for listing extraction schemas."""

    data: list[ExtractionSchemaResponse]
    total: int


class PreviewExtractionRequest(BaseModel):
    """Request body for previewing structured extraction against a schema.

    Attributes:
        json_schema: A valid JSON Schema (draft-07) document describing
            the fields to extract.
        sample_text: Raw conversation or document text to extract from.
        prompt_template: Optional raw Jinja2 override for the extraction
            prompt.  Defaults to ``extract_structured_v1.jinja2``.
    """

    json_schema: dict = Field(..., examples=[{"type": "object", "properties": {}}])
    sample_text: str = Field(..., min_length=1, max_length=20000)
    prompt_template: str | None = Field(default=None, max_length=10000)


class PreviewExtractionResponse(BaseModel):
    """Response model for an extraction preview (no persistence).

    Attributes:
        data: The extracted object (empty when nothing was extracted).
        validation_errors: Field-level ``"<path>: <message>"`` strings;
            empty when the extracted data conforms to the schema.
    """

    data: dict
    validation_errors: list[str]


class SchemaTemplateResponse(BaseModel):
    """Response model for a static starter schema template."""

    key: str
    name: str
    description: str
    json_schema: dict
    sample_text: str


SCHEMA_TEMPLATES: tuple[SchemaTemplateResponse, ...] = (
    SchemaTemplateResponse(
        key="invoice",
        name="Invoice",
        description=(
            "Extracts billing details from invoices and bills: invoice "
            "number, vendor, dates, line items, and totals. Use when "
            "ingesting accounts-payable documents or payment threads."
        ),
        json_schema={
            "type": "object",
            "required": ["invoice_number", "vendor", "total_amount"],
            "properties": {
                "invoice_number": {"type": "string"},
                "vendor": {"type": "string"},
                "invoice_date": {"type": "string"},
                "due_date": {"type": "string"},
                "currency": {"type": "string"},
                "total_amount": {"type": "number"},
            },
        },
        sample_text=(
            "Invoice INV-2024-1042 from Acme Corp, dated March 3rd. "
            "Total amount $1,245.00 USD, due April 2nd."
        ),
    ),
    SchemaTemplateResponse(
        key="contact",
        name="Contact",
        description=(
            "Extracts a person's contact details from signatures, "
            "introductions, and directory text: name, email, phone, "
            "company, and role. Use when building address books from "
            "conversations."
        ),
        json_schema={
            "type": "object",
            "required": ["name", "email"],
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string", "format": "email"},
                "phone": {"type": "string"},
                "company": {"type": "string"},
                "role": {"type": "string"},
            },
        },
        sample_text=(
            "Hi, I'm Alice Smith, Senior Engineer at Globex. Reach me at "
            "alice@example.com or 555-0142."
        ),
    ),
    SchemaTemplateResponse(
        key="order",
        name="Order",
        description=(
            "Extracts e-commerce order details: order id, customer, line "
            "items with quantities, shipping address, and order total. "
            "Use for purchase confirmations and fulfillment messages."
        ),
        json_schema={
            "type": "object",
            "required": ["order_id", "items", "total"],
            "properties": {
                "order_id": {"type": "string"},
                "customer_name": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["sku", "quantity"],
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "integer", "minimum": 1},
                        },
                    },
                },
                "shipping_address": {"type": "string"},
                "total": {"type": "number"},
            },
        },
        sample_text=(
            "Order ORD-8817 for Bob Jones: 2x SKU-WIDGET-9 and 1x "
            "SKU-GADGET-2, shipping to 12 Main St. Total $89.50."
        ),
    ),
    SchemaTemplateResponse(
        key="meeting_notes",
        name="Meeting notes",
        description=(
            "Extracts meeting outcomes: date, attendees, decisions made, "
            "and action items with owners. Use for standups, reviews, "
            "and planning transcripts."
        ),
        json_schema={
            "type": "object",
            "required": ["date", "attendees", "action_items"],
            "properties": {
                "date": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "action_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["owner", "task"],
                        "properties": {
                            "owner": {"type": "string"},
                            "task": {"type": "string"},
                            "due": {"type": "string"},
                        },
                    },
                },
            },
        },
        sample_text=(
            "Sprint planning on Monday with Priya and Tom. Decided to "
            "ship the search API this sprint. Priya will draft the "
            "migration plan by Friday."
        ),
    ),
    SchemaTemplateResponse(
        key="feedback",
        name="Feedback",
        description=(
            "Extracts product feedback: numeric rating, pros, cons, and a "
            "one-line summary. Use for reviews, surveys, and support "
            "threads to track sentiment drivers."
        ),
        json_schema={
            "type": "object",
            "required": ["rating", "summary"],
            "properties": {
                "product": {"type": "string"},
                "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "pros": {"type": "array", "items": {"type": "string"}},
                "cons": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
        },
        sample_text=(
            "The new dashboard is fast and clean — 4 out of 5. The "
            "export button is hard to find, though."
        ),
    ),
    SchemaTemplateResponse(
        key="receipt",
        name="Receipt",
        description=(
            "Extracts point-of-sale receipt data: merchant, purchase date, "
            "line items, tax, total, and payment method. Use for expense "
            "tracking and reimbursement flows."
        ),
        json_schema={
            "type": "object",
            "required": ["merchant", "purchase_date", "total"],
            "properties": {
                "merchant": {"type": "string"},
                "purchase_date": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
                "subtotal": {"type": "number"},
                "tax": {"type": "number"},
                "total": {"type": "number"},
                "payment_method": {"type": "string"},
            },
        },
        sample_text=(
            "Corner Deli, March 9th: sandwich and coffee, subtotal $12.50, "
            "tax $1.00, total $13.50 paid by card."
        ),
    ),
)
"""Static starter catalogue for the schema builder (no DB backing)."""
