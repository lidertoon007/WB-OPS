from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class ItemType(StrEnum):
    REVIEW = "review"
    QUESTION = "question"

class OrderStatus(StrEnum):
    BUYOUT = "buyout"
    REJECTED = "rejected"
    RETURNED = "returned"
    NOT_SPECIFIED = "notSpecified"


class ComplaintStatus(StrEnum):
    READY_MANUAL = "ready_manual"
    NOT_ELIGIBLE = "not_eligible"
    SUBMITTED_MANUAL = "submitted_manual"
    SUBMITTED_BROWSER = "submitted_browser"
    DISMISSED = "dismissed"


class PublicationMode(StrEnum):
    DRAFT = "draft"
    AUTO = "auto"


class InboxItem(BaseModel):
    id: str
    cabinet_id: str
    item_type: ItemType
    text: str
    rating: int | None = Field(default=None, ge=1, le=5)
    product_id: int | None = None
    order_status: OrderStatus = OrderStatus.NOT_SPECIFIED
    author_name: str | None = None
    marketplace: str = "wildberries"
    product_name: str | None = None
    brand: str | None = None
    has_photo: bool = False
    has_video: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReplyDraft(BaseModel):
    item_id: str
    text: str
    mode: PublicationMode = PublicationMode.DRAFT
    blocked_reason: str | None = None
    confirmed: bool = False
    published: bool = False
    moderation_status: str = "pending"
    moderation_comment: str = ""
    # Как подготовлен черновик: individual (сборщик), ai, templates, knowledge (прошлый ответ); пусто — неизвестно
    source: str = ""


class DraftUpdate(BaseModel):
    text: str


class ModerationUpdate(BaseModel):
    action: str = Field(pattern="^(approve|reject|rework)$")
    comment: str = Field(default="", max_length=1000)


class GroupActionRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(confirm|regenerate|reject|rework)$")


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=2, max_length=5000)
    topic: str = Field(default="general", max_length=50)
    sentiment: str = Field(default="any", pattern="^(any|positive|negative|neutral)$")
    signature: str = Field(default="", max_length=500)
    enabled: bool = True
    kind: str = Field(default="review", pattern="^(review|question)$")
    keywords: list[str] = Field(default_factory=list, max_length=50)
    group: str = Field(default="general", max_length=80)


class QuestionReplySettings(BaseModel):
    enabled: bool = False
    use_keywords: bool = True
    use_ai: bool = True
    require_moderation: bool = True
    address_by_name: bool = True
    signature: str = Field(default="", max_length=500)
    no_answer_action: str = Field(default="manual", pattern="^(manual|ai|ignore)$")


class ChatMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class ChatSyncRequest(BaseModel):
    chats: list[dict] = Field(default_factory=list, max_length=5000)


class AppModuleSettings(BaseModel):
    values: dict[str, object] = Field(default_factory=dict)


class AutoSyncUpdate(BaseModel):
    enabled: bool
    interval_minutes: int = Field(default=15, ge=1, le=1440)


class CabinetCreate(BaseModel):
    """A locally stored WB legal entity/cabinet credential."""

    id: str = Field(default="", max_length=64, pattern=r"^([A-Za-z0-9][A-Za-z0-9_-]+)?$")
    name: str = Field(min_length=1, max_length=160)
    token: str = Field(default="", max_length=4000)
    marketplace: str = Field(default="wildberries", pattern=r"^(wildberries|ozon|yandex)$")
    legal_name: str = Field(default="", max_length=200)
    client_id: str = Field(default="", max_length=100)
    tax_percent: float | None = Field(default=None, ge=0, le=100)
    enabled: bool = True
    country: str = Field(default="RU", pattern=r"^[A-Z]{2}$")
    ads_token: str = Field(default="", max_length=4000)


class AutoReplyRule(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    min_rating: int = Field(default=1, ge=1, le=5)
    max_rating: int = Field(default=5, ge=1, le=5)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    action: str = Field(default="generate", pattern="^(generate|skip)$")
    topics: list[str] = Field(default_factory=list, max_length=20)
    sentiments: list[str] = Field(default_factory=list, max_length=3)
    brands: list[str] = Field(default_factory=list, max_length=30)
    product_ids: list[int] = Field(default_factory=list, max_length=100)
    has_photo: bool | None = None
    has_video: bool | None = None
    min_length: int | None = Field(default=None, ge=0, le=10000)
    max_length: int | None = Field(default=None, ge=0, le=10000)
    response_type: str = Field(default="ai", pattern="^(ai|ai_recommendation|template|manual|ignore)$")
    template_id: str | None = None
    moderation_required: bool = True
    priority: int = Field(default=100, ge=0, le=10000)


class AutoReplySettings(BaseModel):
    enabled: bool = False
    mode: str = Field(default="draft", pattern="^(draft|auto_generate)$")
    min_rating: int = Field(default=1, ge=1, le=5)
    max_rating: int = Field(default=5, ge=1, le=5)
    require_moderation: bool = True
    auto_publish: bool = False
    rules: list[AutoReplyRule] = Field(default_factory=list, max_length=30)


class ReportFile(BaseModel):
    path: str
    filename: str
    report_id: str | None = None
    warehouse: str | None = None
    sha256: str
    size_bytes: int


class ReportRow(BaseModel):
    report_id: str
    warehouse: str
    wb_product_id: str
    article: str
    quantity: int = Field(ge=0)


class ComplaintCase(BaseModel):
    item_id: str
    cabinet_id: str
    rating: int | None
    status: ComplaintStatus
    reason_code: str | None = None
    reason_title: str | None = None
    evidence: str | None = None
    checklist: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ComplaintUpdate(BaseModel):
    status: ComplaintStatus


class ComplaintDetails(ComplaintCase):
    review_text: str = ""
    review_exists: bool = False
    product_id: int | None = None
    order_status: OrderStatus = OrderStatus.NOT_SPECIFIED
    reply_published: bool = False
    complaint_draft: str = ""
