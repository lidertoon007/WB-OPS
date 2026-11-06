import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from urllib.parse import urlsplit

from app.config import get_settings
from app.models import AppModuleSettings, AutoReplySettings, AutoReplyRule, AutoSyncUpdate, CabinetCreate, ChatMessageCreate, ChatSyncRequest, ComplaintCase, ComplaintDetails, ComplaintStatus, ComplaintUpdate, DraftUpdate, GroupActionRequest, InboxItem, ModerationUpdate, OrderStatus, QuestionReplySettings, ReplyDraft, TemplateCreate
from app.services.cabinets import MARKETPLACES as CABINET_MARKETPLACES, Cabinet, dump_cabinets, load_cabinets
from app.services.team import COOKIE as TEAM_COOKIE, COOKIE_DAYS, Team, lan_ip, port_open
from app.services.notifier import Notifier
from app.context import current_actor
from app.services.review_list import ANSWER_STATUSES, ORDER_STATUSES, TAG_ICONS, ReviewList
from app.services.wb_images import WBImages
from app.services.nomenclature import Nomenclature, NomenclatureError
from app.services.dashboard import Dashboard, DashboardError
from app.services import home as home_module
from app.services import abc as abc_module
from app.services.analytics import TAGS as ANALYTICS_TAGS
from app.services.complaints import ComplaintService
from app.services.replies import ReplyService
from app.services.publishing import ReplyPublisher
from app.services.bulk_replies import BulkPreviewRequest, BulkReplySender, BulkSendRequest
from app.services.complaint_browser import ComplaintBrowser, ComplaintPrepareRequest, ComplaintSubmitRequest
from app.services.reports import ReportImporter
from app.services.product_knowledge import ProductKnowledge
from app.services.review_analysis import analyze_review, topic_label
from app.services.recommendations import RecommendationStore
from app.services.telegram import send_telegram_message
from app.services.templates import TemplateStore
from app.services.review_composer import DEFAULT_PHRASES, PHRASE_TITLES, PhraseStore, ReviewComposer, analyze as analyze_review_facts
from app.services import ai_writer
from app.services.product_models import FACT_FIELDS, SIZE_FIELDS, ProductModels
from app.services.question_composer import QuestionComposer, analyze_question
from app.services.chats import ChatStore
from app.services.knowledge_groups import KnowledgeGroupStore
from app.services.wb_client import WBApiError, WBFeedbackClient
from app.services import finance
from app.services import keycheck
from app.services import analytics as analytics_module
from app.services.analytics import Analytics
from app.services.rnp import RNP, RNPError, compose as rnp_compose, heatmap_of as rnp_heatmap_of
from app.services.rnp_ozon import OzonRNP
from app.services import tax as tax_module
from app.services.transfer.common import TransferError
from app.services.repricer import Repricer, RepricerError
from app.services.stocks import Stocks, StocksError
from app.services.fbs_analytics import FbsAnalytics
from app.services.fbs_ozon import FbsOzon
from app.services.penalties import Penalties
from app.services.ads import AdsAnalytics
from app.services.planfact import PlanFact, PlanFactError
from app.services.abtests import ABError, ABTests
from app.services.ab_hypotheses import ABHypothesisError, ABHypotheses, PRIMARY_METRICS
from app.services.transfer.service import TransferService
from pydantic import BaseModel, Field
from app.storage import DraftStore

settings = get_settings()
STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_FILE = STATIC_DIR / "dashboard.html"
DASHBOARD_PAGES = ("workspace", "abc", "products", "cabinets", "knowledge", "analytics", "reviews", "templates", "recommendations", "settings", "ai", "import", "questions", "chats", "messengers", "profile", "finances", "notifications", "team", "complaints", "nomenclature", "dashboard", "dashboard-ozon", "rnp", "transfer", "repricer", "stocks", "fbs-analytics", "penalties", "ads", "planfact", "ab", "hypotheses")
DASHBOARD_PATHS = {"/", *(f"/{page}" for page in DASHBOARD_PAGES)}
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="API для отзывов, вопросов, жалоб и отчётов продавца Wildberries.",
    docs_url=None,
    redoc_url=None,
)
reply_service = ReplyService()
product_knowledge = ProductKnowledge()
chat_store = ChatStore()
knowledge_groups = KnowledgeGroupStore()
template_store = TemplateStore()
recommendation_store = RecommendationStore()
complaint_service = ComplaintService()
draft_store = DraftStore(settings.database_url)
phrase_store = PhraseStore()
reply_service.composer = ReviewComposer(phrase_store)
REVIEW_MODES = ("individual", "ai", "templates")
reply_service.review_mode = draft_store.get_setting("review_mode", "individual")
if reply_service.review_mode not in REVIEW_MODES:
    reply_service.review_mode = "individual"


def ai_config() -> dict:
    values = get_module_settings("ai")["values"]
    return {"provider": draft_store.get_setting("ai_provider", "anthropic"),
            "api_key": draft_store.get_setting("ai_api_key", ""),
            "base_url": draft_store.get_setting("ai_base_url", ""),
            "model": draft_store.get_setting("ai_model", ""),
            "tone": str(values.get("tone") or "").strip(), "length": str(values.get("length") or "").strip(),
            "use_emojis": bool(values.get("use_emojis", False))}


ai_reply_writer = ai_writer.AIWriter(lambda: ai_config())
reply_service.ai = ai_reply_writer
analytics = Analytics(draft_store, chat_store)
team = Team(draft_store)
wb_images = WBImages(draft_store)


def _cabinet_names() -> dict:
    try:
        return {c.id: c.name for c in configured_cabinets()}
    except Exception:
        return {}


review_list = ReviewList(draft_store, cabinet_names=_cabinet_names, product_names=lambda: analytics.product_names())
notifier = Notifier(draft_store)

# Модели товаров (таблица замеров и факты) для ответов на вопросы
product_models = ProductModels()
reply_service.question_composer = QuestionComposer(product_models, products=product_knowledge)
reply_service.question_mode = draft_store.get_setting("question_mode", "individual")
if reply_service.question_mode not in REVIEW_MODES:
    reply_service.question_mode = "individual"


def seed_product_models() -> int:
    """Пустые модели «артикул → WB ID» из прошлых вопросов, чтобы оператору оставалось заполнить замеры."""
    links: dict[str, set] = {}
    for record in getattr(reply_service.knowledge, "records", []) or []:
        vendor, nm = str(record.get("vendor_code") or "").strip(), record.get("nm_id")
        if vendor and nm:
            links.setdefault(vendor, set()).add(int(nm))
    # Связи «WB ID → артикул» из отчёта реализации WB (app/knowledge/nm_vendor.json)
    try:
        extra = json.loads(Path("app/knowledge/nm_vendor.json").read_text(encoding="utf-8")).get("links", {})
    except (OSError, ValueError):
        extra = {}
    for nm, vendor in extra.items():
        if str(nm).isdigit() and str(vendor).strip():
            links.setdefault(str(vendor).strip(), set()).add(int(nm))
    # Только товары из карточек WB по API (app/knowledge/wb_cards.json): старые вопросы и отчёты бывают
    # от чужих или закрытых кабинетов — их модели в «Товарах» не нужны. Нет карточек — ничего не создаём.
    try:
        cards = json.loads(Path("app/knowledge/wb_cards.json").read_text(encoding="utf-8")).get("cards", [])
    except (OSError, ValueError):
        cards = []
    api = {int(c["nm"]) for c in cards if str(c.get("nm") or "").isdigit()}
    links = {vendor: {nm for nm in nms if nm in api} for vendor, nms in links.items()}
    return product_models.seed({vendor: nms for vendor, nms in links.items() if nms})


try:
    seed_product_models()
except Exception:  # справочник — не повод не запускаться
    pass


def configured_cabinets():
    """Load cabinets from local settings, falling back to the .env bootstrap list."""
    raw = draft_store.get_setting("cabinets_json", settings.cabinets_json)
    try:
        return load_cabinets(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return load_cabinets(settings.cabinets_json)


def save_configured_cabinets(cabinets) -> None:
    draft_store.set_setting("cabinets_json", dump_cabinets(cabinets))


manual_send_file = Path("data/manual_send.json")
manual_send_enabled = manual_send_file.exists() and json.loads(manual_send_file.read_text(encoding="utf-8")).get("enabled") is True
# ReplyPublisher historically calls its loader with the env JSON argument;
# keep that interface while resolving the persisted local cabinet list here.
reply_publisher = ReplyPublisher(draft_store, settings, lambda _raw: configured_cabinets(), manual_send_enabled=manual_send_enabled)
bulk_sender = BulkReplySender(draft_store, reply_publisher)
complaint_browser = ComplaintBrowser(store=draft_store)
bulk_sender.recover_interrupted()
auto_sync_enabled = draft_store.get_setting("auto_sync_enabled", str(settings.auto_sync_enabled)).lower() == "true"
auto_sync_interval_minutes = int(draft_store.get_setting("auto_sync_interval_minutes", str(settings.auto_sync_interval_minutes)))
auto_sync_task: asyncio.Task[None] | None = None


def get_auto_reply_settings() -> AutoReplySettings:
    raw = draft_store.get_setting("auto_reply_settings", "")
    if not raw:
        return AutoReplySettings()
    try:
        return AutoReplySettings.model_validate(json.loads(raw))
    except (ValueError, TypeError):
        return AutoReplySettings()


def save_auto_reply_settings(value: AutoReplySettings) -> AutoReplySettings:
    if value.min_rating > value.max_rating:
        raise HTTPException(400, "Минимальный рейтинг не может быть больше максимального")
    for rule in value.rules:
        if rule.min_rating > rule.max_rating:
            raise HTTPException(400, f"В правиле «{rule.name}» минимальный рейтинг выше максимального")
    if value.auto_publish and value.require_moderation:
        raise HTTPException(400, "Для автопубликации отключите обязательную модерацию")
    draft_store.set_setting("auto_reply_settings", json.dumps(value.model_dump(), ensure_ascii=False))
    draft_store.audit("auto_replies_settings_updated", details=f"enabled={value.enabled};mode={value.mode};auto_publish={value.auto_publish}")
    return value


def auto_reply_matches(item: InboxItem, settings_value: AutoReplySettings) -> tuple[bool, str]:
    if item.item_type.value != "review" or item.rating is None:
        return False, "только отзывы с рейтингом"
    if not settings_value.enabled:
        return False, "автоответы выключены"
    if not settings_value.min_rating <= item.rating <= settings_value.max_rating:
        return False, "рейтинг вне диапазона"
    if not settings_value.rules:
        return True, "общие правила"
    text = item.text.lower()
    markup = analyze_review(item.text, item.rating)
    matched = False
    for rule in settings_value.rules:
        if not rule.enabled or not rule.min_rating <= item.rating <= rule.max_rating:
            continue
        keywords = [word.strip().lower() for word in rule.keywords if word.strip()]
        if keywords and not any(word in text for word in keywords):
            continue
        if rule.topics and not set(rule.topics).intersection(markup.topics):
            continue
        if rule.sentiments and markup.sentiment not in rule.sentiments:
            continue
        if rule.brands and (item.brand or "") not in rule.brands:
            continue
        if rule.product_ids and (item.product_id or 0) not in rule.product_ids:
            continue
        if rule.has_photo is not None and rule.has_photo != item.has_photo:
            continue
        if rule.has_video is not None and rule.has_video != item.has_video:
            continue
        if rule.min_length is not None and len(item.text) < rule.min_length:
            continue
        if rule.max_length is not None and len(item.text) > rule.max_length:
            continue
        matched = True
        if rule.action == "skip":
            return False, f"правило «{rule.name}» исключает отзыв"
    return (matched, "подошло правило" if matched else "нет совпавшего правила")


def selected_auto_rule(item: InboxItem, settings_value: AutoReplySettings) -> AutoReplyRule | None:
    markup = analyze_review(item.text, item.rating)
    text = item.text.lower()
    for rule in sorted(settings_value.rules, key=lambda value: value.priority):
        if not rule.enabled or item.rating is None or not rule.min_rating <= item.rating <= rule.max_rating:
            continue
        keywords = [word.strip().lower() for word in rule.keywords if word.strip()]
        if keywords and not any(word in text for word in keywords):
            continue
        if rule.topics and not set(rule.topics).intersection(markup.topics):
            continue
        if rule.sentiments and markup.sentiment not in rule.sentiments:
            continue
        if rule.product_ids and (item.product_id or 0) not in rule.product_ids:
            continue
        if rule.has_photo is not None and rule.has_photo != item.has_photo:
            continue
        if rule.has_video is not None and rule.has_video != item.has_video:
            continue
        if rule.min_length is not None and len(item.text) < rule.min_length:
            continue
        if rule.max_length is not None and len(item.text) > rule.max_length:
            continue
        return rule
    return None


def process_auto_review(item: InboxItem) -> dict[str, object]:
    settings_value = get_auto_reply_settings()
    matched, reason = auto_reply_matches(item, settings_value)
    if not matched:
        return {"item_id": item.id, "status": "skipped", "reason": reason}
    rule = selected_auto_rule(item, settings_value)
    if rule and rule.action == "skip" or rule and rule.response_type == "ignore":
        return {"item_id": item.id, "status": "skipped", "reason": f"правило «{rule.name}» игнорирует отзыв"}
    generated = reply_service.draft(item)
    ai_values = get_module_settings("ai")["values"]
    if isinstance(ai_values, dict):
        signature = str(ai_values.get("signature") or "").strip()
        if signature and signature.lower() not in generated.text.lower():
            generated.text = f"{generated.text.rstrip()}\n\n{signature}"
        if not bool(ai_values.get("use_emojis", False)):
            generated.text = generated.text.replace(" 😊", "").replace(" 🙂", "")
    moderation_values = get_module_settings("moderation")["values"]
    stop_words = [str(value).lower() for value in moderation_values.get("stop_words", [])] if isinstance(moderation_values, dict) else []
    if stop_words and any(value in item.text.lower() for value in stop_words):
        generated.blocked_reason = "В отзыве обнаружено стоп-слово"
    if rule and rule.response_type == "template" and rule.template_id:
        custom = next((value for value in template_store.custom_all() if value.get("id") == rule.template_id), None)
        if custom:
            variables = {"name": item.author_name or "", "rating": item.rating or "", "product_id": item.product_id or "", "product_name": item.product_name or "", "text": item.text}
            generated.text = template_store.render(str(custom.get("text", generated.text)), variables)
            generated.source = "templates"
            custom_signature = str(custom.get("signature") or "").strip()
            if custom_signature:
                generated.text = f"{generated.text.rstrip()}\n\n{template_store.render(custom_signature, variables)}"
    draft = draft_store.save_generated(generated)
    warnings = reply_service.validate(draft.text, item)
    if generated.blocked_reason:
        warnings.append(generated.blocked_reason)
    status = "moderation" if warnings or settings_value.require_moderation else "ready"
    draft_store.audit("auto_reply_generated", item.id, f"status={status};warnings={len(warnings)}")
    return {"item_id": item.id, "status": status, "warnings": warnings, "text": draft.text}


def get_question_settings() -> QuestionReplySettings:
    values = get_module_settings("questions")["values"]
    try:
        return QuestionReplySettings.model_validate(values)
    except (ValueError, TypeError):
        return QuestionReplySettings()


def process_auto_question(item: InboxItem) -> dict[str, object]:
    settings_value = get_question_settings()
    if not settings_value.enabled:
        return {"item_id": item.id, "status": "skipped", "reason": "автоответы на вопросы выключены"}
    generated = reply_service.draft(item)
    templates = [value for value in template_store.custom_all() if value.get("kind", "review") == "question" and value.get("enabled", True)]
    text_lower = item.text.lower()
    selected = next((value for value in templates if any(str(word).strip().lower() in text_lower for word in value.get("keywords", []) if str(word).strip())), None) if settings_value.use_keywords else None
    if selected is None and settings_value.no_answer_action == "ignore":
        draft_store.audit("auto_question_skipped", item.id, "no_matching_template")
        return {"item_id": item.id, "status": "skipped", "reason": "нет подходящего шаблона"}
    if selected:
        variables = {"name": item.author_name or "", "rating": "", "product_id": item.product_id or "", "product_name": item.product_name or "", "text": item.text}
        generated.text = template_store.render(str(selected.get("text", generated.text)), variables)
        generated.source = "templates"
        if selected.get("signature"):
            generated.text = f"{generated.text.rstrip()}\n\n{template_store.render(str(selected['signature']), variables)}"
    if settings_value.signature:
        generated.text = f"{generated.text.rstrip()}\n\n{settings_value.signature.strip()}"
    draft = draft_store.save_generated(generated)
    warnings = reply_service.validate(draft.text, item)
    status = "moderation" if warnings or settings_value.require_moderation else "ready"
    draft_store.audit("auto_question_generated", item.id, f"status={status};template={selected.get('name') if selected else 'knowledge'}")
    return {"item_id": item.id, "status": status, "warnings": warnings, "text": draft.text}


LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")
# Только владельцу (на компьютере с WB OPS): команда, токены, ключи, финансы, резервные копии
OWNER_ONLY = ("/api/team", "/api/admin", "/api/notifications", "/api/finance", "/api/home", "/api/abc", "/api/nomenclature", "/api/dashboard", "/api/rnp", "/api/transfer", "/api/repricer", "/api/penalties", "/api/ads", "/api/planfact", "/api/ab", "/api/settings/modules/telegram", "/docs", "/openapi.json",
              "/api/complaint-browser")
OWNER_ONLY_WRITE = ("/api/cabinets", "/api/ai/settings", "/api/account", "/api/settings/auto-sync")
MEMBER_OPEN = ("/api/team/me", "/api/team/logout")


def is_owner_only(path: str, method: str) -> bool:
    if path.startswith(MEMBER_OPEN):
        return False
    if path.startswith(OWNER_ONLY) or path in ("/finances", "/nomenclature", "/dashboard", "/dashboard-ozon", "/abc", "/rnp", "/transfer", "/repricer", "/penalties", "/ads", "/planfact", "/ab"):
        return True
    return method not in ("GET", "HEAD") and path.startswith(OWNER_ONLY_WRITE)


@app.middleware("http")
async def guard_complaint_browser_origin(request: Request, call_next):
    path = request.url.path
    local = request.client is None or request.client.host in LOCAL_HOSTS
    member = None
    if not local and not settings.allow_remote:
        member = team.by_token(request.cookies.get(TEAM_COOKIE))
        if member is None:
            if path not in ("/join", "/api/team/join"):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Нужен вход: откройте ссылку-приглашение от владельца панели"}, status_code=401)
                return RedirectResponse("/join", status_code=303)
        elif is_owner_only(path, request.method):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Доступно только владельцу панели"}, status_code=403)
            return RedirectResponse("/", status_code=303)
    request.state.member = member
    if request.method == "POST" and path.startswith("/api/complaint-browser/"):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != request.url.port:
                return JSONResponse({"detail": "Операция доступна только из локальной панели сервиса"}, status_code=403)
    actor = current_actor.set(member["name"] if member else "")
    try:
        return await call_next(request)
    finally:
        current_actor.reset(actor)


@app.middleware("http")
async def inject_knowledge_workspace(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").split(";")[0] != "text/html":
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    html = body.decode("utf-8")
    if request.url.path in DASHBOARD_PATHS:
        scripts = "".join(f"<script>{(STATIC_DIR / name).read_text(encoding='utf-8')}</script>" for name in ("finance.js", "replies.js", "questions.js", "analytics.js", "settings.js", "reviews.js", "complaints.js", "products.js", "dashboard.js", "rnp.js", "transfer.js", "repricer.js", "stocks.js", "fbs.js", "fbs_ozon.js", "penalties.js", "ads.js", "planfact.js", "ab.js", "hypotheses.js", "home.js", "abc.js", "router.js", "knowledge.js"))
        html = html.replace("</body>", scripts + "</body>")
    return Response(content=html, status_code=response.status_code, headers={"content-type": "text/html; charset=utf-8"})


async def perform_sync(cabinet_id: str) -> dict[str, object]:
    cabinets = {cabinet.id: cabinet for cabinet in configured_cabinets()}
    cabinet = cabinets.get(cabinet_id)
    if cabinet is None:
        raise WBApiError("Кабинет не найден")
    if not cabinet.enabled:
        raise WBApiError("Кабинет выключен — включите его в настройках кабинета")
    if cabinet.marketplace != "wildberries":
        raise WBApiError(f"Загрузка из {CABINET_MARKETPLACES.get(cabinet.marketplace, cabinet.marketplace)} пока не поддерживается")
    if not (cabinet.reviews or cabinet.questions):
        raise WBApiError("В кабинете выключены и отзывы, и вопросы")
    items = await WBFeedbackClient(cabinet.token, settings.feedbacks_api_base_url).fetch_unanswered()
    items = [i for i in items if (cabinet.reviews if i.item_type.value == "review" else cabinet.questions)]
    auto_settings = get_auto_reply_settings()
    for item in items:
        item.cabinet_id = cabinet.id
        if auto_settings.enabled and item.item_type.value == "review":
            process_auto_review(item)
        elif item.item_type.value == "question" and get_question_settings().enabled:
            process_auto_question(item)
        else:
            draft_store.save_generated(reply_service.draft(item))
        complaint = complaint_service.evaluate(item)
        if complaint:
            complaint = draft_store.save_complaint(complaint)
            event = "complaint_ready" if complaint.status == ComplaintStatus.READY_MANUAL else "complaint_not_eligible" if complaint.status == ComplaintStatus.NOT_ELIGIBLE else "complaint_manual_status_preserved"
            draft_store.audit(event, item.id, complaint.reason_code or "no_clear_violation")
    draft_store.save_inbox(items)
    from datetime import datetime, timezone
    draft_store.set_setting("sync_last_at", datetime.now(timezone.utc).isoformat())
    draft_store.set_setting("sync_last_received", str(len(items)))
    draft_store.set_setting("sync_last_error", "")
    draft_store.audit("sync", cabinet_id, f"received={len(items)}")
    if items:
        try:
            await notifier.new_items(cabinet, items)
        except Exception as exc:  # уведомление не должно ломать синхронизацию
            draft_store.audit("notify_error", cabinet.id, str(exc)[:300])
    return {"cabinet_id": cabinet.id, "received": len(items), "drafts_created": len(items), "published": 0}


async def auto_sync_loop() -> None:
    global auto_sync_enabled
    while True:
        if auto_sync_enabled:
            for cabinet in configured_cabinets():
                if not cabinet.enabled or cabinet.marketplace != "wildberries" or not cabinet.token or not (cabinet.reviews or cabinet.questions):
                    continue
                try:
                    await perform_sync(cabinet.id)
                except Exception as exc:
                    # Errors remain isolated to this cycle; manual sync exposes details.
                    draft_store.set_setting("sync_last_error", str(exc)[:500])
                    draft_store.audit("sync_error", cabinet.id, str(exc)[:500])
                    try:
                        await notifier.sync_error(cabinet, str(exc))
                    except Exception:
                        pass
                    continue
        await asyncio.sleep(auto_sync_interval_minutes * 60)


@app.on_event("startup")
async def start_scheduler() -> None:
    global auto_sync_task, notify_task
    auto_sync_task = asyncio.create_task(auto_sync_loop())
    notify_task = asyncio.create_task(notify_loop())
    asyncio.create_task(rnp_loop())
    asyncio.create_task(stocks_loop())
    asyncio.create_task(fbs_loop())
    asyncio.create_task(penalties_loop())
    asyncio.create_task(ads_loop())
    asyncio.create_task(planfact_loop())
    asyncio.create_task(ab_loop())
    try:
        cabinets = configured_cabinets()
        for cabinet in cabinets:
            _apply_tax(cabinet, cabinets)        # ставки кабинетов → расчёт ОПиУ (если кабинет там появился)
    except Exception as exc:
        draft_store.audit("cabinet_tax_error", details=str(exc)[:300])
    try:
        reevaluate_complaints()
    except Exception as exc:
        draft_store.audit("complaints_reevaluate_error", details=str(exc)[:300])


notify_task = None


async def notify_loop() -> None:
    """Раз в минуту: сводка отправленных ответов, черновиков на проверке и чатов."""
    notifier.init_cursor()
    while True:
        await asyncio.sleep(60)
        try:
            await notifier.scan_journal()
        except Exception as exc:
            draft_store.audit("notify_error", details=str(exc)[:300])


@app.on_event("shutdown")
async def stop_scheduler() -> None:
    if auto_sync_task:
        auto_sync_task.cancel()
    if notify_task:
        notify_task.cancel()
    finance_runner.stop()
    tasks = list(bulk_sender.tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await complaint_browser.close()




@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_FILE.read_text(encoding="utf-8"))


for _page in DASHBOARD_PAGES:
    app.add_api_route(f"/{_page}", dashboard, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def api_docs() -> HTMLResponse:
    swagger = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="WB Operations · API",
        swagger_ui_parameters={
            "deepLinking": True,
            "displayRequestDuration": True,
            "docExpansion": "none",
            "filter": True,
            "persistAuthorization": True,
            "tryItOutEnabled": True,
            "defaultModelsExpandDepth": -1,
            "operationsSorter": "method",
            "tagsSorter": "alpha",
            "syntaxHighlight.theme": "monokai",
        },
    )
    html = swagger.body.decode("utf-8")
    styles = """
    <style>
      :root{--ink:#1f2937;--muted:#6b7280;--line:#e7e5ef;--purple:#6d28d9;--pink:#db2777;--soft:#f7f5fb}
      *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0!important;background:linear-gradient(180deg,#f5f3ff 0,#fafafa 280px,#f6f7fb 100%);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
      .topbar{display:none}.wb-docs-head{padding:28px 24px 22px;background:linear-gradient(135deg,#4c1d95 0%,#7c3aed 55%,#db2777 125%);color:#fff;box-shadow:0 12px 35px #4c1d9533}
      .wb-docs-head__inner{max-width:1180px;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:24px}.wb-docs-brand{display:flex;align-items:center;gap:14px}.wb-docs-logo{width:48px;height:48px;border-radius:15px;background:#ffffff1f;border:1px solid #ffffff38;display:grid;place-items:center;font-weight:900;letter-spacing:-.04em;box-shadow:inset 0 1px #ffffff44}.wb-docs-kicker{font-size:12px;font-weight:800;letter-spacing:.13em;text-transform:uppercase;opacity:.76}.wb-docs-title{margin:2px 0 4px;font-size:25px;line-height:1.15}.wb-docs-subtitle{font-size:14px;opacity:.82}.wb-docs-back{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:11px;background:#fff;color:#5b21b6;text-decoration:none;font-size:14px;font-weight:800;box-shadow:0 5px 15px #2e106533}.wb-docs-back:hover{transform:translateY(-1px)}
      .wb-docs-guide{max-width:1180px;margin:20px auto 2px;padding:0 24px}.wb-docs-guide__card{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;overflow:hidden;border:1px solid var(--line);border-radius:15px;background:var(--line);box-shadow:0 10px 28px #1118270b}.wb-docs-guide__item{padding:16px 18px;background:#fff}.wb-docs-guide__item strong{display:block;margin-bottom:3px;color:#4c1d95;font-size:13px}.wb-docs-guide__item span{color:var(--muted);font-size:13px;line-height:1.45}
      #swagger-ui{max-width:1230px;margin:0 auto;padding:8px 24px 60px}.swagger-ui{font-family:inherit;color:var(--ink)}.swagger-ui .wrapper{max-width:none;padding:0}.swagger-ui .information-container{margin:18px 0}.swagger-ui .info{margin:0;padding:22px;border:1px solid var(--line);border-radius:16px;background:#fff;box-shadow:0 10px 28px #1118270b}.swagger-ui .info .title{color:#24153f;font-size:26px;letter-spacing:-.025em}.swagger-ui .info .title small{top:-3px;background:#ede9fe;color:#5b21b6;border-radius:999px;padding:5px 9px}.swagger-ui .info p,.swagger-ui .info li{color:var(--muted);font-size:14px}.swagger-ui .scheme-container{margin:14px 0 20px;padding:15px 18px;border:1px solid var(--line);border-radius:14px;background:#fff;box-shadow:none}.swagger-ui .auth-wrapper .authorize{border-color:var(--purple);color:var(--purple);border-radius:9px}.swagger-ui .auth-wrapper .authorize svg{fill:var(--purple)}
      .swagger-ui .filter-container{margin:0 0 18px}.swagger-ui .filter-container input{width:100%;max-width:none;padding:12px 14px;border:1px solid #d8d4e3;border-radius:11px;background:#fff;font-size:14px;box-shadow:0 5px 16px #11182708}.swagger-ui .filter-container input:focus{border-color:#8b5cf6;outline:3px solid #ddd6fe}.swagger-ui .opblock-tag{margin:18px 0 10px;padding:15px 17px;border:1px solid var(--line);border-radius:13px;background:#fff;font-size:19px;color:#2f1b4e;box-shadow:0 7px 20px #11182708}.swagger-ui .opblock-tag:hover{background:#faf7ff}.swagger-ui .opblock{margin:0 0 10px;border-radius:12px;overflow:hidden;box-shadow:0 5px 16px #11182708}.swagger-ui .opblock .opblock-summary{padding:10px}.swagger-ui .opblock .opblock-summary-method{min-width:78px;border-radius:8px;text-shadow:none}.swagger-ui .opblock .opblock-summary-path{font-size:14px}.swagger-ui .opblock .opblock-summary-description{color:#5f6470;font-size:13px}.swagger-ui .opblock.opblock-get{border-color:#60a5fa;background:#eff6ff}.swagger-ui .opblock.opblock-post{border-color:#34d399;background:#ecfdf5}.swagger-ui .opblock.opblock-put{border-color:#fbbf24;background:#fffbeb}.swagger-ui .opblock.opblock-delete{border-color:#fb7185;background:#fff1f2}.swagger-ui .opblock-body{background:#fff}.swagger-ui .btn{border-radius:9px}.swagger-ui .btn.execute{border-color:var(--purple);background:var(--purple);color:#fff}.swagger-ui input[type=text],.swagger-ui textarea,.swagger-ui select{border:1px solid #d8d4e3;border-radius:8px;background:#fff}.swagger-ui .model-box{border-radius:10px;background:#f8f7fb}.swagger-ui section.models{border:1px solid var(--line);border-radius:13px;background:#fff}.swagger-ui .responses-inner{padding:18px}.swagger-ui table tbody tr td{padding:12px 10px}.swagger-ui .copy-to-clipboard{border-radius:7px}.swagger-ui .download-contents{border-radius:8px}.swagger-ui .loading-container .loading:after{color:var(--purple)}
      @media(max-width:760px){.wb-docs-head{padding:22px 18px}.wb-docs-head__inner{align-items:flex-start;flex-direction:column}.wb-docs-title{font-size:21px}.wb-docs-guide{padding:0 14px}.wb-docs-guide__card{grid-template-columns:1fr}.wb-docs-guide__item{padding:13px 15px}#swagger-ui{padding:6px 12px 40px}.swagger-ui .info{padding:17px}.swagger-ui .opblock .opblock-summary{align-items:flex-start;flex-wrap:wrap}.swagger-ui .opblock .opblock-summary-path{max-width:100%;word-break:break-word}.swagger-ui .opblock .opblock-summary-description{width:100%;padding:5px 0 0 88px}}
    </style>
    """
    header = """
    <header class="wb-docs-head"><div class="wb-docs-head__inner">
      <div class="wb-docs-brand"><div class="wb-docs-logo">WB</div><div><div class="wb-docs-kicker">Operations API</div><h1 class="wb-docs-title">Документация сервиса</h1><div class="wb-docs-subtitle">Отзывы, вопросы, жалобы и отчёты Wildberries</div></div></div>
      <a class="wb-docs-back" href="/">← Вернуться в панель</a>
    </div></header>
    <section class="wb-docs-guide"><div class="wb-docs-guide__card">
      <div class="wb-docs-guide__item"><strong>1. Найдите метод</strong><span>Используйте строку Filter или раскройте нужный раздел API.</span></div>
      <div class="wb-docs-guide__item"><strong>2. Заполните параметры</strong><span>Нажмите Try it out и проверьте данные перед выполнением.</span></div>
      <div class="wb-docs-guide__item"><strong>3. Выполните запрос</strong><span>Execute покажет код ответа, тело и время выполнения.</span></div>
    </div></section>
    """
    html = html.replace("</head>", styles + "</head>").replace("<body>", "<body>" + header)
    return HTMLResponse(html)


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "environment": settings.environment, "dry_run": settings.dry_run, "manual_send_enabled": manual_send_enabled}


def _tax_project():
    try:
        return finance_project()
    except HTTPException:
        return None


def _cabinet_public(cabinet, cabinets=None, project=...) -> dict[str, object]:
    cabinets = cabinets if cabinets is not None else configured_cabinets()
    project = _tax_project() if project is ... else project
    same = sum(1 for c in cabinets if c.marketplace == cabinet.marketplace)
    opiu = tax_module.opiu_rate(project, cabinet, same)
    data = cabinet.public()
    data.update({"tax_effective": cabinet.tax_percent if cabinet.tax_percent is not None else opiu["rate"],
                 "tax_opiu_rate": opiu["rate"], "tax_opiu_account": opiu["account"], "tax_opiu_found": project is not None,
                 "key_check": keycheck.present(_stored_key_check(cabinet.id), cabinet)})
    return data


def _stored_key_check(cabinet_id: str) -> dict | None:
    try:
        return json.loads(draft_store.get_setting(f"key_check:{cabinet_id}", "") or "null")
    except ValueError:
        return None


def _apply_tax(cabinet, cabinets) -> None:
    """Перенести ставку кабинета в конфиг ОПиУ (если кабинет там есть)."""
    if cabinet.tax_percent is None:
        return
    try:
        same = sum(1 for c in cabinets if c.marketplace == cabinet.marketplace)
        account = tax_module.write_opiu_rate(_tax_project(), cabinet, same, cabinet.tax_percent)
        if account is not None:
            draft_store.audit("cabinet_tax_applied", cabinet.id, f"opiu={account};rate={cabinet.tax_percent}")
    except OSError as exc:
        draft_store.audit("cabinet_tax_error", cabinet.id, str(exc)[:300])


@app.get("/api/cabinets")
def list_cabinets() -> list[dict[str, object]]:
    cabinets, project = configured_cabinets(), _tax_project()
    return [_cabinet_public(cabinet, cabinets, project) for cabinet in cabinets]


def _new_cabinet_id(marketplace: str, existing: set[str]) -> str:
    import secrets as _secrets
    prefix = {"wildberries": "wb", "ozon": "ozon", "yandex": "ym"}.get(marketplace, "mp")
    while True:
        candidate = f"{prefix}-{_secrets.token_hex(3)}"
        if candidate not in existing:
            return candidate


@app.post("/api/cabinets")
def create_cabinet(payload: CabinetCreate) -> dict[str, object]:
    cabinets = configured_cabinets()
    ids = {cabinet.id for cabinet in cabinets}
    cabinet_id = payload.id or _new_cabinet_id(payload.marketplace, ids)
    if cabinet_id in ids:
        raise HTTPException(409, "Кабинет с таким ID уже существует")
    if payload.marketplace == "wildberries" and not payload.token.strip():
        raise HTTPException(400, "Вставьте API-токен кабинета Wildberries")
    cabinet = Cabinet(id=cabinet_id, name=payload.name.strip(), token=payload.token.strip(), marketplace=payload.marketplace,
                      legal_name=payload.legal_name.strip(), client_id=payload.client_id.strip(),
                      chats=False, tax_percent=payload.tax_percent, enabled=payload.enabled, country=payload.country,
                      ads_token=payload.ads_token.strip())
    cabinets.append(cabinet)
    save_configured_cabinets(cabinets)
    draft_store.audit("cabinet_created", cabinet_id, f"name={cabinet.name};marketplace={cabinet.marketplace};tax={cabinet.tax_percent}")
    _apply_tax(cabinet, cabinets)
    return _cabinet_public(cabinet, cabinets)


def _find_cabinet(cabinet_id: str):
    cabinets = configured_cabinets()
    current = next((cabinet for cabinet in cabinets if cabinet.id == cabinet_id), None)
    if current is None:
        raise HTTPException(404, "Кабинет не найден")
    return cabinets, current


@app.put("/api/cabinets/{cabinet_id}")
def update_cabinet(cabinet_id: str, payload: CabinetCreate) -> dict[str, object]:
    if payload.id and payload.id != cabinet_id:
        raise HTTPException(400, "ID кабинета в адресе и теле запроса должны совпадать")
    cabinets, current = _find_cabinet(cabinet_id)
    updated = current.with_changes(name=payload.name.strip(), token=payload.token.strip() or current.token,
                                   legal_name=payload.legal_name.strip() if payload.legal_name or "legal_name" in payload.model_fields_set else current.legal_name,
                                   client_id=payload.client_id.strip() or current.client_id,
                                   tax_percent=payload.tax_percent if "tax_percent" in payload.model_fields_set else current.tax_percent,
                                   enabled=payload.enabled if "enabled" in payload.model_fields_set else current.enabled,
                                   country=payload.country if "country" in payload.model_fields_set else current.country,
                                   ads_token=payload.ads_token.strip() or current.ads_token)
    cabinets[cabinets.index(current)] = updated
    save_configured_cabinets(cabinets)
    draft_store.audit("cabinet_updated", cabinet_id, f"name={updated.name};token_changed={bool(payload.token.strip())};"
                      f"ads_token_changed={bool(payload.ads_token.strip())};enabled={int(updated.enabled)};country={updated.country};tax={updated.tax_percent}")
    if updated.tax_percent is not None and updated.tax_percent != current.tax_percent:
        _apply_tax(updated, cabinets)
    elif updated.tax_percent is None and current.tax_percent is not None:
        try:      # ставку очистили — в ОПиУ снова общая ставка файла
            tax_module.write_opiu_rate(_tax_project(), updated, sum(1 for c in cabinets if c.marketplace == updated.marketplace), None)
        except OSError:
            pass
    return _cabinet_public(updated, cabinets)


@app.patch("/api/cabinets/{cabinet_id}/channels")
def update_cabinet_channels(cabinet_id: str, value: dict) -> dict[str, object]:
    cabinets, current = _find_cabinet(cabinet_id)
    changes = {k: bool(value[k]) for k in ("reviews", "questions", "chats", "stats") if k in value}
    if not changes:
        raise HTTPException(400, "Укажите reviews, questions, chats или stats")
    updated = current.with_changes(**changes)
    cabinets[cabinets.index(current)] = updated
    save_configured_cabinets(cabinets)
    draft_store.audit("cabinet_channels_updated", cabinet_id, ";".join(f"{k}={int(v)}" for k, v in changes.items()))
    return _cabinet_public(updated, cabinets)


@app.post("/api/cabinets/{cabinet_id}/check-key")
async def check_cabinet_key(cabinet_id: str) -> dict[str, object]:
    """Проверить ключ API кабинета у маркетплейса; результат хранится до замены ключа."""
    cabinets, current = _find_cabinet(cabinet_id)
    result = await keycheck.check(current)
    draft_store.set_setting(f"key_check:{cabinet_id}", json.dumps(result, ensure_ascii=False))
    draft_store.audit("cabinet_key_checked", cabinet_id, result.get("status", ""))
    return await asyncio.to_thread(_cabinet_public, current, cabinets)


@app.delete("/api/cabinets/{cabinet_id}")
def delete_cabinet(cabinet_id: str) -> dict[str, object]:
    cabinets, current = _find_cabinet(cabinet_id)
    cabinets.remove(current)
    save_configured_cabinets(cabinets)
    draft_store.audit("cabinet_deleted", cabinet_id, f"name={current.name}")
    return {"deleted": cabinet_id}


# ---------------------------------------------------------------- учётная запись и команда
def owner_label() -> str:
    return draft_store.get_setting("owner_email", "") or draft_store.get_setting("owner_name", "") or "Владелец"


@app.get("/api/account")
def get_account(request: Request) -> dict[str, object]:
    member = getattr(request.state, "member", None)
    if member:
        return {"role": "member", "name": member["name"], "email": member["email"]}
    return {"role": "owner", "name": draft_store.get_setting("owner_name", ""), "email": draft_store.get_setting("owner_email", "")}


@app.put("/api/account")
def update_account(value: dict) -> dict[str, object]:
    name = " ".join(str(value.get("name") or "").split())[:80]
    email = str(value.get("email") or "").strip()[:120]
    if email and "@" not in email:
        raise HTTPException(400, "Проверьте email")
    draft_store.set_setting("owner_name", name)
    draft_store.set_setting("owner_email", email)
    draft_store.audit("account_updated")
    return {"role": "owner", "name": name, "email": email}


def team_network(request: Request) -> dict[str, object]:
    ip = lan_ip()
    port = request.url.port or 8000
    reachable = ip != "127.0.0.1" and port_open(ip, port)
    return {"lan_ip": ip, "port": port, "reachable": reachable, "base_url": f"http://{ip}:{port}"}


@app.get("/api/team/me")
def team_me(request: Request) -> dict[str, object]:
    member = getattr(request.state, "member", None)
    return {"role": "member", "member": member} if member else {"role": "owner", "name": owner_label()}


@app.get("/api/team")
def team_overview(request: Request) -> dict[str, object]:
    return {"invites": team.invites(), "members": team.members(), "network": team_network(request)}


@app.post("/api/team/invites")
def team_create_invite(request: Request, value: dict | None = None) -> dict[str, object]:
    invite = team.create_invite(owner_label(), int((value or {}).get("days") or 7))
    network = team_network(request)
    draft_store.audit("team_invite_created", details=f"expires={invite['expires_at'][:10]}")
    return {**invite, "link": f"{network['base_url']}/join?code={invite['code']}", "network": network}


@app.delete("/api/team/invites/{code}")
def team_delete_invite(code: str) -> dict[str, object]:
    if not team.delete_invite(code):
        raise HTTPException(404, "Приглашение не найдено")
    draft_store.audit("team_invite_deleted")
    return {"deleted": code}


@app.put("/api/team/members/{member_id}")
def team_update_member(member_id: int, value: dict) -> dict[str, object]:
    try:
        member = team.set_active(member_id, bool(value.get("active")))
    except KeyError:
        raise HTTPException(404, "Сотрудник не найден")
    draft_store.audit("team_member_" + ("enabled" if member["active"] else "disabled"), details=member["name"])
    return member


@app.delete("/api/team/members/{member_id}")
def team_delete_member(member_id: int) -> dict[str, object]:
    member = team.member(member_id)
    if not member or not team.delete_member(member_id):
        raise HTTPException(404, "Сотрудник не найден")
    draft_store.audit("team_member_deleted", details=member["name"])
    return {"deleted": member_id}


@app.post("/api/team/join")
def team_join(value: dict) -> JSONResponse:
    try:
        member, token = team.join(value.get("code"), value.get("name"), value.get("email", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("team_member_joined", details=member["name"])
    response = JSONResponse({"member": member})
    response.set_cookie(TEAM_COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax")
    return response


@app.post("/api/team/logout")
def team_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(TEAM_COOKIE)
    return response


@app.get("/join", response_class=HTMLResponse)
def join_page(code: str = "") -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "join.html").read_text(encoding="utf-8").replace("{{code}}", re.sub(r"[^0-9a-fA-F-]", "", code)[:36]))


# ---------------------------------------------------------------- уведомления
@app.get("/api/notifications")
def get_notifications() -> dict[str, object]:
    return {**notifier.public(), "cabinet_list": [{"id": c.id, "name": c.name, "marketplace": c.marketplace} for c in configured_cabinets()]}


@app.put("/api/notifications")
def update_notifications(value: dict) -> dict[str, object]:
    try:
        saved = notifier.save(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("notifications_updated")
    return {**saved, "cabinet_list": [{"id": c.id, "name": c.name, "marketplace": c.marketplace} for c in configured_cabinets()]}


@app.post("/api/notifications/test")
async def test_notifications(value: dict) -> dict[str, object]:
    channel = value.get("channel")
    if channel not in ("telegram", "email"):
        raise HTTPException(400, "channel: telegram или email")
    try:
        return await notifier.test(channel)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/notifications/telegram/chat")
async def detect_telegram_chat(value: dict | None = None) -> dict[str, object]:
    try:
        return await notifier.detect_chat((value or {}).get("bot_token"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Telegram недоступен: {type(exc).__name__}")


@app.post("/api/sync/{cabinet_id}")
async def sync_cabinet(cabinet_id: str) -> dict[str, object]:
    try:
        return await perform_sync(cabinet_id)
    except WBApiError as exc:
        draft_store.set_setting("sync_last_error", str(exc)[:500])
        status = 404 if "не найден" in str(exc) else 400 if ("не поддерживается" in str(exc) or "выключены" in str(exc)) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/api/import/reviews")
async def import_reviews(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект настроек импорта")
    cabinet_ids = payload.get("cabinet_ids") or [cabinet.id for cabinet in configured_cabinets() if cabinet.enabled]
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    with_markup = bool(payload.get("with_markup", True))
    imported = 0
    results = []
    for cabinet_id in cabinet_ids:
        cabinets = {cabinet.id: cabinet for cabinet in configured_cabinets()}
        cabinet = cabinets.get(str(cabinet_id))
        if cabinet is None:
            results.append({"cabinet_id": cabinet_id, "status": "error", "reason": "Кабинет не найден"})
            continue
        try:
            items = await WBFeedbackClient(cabinet.token, settings.feedbacks_api_base_url).fetch_history(date_from, date_to, with_answered=True)
            for item in items:
                item.cabinet_id = cabinet.id
            imported += draft_store.save_inbox(items)
            for item in items:
                draft_store.save_generated(reply_service.draft(item))
                if with_markup and item.item_type.value == "review":
                    markup = analyze_review(item.text, item.rating)
                    draft_store.audit("review_marked", item.id, f"topics={','.join(markup.topics)};sentiment={markup.sentiment}")
            results.append({"cabinet_id": cabinet.id, "status": "ok", "count": len(items)})
        except WBApiError as exc:
            results.append({"cabinet_id": cabinet.id, "status": "error", "reason": str(exc)})
    draft_store.audit("reviews_imported", details=f"count={imported};from={date_from};to={date_to}")
    return {"imported": imported, "results": results, "with_markup": with_markup}


@app.get("/api/settings/auto-sync")
def get_auto_sync() -> dict[str, object]:
    return {"enabled": auto_sync_enabled, "interval_minutes": auto_sync_interval_minutes}


@app.get("/api/sync-status")
def sync_status() -> dict[str, object]:
    return {
        "last_at": draft_store.get_setting("sync_last_at", ""),
        "last_received": int(draft_store.get_setting("sync_last_received", "0") or 0),
        "last_error": draft_store.get_setting("sync_last_error", ""),
        "next_in_minutes": auto_sync_interval_minutes if auto_sync_enabled else None,
        "auto_enabled": auto_sync_enabled,
    }


@app.put("/api/settings/auto-sync")
def update_auto_sync(update: AutoSyncUpdate) -> dict[str, object]:
    global auto_sync_enabled, auto_sync_interval_minutes
    auto_sync_enabled = update.enabled
    auto_sync_interval_minutes = update.interval_minutes
    draft_store.set_setting("auto_sync_enabled", str(update.enabled))
    draft_store.set_setting("auto_sync_interval_minutes", str(update.interval_minutes))
    draft_store.audit("auto_sync_updated", details=f"enabled={update.enabled};interval={update.interval_minutes}")
    return get_auto_sync()


@app.get("/api/settings/auto-replies", response_model=AutoReplySettings)
def get_auto_replies() -> AutoReplySettings:
    return get_auto_reply_settings()


@app.put("/api/settings/auto-replies", response_model=AutoReplySettings)
def update_auto_replies(value: AutoReplySettings) -> AutoReplySettings:
    return save_auto_reply_settings(value)


@app.post("/api/auto-replies/run")
def run_auto_replies(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    """Сформировать автоответы для уже загруженных отзывов без публикации."""
    settings_value = get_auto_reply_settings()
    if not settings_value.enabled:
        raise HTTPException(409, "Автоответы выключены в настройках")
    items = draft_store.list_inbox(item_type="review", status="draft", limit=limit)
    results = [process_auto_review(item) for item in items]
    generated = sum(1 for result in results if result["status"] in {"moderation", "ready"})
    draft_store.audit("auto_replies_run", details=f"checked={len(results)};generated={generated}")
    return {"checked": len(results), "generated": generated, "results": results}


@app.get("/api/settings/questions", response_model=QuestionReplySettings)
def get_questions_settings() -> QuestionReplySettings:
    return get_question_settings()


@app.put("/api/settings/questions", response_model=QuestionReplySettings)
def update_questions_settings(value: QuestionReplySettings) -> QuestionReplySettings:
    draft_store.set_setting("module:questions", json.dumps(value.model_dump(), ensure_ascii=False))
    draft_store.audit("question_settings_updated", details=f"enabled={value.enabled};moderation={value.require_moderation}")
    return value


@app.post("/api/auto-questions/run")
def run_auto_questions(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    settings_value = get_question_settings()
    if not settings_value.enabled:
        raise HTTPException(409, "Автоответы на вопросы выключены")
    items = draft_store.list_inbox(item_type="question", status="draft", limit=limit)
    results = [process_auto_question(item) for item in items]
    generated = sum(1 for result in results if result["status"] in {"moderation", "ready"})
    draft_store.audit("auto_questions_run", details=f"checked={len(results)};generated={generated}")
    return {"checked": len(results), "generated": generated, "results": results}


@app.get("/api/audit")
def list_audit(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, object]]:
    return draft_store.list_audit(limit)


@app.get("/api/admin/backup")
def download_backup() -> FileResponse:
    """Create a recoverable SQLite snapshot for the operator."""
    source = draft_store.path
    if not source.exists():
        raise HTTPException(status_code=404, detail="Файл базы данных ещё не создан")
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    target = backup_dir / f"wbops_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
    shutil.copy2(source, target)
    draft_store.audit("backup_created", details=target.name)
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.get("/api/complaints", response_model=list[ComplaintDetails])
def list_complaints(status: ComplaintStatus | None = Query(default=None)) -> list[ComplaintDetails]:
    return [complaint_service.details(case, draft_store.get_inbox(case.item_id), draft_store.get(case.item_id))
            for case in draft_store.list_complaints(status)]


@app.get("/api/complaints/{item_id}", response_model=ComplaintDetails)
def complaint_details(item_id: str) -> ComplaintDetails:
    case = next((case for case in draft_store.list_complaints() if case.item_id == item_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail="Карточка жалобы не найдена")
    return complaint_service.details(case, draft_store.get_inbox(item_id), draft_store.get(item_id))

COMPLAINT_GROUPS = {"ready": ("ready_manual",), "not_eligible": ("not_eligible",), "submitted": ("submitted_manual", "submitted_browser"), "dismissed": ("dismissed",)}


def reevaluate_complaints() -> int:
    """Проверить основания для жалоб по всем отзывам 1–2★ (поданные, скрытые и открытые вручную не трогаем)."""
    existing = {c.item_id: c for c in draft_store.list_complaints()}
    changed = 0
    with draft_store._connect() as connection:
        ids = [r[0] for r in connection.execute("SELECT id FROM inbox_items WHERE item_type='review' AND rating IN (1,2)")]
    for item_id in ids:
        item = draft_store.get_inbox(item_id)
        if item is None:
            continue
        current = existing.get(item.id)
        if current and current.status != ComplaintStatus.NOT_ELIGIBLE:
            continue
        case = complaint_service.evaluate(item)
        if case and (current is None or current.reason_code != case.reason_code or current.status != case.status):
            draft_store.save_complaint(case)
            changed += 1
    return changed


def complaint_reasons() -> list[str]:
    try:
        reasons = json.loads(draft_store.get_setting("wb_complaint_reasons", "[]") or "[]")
    except ValueError:
        reasons = []
    return reasons or ["Другое"]


@app.get("/api/complaints-queue")
def complaints_queue(group: str = "ready", search: str = "", refresh: bool = False) -> dict[str, object]:
    if refresh:
        reevaluate_complaints()
    cases = draft_store.list_complaints()
    counts = {g: sum(1 for c in cases if c.status.value in statuses) for g, statuses in COMPLAINT_GROUPS.items()}
    wanted = COMPLAINT_GROUPS.get(group)
    names = analytics.product_names()
    cabinets = _cabinet_names()
    term = search.strip().lower()
    items = []
    for case in cases:
        if wanted and case.status.value not in wanted:
            continue
        item = draft_store.get_inbox(case.item_id)
        details = complaint_service.details(case, item, draft_store.get(case.item_id))
        row = {**details.model_dump(mode="json"),
               "created": item.created_at.isoformat() if item else None,
               "author_name": item.author_name if item else "", "product_name": (item.product_name if item else "") or names.get(details.product_id, ""),
               "cabinet_name": cabinets.get(case.cabinet_id, case.cabinet_id), "marketplace": item.marketplace if item else "wildberries",
               "delivery": draft_store.get_setting("complaint_delivery:" + case.item_id, "")}
        if term and term not in " ".join(str(row.get(k) or "") for k in ("review_text", "product_name", "author_name", "product_id", "item_id")).lower():
            continue
        items.append(row)
    items.sort(key=lambda r: r.get("created") or "", reverse=True)
    return {"items": items, "counts": counts, "reasons": complaint_reasons(), "browser": complaint_browser.snapshot()}


@app.post("/api/complaints-reevaluate")
def complaints_reevaluate() -> dict[str, object]:
    changed = reevaluate_complaints()
    draft_store.audit("complaints_reevaluated", details=f"changed={changed}")
    return {"changed": changed}


@app.get("/api/complaint-browser/status")
def complaint_browser_status() -> dict:
    return complaint_browser.snapshot()


@app.post("/api/complaint-browser/connect")
async def connect_complaint_browser() -> dict:
    return complaint_browser.start()


@app.get("/api/complaint-browser/inspect")
async def inspect_complaint_browser() -> dict:
    return await complaint_browser.inspect()

@app.post("/api/complaint-browser/reviews")
async def open_browser_reviews() -> dict:
    return await complaint_browser.reviews()

@app.post("/api/complaint-browser/review/{item_id}")
async def open_browser_complaint_review(item_id: str) -> dict:
    item = draft_store.get_inbox(item_id)
    if item is None or item.item_type.value != "review" or item.rating not in (1, 2) or not item.product_id:
        raise HTTPException(404, "Отзыв 1–2 звезды с WB ID не найден")
    return await complaint_browser.open_review(item)


@app.post("/api/complaint-browser/prepare/{item_id}")
async def prepare_browser_complaint(item_id: str, request: ComplaintPrepareRequest) -> dict:
    item = draft_store.get_inbox(item_id)
    if item is None:
        raise HTTPException(404, "Отзыв не найден")
    return await complaint_browser.prepare(item, request)


@app.post("/api/complaint-browser/submit")
async def submit_browser_complaint(request: ComplaintSubmitRequest) -> dict:
    return await complaint_browser.submit(request)


@app.put("/api/complaints/{item_id}", response_model=ComplaintCase)
def update_complaint(item_id: str, update: ComplaintUpdate) -> ComplaintCase:
    if update.status == ComplaintStatus.SUBMITTED_BROWSER:
        raise HTTPException(400, "Этот статус устанавливается только после подтверждения WB")
    case = draft_store.update_complaint_status(item_id, update.status)
    if case is None:
        raise HTTPException(status_code=404, detail="Карточка жалобы не найдена")
    draft_store.audit("complaint_status_updated", item_id, update.status.value)
    return case


@app.get("/api/inbox", response_model=list[InboxItem])
def list_inbox(item_type: str | None = Query(default=None, pattern="^(review|question)$"), rating: int | None = Query(default=None, ge=1, le=5), product_id: int | None = Query(default=None), order_status: OrderStatus | None = Query(default=None), search: str | None = Query(default=None, max_length=100), status: str | None = Query(default=None, pattern="^(draft|published)$"), limit: int = Query(default=200, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> list[InboxItem]:
    return draft_store.list_inbox(item_type=item_type, rating=rating, product_id=product_id, order_status=order_status, search=search, status=status, limit=limit, offset=offset)


@app.get("/api/inbox/overview")
def inbox_overview(item_type: str | None = Query(default=None, pattern="^(review|question)$"), rating: int | None = Query(default=None, ge=1, le=5), product_id: int | None = Query(default=None), cabinet_id: str | None = Query(default=None, max_length=64), order_status: OrderStatus | None = Query(default=None), search: str | None = Query(default=None, max_length=100), status: str | None = Query(default=None, pattern="^(new|draft|ready|sending|error|published)$"), topic: str | None = Query(default=None, max_length=40), sentiment: str | None = Query(default=None, pattern="^(positive|negative|neutral)$"), date_from: str | None = Query(default=None, max_length=10), date_to: str | None = Query(default=None, max_length=10), sort: str = Query(default="desc", pattern="^(asc|desc)$"), limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> list[dict[str, object]]:
    return draft_store.list_inbox_overview(item_type=item_type, rating=rating, product_id=product_id, cabinet_id=cabinet_id, order_status=order_status, search=search, status=status, topic=topic, sentiment=sentiment, date_from=date_from, date_to=date_to, sort=sort, limit=limit, offset=offset)


@app.post("/api/replies/draft", response_model=ReplyDraft)
def create_reply_draft(item: InboxItem) -> ReplyDraft:
    return draft_store.save_generated(reply_service.draft(item))


@app.get("/api/replies/drafts", response_model=list[ReplyDraft])
def list_reply_drafts() -> list[ReplyDraft]:
    return draft_store.list()


@app.put("/api/replies/drafts/{item_id}", response_model=ReplyDraft)
def update_reply_draft(item_id: str, update: DraftUpdate) -> ReplyDraft:
    if not update.text.strip():
        raise HTTPException(status_code=400, detail="Текст черновика не может быть пустым")
    draft = draft_store.update_text(item_id, update.text.strip())
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик не найден")
    draft_store.audit("draft_updated", item_id)
    return draft


@app.get("/api/replies/drafts/{item_id}/history")
def reply_draft_history(item_id: str) -> list[dict[str, object]]:
    return draft_store.draft_history(item_id)


@app.post("/api/replies/drafts/{item_id}/regenerate", response_model=ReplyDraft)
def regenerate_reply_draft(item_id: str) -> ReplyDraft:
    item = draft_store.get_inbox(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Входящий объект не найден")
    draft = draft_store.update_text(item_id, reply_service.draft(item).text)
    if draft is None:
        draft = draft_store.save_generated(reply_service.draft(item))
    draft_store.audit("draft_regenerated", item_id)
    return draft


@app.post("/api/replies/check/{item_id}")
def check_reply(item_id: str, update: DraftUpdate) -> dict[str, object]:
    item = draft_store.get_inbox(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Входящий объект не найден")
    warnings = reply_service.validate(update.text, item)
    return {"ok": not warnings, "warnings": warnings}


@app.post("/api/replies/drafts/{item_id}/confirm", response_model=ReplyDraft)
def confirm_reply_draft(item_id: str) -> ReplyDraft:
    draft = draft_store.confirm(item_id, True)
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик не найден")
    draft_store.audit("draft_confirmed", item_id)
    return draft


@app.post("/api/replies/drafts/{item_id}/moderate", response_model=ReplyDraft)
def moderate_reply_draft(item_id: str, update: ModerationUpdate) -> ReplyDraft:
    draft = draft_store.get(item_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик не найден")
    if update.action == "approve":
        result = draft_store.moderate(item_id, "approved", update.comment)
    elif update.action == "reject":
        result = draft_store.moderate(item_id, "rejected", update.comment)
    else:
        result = draft_store.moderate(item_id, "rework", update.comment)
    draft_store.audit(f"draft_{update.action}", item_id, update.comment)
    return result or draft


@app.post("/api/replies/group-action")
def group_reply_action(request: GroupActionRequest) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for item_id in request.item_ids:
        draft = draft_store.get(item_id)
        if draft is None:
            results.append({"item_id": item_id, "status": "missing"})
            continue
        if request.action == "confirm":
            draft_store.moderate(item_id, "approved")
        elif request.action == "reject":
            draft_store.moderate(item_id, "rejected")
        elif request.action == "rework":
            draft_store.moderate(item_id, "rework")
        elif request.action == "regenerate":
            item = draft_store.get_inbox(item_id)
            if item:
                draft_store.update_text(item_id, reply_service.draft(item).text)
        results.append({"item_id": item_id, "status": "ok"})
    draft_store.audit("group_action", details=f"action={request.action};count={len(results)}")
    return {"action": request.action, "results": results}


@app.post("/api/replies/publish/{item_id}")
async def publish_reply(item_id: str) -> dict[str, object]:
    item = draft_store.get_inbox(item_id)
    draft = draft_store.get(item_id)
    if item is None or draft is None:
        raise HTTPException(status_code=404, detail="Входящий объект или черновик не найден")
    warnings = reply_service.validate(draft.text, item)
    if warnings:
        raise HTTPException(status_code=409, detail={"message": "Черновик не прошёл проверку", "warnings": warnings})
    return await reply_publisher.send(item_id)


@app.post("/api/replies/send/{item_id}")
async def send_reply(item_id: str, update: DraftUpdate) -> dict[str, object]:
    item = draft_store.get_inbox(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Входящий объект не найден")
    existing = draft_store.get(item_id)
    if existing and existing.moderation_status != "approved":
        raise HTTPException(status_code=409, detail="Сначала проверьте и подтвердите ответ в модерации")
    warnings = reply_service.validate(update.text, item)
    if warnings:
        raise HTTPException(status_code=409, detail={"message": "Ответ не прошёл проверку", "warnings": warnings})
    return await reply_publisher.send(item_id, update.text)

@app.post("/api/replies/bulk/preview")
def preview_review_batch(request: BulkPreviewRequest) -> dict:
    return bulk_sender.preview(request)


@app.post("/api/replies/bulk/{token}/send")
async def send_review_batch(token: str, request: BulkSendRequest | None = None) -> dict:
    return bulk_sender.start(token, request.texts if request else None)


@app.get("/api/replies/bulk/{token}")
def review_batch_status(token: str) -> dict:
    return bulk_sender.get(token)


@app.get("/api/reports")
def list_reports() -> dict[str, object]:
    importer = ReportImporter(settings.fbs_report_root)
    reports = importer.scan()
    rows = importer.rows()
    return {
        "count": len(reports),
        "row_count": len(rows),
        "quantity_total": sum(row.quantity for row in rows),
        "reports": reports,
        "rows": rows,
    }


@app.get("/api/fbs-reports")
def list_fbs_reports() -> dict[str, object]:
    """Явное имя нового раздела; старый /api/reports оставлен для совместимости."""
    result = list_reports()
    reports = [report.model_dump() if hasattr(report, "model_dump") else report for report in result["reports"]]
    for report in reports:
        report["status"] = draft_store.get_setting(f"fbs_status:{report['filename']}", "new")
    result["reports"] = reports
    return result


@app.post("/api/fbs-reports/upload")
async def upload_fbs_report(file: UploadFile = File(...)) -> dict[str, object]:
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in {".pdf", ".csv", ".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Поддерживаются только PDF, CSV и Excel-файлы")
    target_dir = Path(settings.fbs_report_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        raise HTTPException(status_code=409, detail="Файл с таким именем уже загружен")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл не должен превышать 50 МБ")
    target.write_bytes(content)
    draft_store.set_setting(f"fbs_status:{filename}", "new")
    draft_store.audit("fbs_report_uploaded", details=filename)
    return {"filename": filename, "status": "new", "size_bytes": len(content)}


@app.put("/api/fbs-reports/{filename}/status")
def update_fbs_report_status(filename: str, status: str = Query(..., pattern="^(new|checked|reported|error)$")) -> dict[str, str]:
    safe_name = Path(filename).name
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    if not (Path(settings.fbs_report_root) / safe_name).exists():
        raise HTTPException(status_code=404, detail="FBS-файл не найден")
    draft_store.set_setting(f"fbs_status:{safe_name}", status)
    draft_store.audit("fbs_report_status_updated", details=f"{safe_name}:{status}")
    return {"filename": safe_name, "status": status}


@app.get("/api/templates")
def list_templates() -> dict[str, str]:
    return template_store.all()


@app.put("/api/templates/{key}")
async def update_template(key: str, request: Request) -> dict[str, str]:
    payload = await request.json()
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Ожидается поле text")
    try:
        value = template_store.update(key, text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Такого шаблона нет") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    draft_store.audit("template_updated", details=key)
    return {"key": key, "text": value}


@app.get("/api/templates/custom")
def list_custom_templates() -> list[dict[str, object]]:
    return template_store.custom_all()


@app.post("/api/templates/custom")
def create_custom_template(value: TemplateCreate) -> dict[str, object]:
    result = template_store.custom_create(value.model_dump())
    draft_store.audit("custom_template_created", details=str(result.get("id")))
    return result


@app.put("/api/templates/custom/{template_id}")
def update_custom_template(template_id: str, value: TemplateCreate) -> dict[str, object]:
    try:
        result = template_store.custom_update(template_id, value.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Шаблон не найден") from exc
    draft_store.audit("custom_template_updated", details=template_id)
    return result


@app.delete("/api/templates/custom/{template_id}")
def delete_custom_template(template_id: str) -> dict[str, bool]:
    try:
        template_store.custom_delete(template_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Шаблон не найден") from exc
    draft_store.audit("custom_template_deleted", details=template_id)
    return {"deleted": True}


@app.get("/api/settings/modules/{module}")
def get_module_settings(module: str) -> dict[str, object]:
    raw = draft_store.get_setting(f"module:{module}", "{}")
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        values = {}
    return {"module": module, "values": values if isinstance(values, dict) else {}}


@app.put("/api/settings/modules/{module}")
def update_module_settings(module: str, value: AppModuleSettings) -> dict[str, object]:
    draft_store.set_setting(f"module:{module}", json.dumps(value.values, ensure_ascii=False))
    draft_store.audit("module_settings_updated", details=module)
    return {"module": module, "values": value.values}


@app.get("/api/chats")
def list_chats(marketplace: str | None = None, status: str | None = None, search: str | None = None) -> list[dict]:
    return chat_store.list(marketplace=marketplace, status=status, search=search)


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str) -> dict:
    chat = chat_store.get(chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    return chat


@app.post("/api/chats/sync")
def sync_chats(value: ChatSyncRequest | None = None) -> dict[str, object]:
    count = chat_store.upsert_many(value.chats if value else [])
    draft_store.audit("chats_synced", details=f"received={count}")
    return {"received": count, "source": "normalized_chat_store"}


@app.post("/api/chats/{chat_id}/reply")
def reply_to_chat(chat_id: str, value: ChatMessageCreate) -> dict:
    chat = chat_store.get(chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    updated = chat_store.add_message(chat_id, value.text, sender="seller", status="answered")
    draft_store.audit("chat_reply_saved", chat_id)
    return updated


@app.post("/api/chats/{chat_id}/status")
def update_chat_status(chat_id: str, value: dict[str, str]) -> dict:
    chat = chat_store.get(chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    status = value.get("status", "waiting")
    if status not in {"waiting", "answered", "closed"}:
        raise HTTPException(400, "Недопустимый статус чата")
    chat_store.upsert_many([{**chat, "status": status}])
    return chat_store.get(chat_id) or chat


@app.get("/api/analytics/reviews")
def review_analytics() -> dict[str, object]:
    rows = draft_store.list_inbox_overview(item_type="review", limit=500)
    topics: dict[str, int] = {}
    sentiments: dict[str, int] = {}
    ratings: dict[str, int] = {}
    for row in rows:
        for value in row.get("topics", []):
            topics[value] = topics.get(value, 0) + 1
        sentiment = str(row.get("sentiment", "neutral"))
        sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
        rating = str(row.get("rating") or "без оценки")
        ratings[rating] = ratings.get(rating, 0) + 1
    return {"total": len(rows), "topics": topics, "topic_labels": {key: topic_label(key) for key in topics}, "sentiments": sentiments, "ratings": ratings}


def _analytics_period(date_from: str | None, date_to: str | None, default_days: int = 30) -> tuple:
    from datetime import date as _date, timedelta as _td
    try:
        end = _date.fromisoformat(date_to) if date_to else _date.today()
        start = _date.fromisoformat(date_from) if date_from else end - _td(days=default_days - 1)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if start > end:
        raise HTTPException(400, "Дата «с» позже даты «по»")
    if (end - start).days > 1100:
        raise HTTPException(400, "Период не больше трёх лет")
    return start, end


def _analytics_kind(kind: str) -> str:
    if kind not in ("review", "question", "chat"):
        raise HTTPException(400, "kind: review, question или chat")
    return kind


def _grain(grain: str) -> str:
    return grain if grain in ("day", "week", "month") else "day"


def _table_download(tables: dict, fmt: str, name: str) -> Response:
    if fmt == "xlsx":
        try:
            content = analytics_module.to_xlsx(tables)
        except ImportError:
            raise HTTPException(500, "Для Excel нужен пакет openpyxl — скачайте CSV или выполните .venv\\Scripts\\pip install openpyxl")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif fmt == "csv":
        content, media = analytics_module.to_csv(tables), "text/csv; charset=utf-8"
    else:
        raise HTTPException(400, "format: csv или xlsx")
    from urllib.parse import quote
    filename = f"{name}.{fmt}"
    return Response(content, media_type=media, headers={"Content-Disposition": f"attachment; filename=\"report.{fmt}\"; filename*=UTF-8''{quote(filename)}"})


@app.get("/api/analytics/filters")
def analytics_filters() -> dict[str, object]:
    with draft_store._connect() as c:
        cabinets = [r[0] for r in c.execute("SELECT DISTINCT cabinet_id FROM inbox_items WHERE cabinet_id<>'' ORDER BY cabinet_id")]
        span = c.execute("SELECT MIN(created_at), MAX(created_at) FROM inbox_items").fetchone()
    try:
        names = {c.id: c.name for c in configured_cabinets()}
    except Exception:
        names = {}
    return {"marketplaces": analytics_module.MARKETPLACES, "cabinets": [{"id": c, "name": names.get(c, c)} for c in cabinets],
            "first_day": (span[0] or "")[:10], "last_day": (span[1] or "")[:10], "methods": analytics_module.METHODS}


@app.get("/api/analytics/dashboard")
def analytics_dashboard(kind: str = "review", date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                        cabinet: str | None = None, marketplace: str | None = None, grain: str = "day",
                        format: str | None = None):
    start, end = _analytics_period(date_from, date_to)
    data = analytics.dashboard(_analytics_kind(kind), start, end, cabinet or None, marketplace or None, _grain(grain))
    if format:
        return _table_download(analytics_module.dashboard_tables(data), format, f"analytics_{kind}_{start}_{end}")
    return data


@app.get("/api/analytics/detailed")
def analytics_detailed(kind: str = "review", date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                       cabinet: str | None = None, marketplace: str | None = None, grain: str = "week") -> dict[str, object]:
    start, end = _analytics_period(date_from, date_to)
    return analytics.detailed(_analytics_kind(kind), start, end, cabinet or None, marketplace or None, _grain(grain))


@app.get("/api/analytics/tags")
def analytics_tags(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                   cabinet: str | None = None, marketplace: str | None = None, product_id: int | None = None) -> dict[str, object]:
    start, end = _analytics_period(date_from, date_to)
    return analytics.tags(start, end, cabinet or None, marketplace or None, product_id)


@app.get("/api/analytics/trends")
def analytics_trends(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                     cabinet: str | None = None, marketplace: str | None = None) -> dict[str, object]:
    start, end = _analytics_period(date_from, date_to)
    return analytics.trends(start, end, cabinet or None, marketplace or None)


@app.get("/api/analytics/products")
def analytics_products(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to")) -> dict[str, object]:
    start, end = _analytics_period(date_from, date_to, 365)
    return {"products": analytics.products(start, end)}


@app.get("/api/analytics/product/{product_id}")
def analytics_product(product_id: int, date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                      format: str | None = None):
    start, end = _analytics_period(date_from, date_to, 365)
    data = analytics.product(product_id, start, end)
    if format:
        return _table_download(analytics_module.report_tables(data), format, f"product_{product_id}_{start}_{end}")
    return data


@app.post("/api/analytics/product/{product_id}/ai")
async def analytics_product_ai(product_id: int, date_from: str | None = Query(None, alias="from"),
                               date_to: str | None = Query(None, alias="to")) -> dict[str, object]:
    start, end = _analytics_period(date_from, date_to, 365)
    data = analytics.product(product_id, start, end)
    texts = analytics.review_texts(product_id, start, end)
    try:
        result = await asyncio.to_thread(ai_reply_writer.analyze_product, data.get("name") or "", texts)
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300])
    return {**result, "reviews_used": len(texts)}


@app.get("/api/analytics/reports")
def analytics_reports(search: str = "") -> dict[str, object]:
    return {"reports": analytics.list_reports(search)}


@app.post("/api/analytics/reports")
def analytics_save_report(value: dict) -> dict[str, object]:
    if "product_id" in value and "stats" not in value:
        start, end = _analytics_period(value.get("date_from"), value.get("date_to"), 365)
        data = analytics.product(int(value["product_id"]), start, end)
        if isinstance(value.get("ai"), dict):
            data["ai"] = value["ai"]
    elif {"product_id", "stats", "date_from", "date_to", "pros", "cons", "advice", "ratings"} <= set(value):
        data = value
    else:
        raise HTTPException(400, "Нужен product_id и период")
    if not data["stats"]["reviews"] and not data["stats"]["questions"]:
        raise HTTPException(400, "По товару нет отзывов за выбранный период")
    return analytics.save_report(data)


@app.get("/api/analytics/reports/{report_id}")
def analytics_report(report_id: int, format: str | None = None):
    data = analytics.get_report(report_id)
    if not data:
        raise HTTPException(404, "Отчёт не найден")
    if format:
        return _table_download(analytics_module.report_tables(data), format, f"report_{report_id}_{data['product_id']}")
    return data


@app.delete("/api/analytics/reports/{report_id}")
def analytics_delete_report(report_id: int) -> dict[str, object]:
    if not analytics.delete_report(report_id):
        raise HTTPException(404, "Отчёт не найден")
    return {"deleted": report_id}


def _review_filters(item_type, search, date_from, date_to, marketplace, cabinet_id, order_status, answer, rating, tag, product_id, sort):
    ratings = sorted({int(x) for x in re.findall(r"[1-5]", rating or "")}) or None
    if answer and answer not in ANSWER_STATUSES:
        raise HTTPException(400, "Неизвестный статус ответа")
    return dict(item_type=item_type, search=(search or "")[:100], date_from=date_from, date_to=date_to, marketplace=marketplace or None,
                cabinet_id=cabinet_id or None, order_status=order_status or None, answer=answer or None, ratings=ratings,
                tags=[t for t in (tag or "").split(",") if t][:6], product_id=product_id, sort="asc" if sort == "asc" else "desc")


@app.get("/api/reviews")
def reviews_list(item_type: str = Query("review", pattern="^(review|question)$"), search: str = "", date_from: str | None = Query(None, alias="from"),
                 date_to: str | None = Query(None, alias="to"), marketplace: str = "", cabinet_id: str = "", order_status: str = "", answer: str = "",
                 rating: str = "", tag: str = "", product_id: int | None = None, sort: str = "desc",
                 limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), meta: bool = False) -> dict[str, object]:
    filters = _review_filters(item_type, search, date_from, date_to, marketplace, cabinet_id, order_status, answer, rating, tag, product_id, sort)
    data = review_list.query(**filters, limit=limit, offset=offset, with_warnings=reply_service.validate)
    if meta:
        data["meta"] = {
            "cabinets": [c.public() for c in configured_cabinets()],
            "marketplaces": CABINET_MARKETPLACES, "answer_statuses": ANSWER_STATUSES, "order_statuses": ORDER_STATUSES,
            "tags": [{"tag": k, "title": v[0], "icon": TAG_ICONS.get(k, "•"), "positive": bool(v[1]), "negative": bool(v[2])} for k, v in ANALYTICS_TAGS.items()],
            "publishing": {"enabled": not settings.dry_run or manual_send_enabled},
            "complaints": len(draft_store.list_complaints("ready_manual")),
        }
    return data


@app.get("/api/reviews/export")
def reviews_export(item_type: str = Query("review", pattern="^(review|question)$"), search: str = "", date_from: str | None = Query(None, alias="from"),
                   date_to: str | None = Query(None, alias="to"), marketplace: str = "", cabinet_id: str = "", order_status: str = "", answer: str = "",
                   rating: str = "", tag: str = "", product_id: int | None = None, sort: str = "desc", format: str = "xlsx"):
    filters = _review_filters(item_type, search, date_from, date_to, marketplace, cabinet_id, order_status, answer, rating, tag, product_id, sort)
    rows = review_list.export_rows(**filters)
    draft_store.audit("reviews_exported", details=f"count={len(rows) - 1}")
    return _table_download({"Отзывы" if item_type == "review" else "Вопросы": rows}, format, f"{'reviews' if item_type == 'review' else 'questions'}_{datetime.now():%Y-%m-%d}")


@app.get("/api/wb/images")
async def wb_images_batch(nm: str = "", size: str = "small") -> dict[str, object]:
    ids = [int(x) for x in re.findall(r"\d{4,12}", nm)]
    return {"urls": await wb_images.urls(ids, size)}


@app.get("/api/wb/image/{nm}")
async def wb_image(nm: int, size: str = "small"):
    from app.services.wb_images import image_url
    if nm < 1000 or nm > 10**11:
        raise HTTPException(404, "Нет фото")
    host = await wb_images.host_for(nm)
    if not host:
        raise HTTPException(404, "Нет фото")
    return RedirectResponse(image_url(host, nm, size), status_code=302, headers={"Cache-Control": "max-age=86400"})


@app.get("/api/products/knowledge")
def list_product_knowledge() -> dict[str, object]:
    return {"products": product_knowledge.all()}


@app.get("/api/knowledge/overview")
def knowledge_overview(period: str = Query(default="30")) -> dict[str, object]:
    products = product_knowledge.all()
    known = 0
    records = 0
    for facts in products.values():
        entries = [str(value).strip() for value in facts.values() if value not in (None, "")]
        if entries:
            known += 1
            records += len(entries)
    return {"period": period, "total_products": len(products), "known_products": known, "without_knowledge": max(0, len(products) - known), "records": records, "coverage": round(known / len(products) * 100) if products else 0, "sources": {"descriptions": known, "answers": 0, "reviews": 0, "chats": 0}}


@app.get("/api/knowledge/groups")
def list_knowledge_groups() -> list[dict]:
    return knowledge_groups.all()


@app.post("/api/knowledge/groups")
def save_knowledge_group(value: dict) -> dict:
    result = knowledge_groups.upsert(value)
    draft_store.audit("knowledge_group_saved", details=str(result.get("id")))
    return result


@app.get("/api/products/knowledge/{product_id}")
def get_product_knowledge(product_id: int) -> dict[str, object]:
    return {"product_id": product_id, "facts": product_knowledge.get(product_id) or {}}


@app.put("/api/products/knowledge/{product_id}")
async def update_product_knowledge(product_id: int, request: Request) -> dict[str, object]:
    try:
        facts = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON с характеристиками товара") from exc
    if not isinstance(facts, dict):
        raise HTTPException(status_code=400, detail="Характеристики должны быть объектом")
    saved = product_knowledge.upsert(product_id, facts)
    draft_store.audit("product_knowledge_updated", details=f"product_id={product_id}")
    return {"product_id": product_id, "facts": saved}


# --- Индивидуальные ответы на отзывы: режим, фразы сборщика, ИИ, предпросмотр ---------------------
@app.get("/api/replies/review-mode")
def get_review_mode() -> dict[str, object]:
    return {"mode": reply_service.review_mode, "ai": ai_writer.describe(ai_config()), "ai_last_error": ai_reply_writer.last_error}


@app.put("/api/replies/review-mode")
async def set_review_mode(request: Request) -> dict[str, object]:
    payload = await request.json()
    mode = str((payload or {}).get("mode") or "")
    if mode not in REVIEW_MODES:
        raise HTTPException(400, "Режим: individual, ai или templates")
    if mode == "ai" and not ai_config()["api_key"]:
        raise HTTPException(400, "Сначала укажите API-ключ ИИ")
    draft_store.set_setting("review_mode", mode)
    reply_service.review_mode = mode
    draft_store.audit("review_mode_updated", details=mode)
    return get_review_mode()


@app.get("/api/templates/phrases")
def list_review_phrases() -> dict[str, object]:
    phrases = phrase_store.all()
    return {"phrases": [{"key": key, "title": PHRASE_TITLES.get(key, key), "variants": phrases[key],
                         "customized": phrases[key] != DEFAULT_PHRASES[key]} for key in DEFAULT_PHRASES]}


@app.put("/api/templates/phrases/{key}")
async def update_review_phrase(key: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    variants = (payload or {}).get("variants")
    if not isinstance(variants, list):
        raise HTTPException(400, "Ожидается список вариантов")
    try:
        saved = phrase_store.update(key, [str(v) for v in variants])
    except KeyError as exc:
        raise HTTPException(404, "Неизвестная фраза") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    draft_store.audit("review_phrase_updated", details=key)
    return {"key": key, "variants": saved}


@app.delete("/api/templates/phrases/{key}")
def reset_review_phrase(key: str) -> dict[str, object]:
    try:
        return {"key": key, "variants": phrase_store.reset(key)}
    except KeyError as exc:
        raise HTTPException(404, "Неизвестная фраза") from exc


@app.post("/api/replies/preview")
async def preview_review_reply(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    status = str(payload.get("order_status") or "notSpecified")
    if status not in ("buyout", "rejected", "returned", "notSpecified"):
        status = "notSpecified"
    rating = payload.get("rating")
    item = InboxItem(id=str(payload.get("id") or f"preview-{payload.get('seed', 0)}"), cabinet_id="preview", item_type="review",
                     text=str(payload.get("text") or "")[:5000], rating=int(rating) if rating not in (None, "") else None,
                     order_status=status, author_name=str(payload.get("author_name") or "") or None,
                     product_name=str(payload.get("product_name") or "") or None)
    mode = payload.get("mode") or reply_service.review_mode
    source, text = "individual", None
    if mode == "ai":
        text = ai_reply_writer.write(item, reply_service.validate)
        source = "ai" if text else "individual"
    if not text:
        text, _ = reply_service.composer.compose(item)
    facts = analyze_review_facts(item)
    return {"text": text, "source": source, "ai_error": ai_reply_writer.last_error if mode == "ai" and source != "ai" else None,
            "name": facts.name, "status": facts.status,
            "details": [PHRASE_TITLES.get(k, k) for k in sorted(facts.aspects)],
            "warnings": reply_service.validate(text, item)}


@app.post("/api/replies/regenerate-reviews")
def regenerate_review_drafts() -> dict[str, object]:
    """Пересоздать черновики отзывов в текущем режиме. Подтверждённые, отправленные и исправленные вручную не трогаем."""
    updated = kept = 0
    items, offset = [], 0
    while True:
        page = draft_store.list_inbox(item_type="review", limit=500, offset=offset)
        items += page
        if len(page) < 500:
            break
        offset += 500
    for item in items:
        before = draft_store.get(item.id)
        saved = draft_store.save_generated(reply_service.draft(item))
        if before is not None and saved.text == before.text:
            kept += 1
        else:
            updated += 1
    draft_store.audit("review_drafts_regenerated", details=f"mode={reply_service.review_mode};updated={updated};kept={kept}")
    return {"updated": updated, "kept": kept, "mode": reply_service.review_mode}


@app.get("/api/ai/settings")
def get_ai_settings() -> dict[str, object]:
    return {**ai_writer.describe(ai_config()), "last_error": ai_reply_writer.last_error}


@app.put("/api/ai/settings")
async def update_ai_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    provider = str(payload.get("provider") or "anthropic")
    if provider not in ("anthropic", "openai"):
        raise HTTPException(400, "Провайдер: anthropic или openai")
    draft_store.set_setting("ai_provider", provider)
    draft_store.set_setting("ai_base_url", str(payload.get("base_url") or "").strip())
    draft_store.set_setting("ai_model", str(payload.get("model") or "").strip())
    if payload.get("api_key") is not None:
        key = str(payload.get("api_key") or "").strip()
        draft_store.set_setting("ai_api_key", key)
        if not key and reply_service.review_mode == "ai":
            draft_store.set_setting("review_mode", "individual")
            reply_service.review_mode = "individual"
    draft_store.audit("ai_settings_updated", details=provider)
    return get_ai_settings()


@app.post("/api/ai/test")
def test_ai_settings() -> dict[str, object]:
    cfg = ai_config()
    try:
        models = ai_reply_writer.list_models(cfg)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось подключиться: {exc}") from exc
    return {"ok": True, "models": models[:50], "selected": cfg["model"] or (ai_reply_writer.pick_model(cfg) if models else "")}


# --- Вопросы: режим, проверка, модели товаров ----------------------------------------------------
@app.get("/api/replies/question-mode")
def get_question_mode() -> dict[str, object]:
    models = product_models.all()
    filled = sum(1 for m in models.values() if m.get("sizes"))
    return {"mode": reply_service.question_mode, "ai": ai_writer.describe(ai_config()), "ai_last_error": ai_reply_writer.last_error,
            "models_total": len(models), "models_with_sizes": filled}


@app.put("/api/replies/question-mode")
async def set_question_mode(request: Request) -> dict[str, object]:
    payload = await request.json()
    mode = str((payload or {}).get("mode") or "")
    if mode not in REVIEW_MODES:
        raise HTTPException(400, "Режим: individual, ai или templates")
    if mode == "ai" and not ai_config()["api_key"]:
        raise HTTPException(400, "Сначала укажите API-ключ ИИ в разделе «Настройки ИИ»")
    draft_store.set_setting("question_mode", mode)
    reply_service.question_mode = mode
    draft_store.audit("question_mode_updated", details=mode)
    return get_question_mode()


@app.post("/api/replies/question-preview")
async def preview_question_reply(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    product = str(payload.get("product_id") or "").strip()
    item = InboxItem(id=f"qpreview-{payload.get('seed', 0)}", cabinet_id="preview", item_type="question",
                     text=str(payload.get("text") or "")[:5000], product_id=int(product) if product.isdigit() else None,
                     author_name=str(payload.get("author_name") or "") or None)
    if not item.text.strip():
        raise HTTPException(400, "Введите текст вопроса")
    mode = payload.get("mode") or reply_service.question_mode
    previous = reply_service.question_mode
    reply_service.question_mode = mode if mode in REVIEW_MODES else previous
    try:
        draft = reply_service.draft(item)
    finally:
        reply_service.question_mode = previous
    facts = analyze_question(item)
    model = product_models.for_product(item.product_id)
    source = "templates" if mode == "templates" else ("ai" if mode == "ai" and ai_reply_writer.last_error is None and ai_config()["api_key"] else "individual")
    params = {k: v for k, v in {"рост": facts.height, "вес": facts.weight, "обхват талии": facts.waist, "обхват бёдер": facts.hips,
                                 "размер": facts.size, "рос. размер": facts.ru_size, "ростовка": facts.length_l, "маркировка": facts.mark}.items() if v}
    return {"text": draft.text, "source": source, "ai_error": ai_reply_writer.last_error if mode == "ai" else None,
            "intents": facts.intents, "params": params, "model": (model or {}).get("key"),
            "model_has_sizes": bool((model or {}).get("sizes"))}


@app.post("/api/replies/regenerate-questions")
def regenerate_question_drafts() -> dict[str, object]:
    updated = kept = 0
    items, offset = [], 0
    while True:
        page = draft_store.list_inbox(item_type="question", limit=500, offset=offset)
        items += page
        if len(page) < 500:
            break
        offset += 500
    for item in items:
        before = draft_store.get(item.id)
        saved = draft_store.save_generated(reply_service.draft(item))
        if before is not None and saved.text == before.text:
            kept += 1
        else:
            updated += 1
    draft_store.audit("question_drafts_regenerated", details=f"mode={reply_service.question_mode};updated={updated};kept={kept}")
    return {"updated": updated, "kept": kept, "mode": reply_service.question_mode}


def _model_hints(nm_ids: list) -> list[dict[str, str]]:
    wanted = {int(x) for x in nm_ids}
    hints = []
    for record in getattr(reply_service.knowledge, "records", []) or []:
        answer = str(record.get("answer") or "")
        if record.get("nm_id") in wanted and re.search(r"\d", answer):
            hints.append({"question": str(record.get("question") or "")[:200], "answer": answer[:500]})
    return hints[:25]


def _inbox_products() -> dict[str, dict[str, object]]:
    products: dict[str, dict[str, object]] = {}
    for item in draft_store.list_inbox(item_type="question", limit=500) + draft_store.list_inbox(item_type="review", limit=500):
        if item.product_id:
            entry = products.setdefault(str(item.product_id), {"name": item.product_name or "", "questions": 0})
            if item.item_type.value == "question":
                entry["questions"] += 1
    return products


@app.get("/api/models")
def list_product_models() -> dict[str, object]:
    products = _inbox_products()
    models = []
    for key, m in sorted(product_models.all().items(), key=lambda kv: kv[0].lower()):
        names = [products[str(nm)]["name"] for nm in m.get("nm_ids", []) if str(nm) in products and products[str(nm)]["name"]]
        questions = sum(products.get(str(nm), {}).get("questions", 0) for nm in m.get("nm_ids", []))
        filled = [f for f in FACT_FIELDS if f != "name" and m.get(f)]
        models.append({"key": key, "name": m.get("name") or (names[0] if names else ""), "nm_ids": m.get("nm_ids", []),
                       "sizes": len(m.get("sizes", [])), "facts": len(filled), "open_questions": questions})
    linked = {str(nm) for m in product_models.all().values() for nm in m.get("nm_ids", [])}
    unlinked = [{"nm_id": nm, **info} for nm, info in products.items() if nm not in linked and info["questions"]]
    unlinked.sort(key=lambda x: -x["questions"])
    models.sort(key=lambda m: (-m["open_questions"], m["key"].lower()))
    return {"models": models, "unlinked": unlinked[:30], "fact_fields": FACT_FIELDS, "size_fields": SIZE_FIELDS}


@app.get("/api/models/{key}")
def get_product_model(key: str) -> dict[str, object]:
    model = product_models.get(key)
    if model is None:
        raise HTTPException(404, "Модель не найдена")
    return {"key": key, **model, "hints": _model_hints(model.get("nm_ids", []))}


@app.put("/api/models/{key}")
async def save_product_model(key: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    new_key = str(payload.get("key") or key).strip()
    old = product_models.get(key) if new_key != key else None
    if new_key != key and product_models.get(new_key) is not None:
        raise HTTPException(400, f"Модель {new_key} уже есть")
    try:
        if old is not None:
            product_models.delete(key)
        saved = product_models.save(new_key, payload)
    except ValueError as exc:
        if old is not None:
            product_models.save(key, old)
        raise HTTPException(400, str(exc)) from exc
    draft_store.audit("product_model_saved", details=new_key)
    return saved


@app.delete("/api/models/{key}")
def delete_product_model(key: str) -> dict[str, object]:
    try:
        product_models.delete(key)
    except KeyError as exc:
        raise HTTPException(404, "Модель не найдена") from exc
    draft_store.audit("product_model_deleted", details=key)
    return {"deleted": key}


# --- Финансы: ОПиУ из папки opiu -----------------------------------------------------------
finance_runner = finance.ReportRunner()


def finance_settings() -> dict[str, str]:
    return {"dir": draft_store.get_setting("finance_dir", ""), "python": draft_store.get_setting("finance_python", "")}


def finance_project():
    configured = finance_settings()["dir"]
    project = finance.find_project_dir(configured or None)
    if project is None:
        detail = (f"В папке «{configured}» нет opiu_all.py" if configured
                  else "Не найдена папка opiu с расчётом ОПиУ (WB OPS\\opiu\\opiu_all.py).")
        raise HTTPException(404, detail)
    return project


def sync_opiu_keys(project) -> dict[str, int]:
    """Ключи API кабинетов панели — в конфиги расчёта ОПиУ (их же читают дашборд и номенклатура)."""
    return finance.sync_accounts(project, configured_cabinets())


def start_finance_report(project, mode: str) -> dict[str, object]:
    """Пересборка ОПиУ: сначала ключи API из кабинетов панели — в конфиги расчёта, потом запуск."""
    synced = sync_opiu_keys(project)
    titles = {"wildberries": "WB", "ozon": "Ozon", "yandex": "Я.Маркет"}
    note = ("Ключи API из кабинетов панели: " + ", ".join(f"{titles[k]} — {n}" for k, n in synced.items())
            if synced else "В кабинетах панели нет ключей API — расчёт возьмёт ключи из своих конфигов")
    if synced and not any(synced.values()):
        note += " (сбор статистики и продаж выключен во всех кабинетах)"
    return finance_runner.start(project, finance_settings()["python"] or finance.default_python(), mode, note)


@app.get("/api/finance/settings")
def get_finance_settings() -> dict[str, object]:
    values = finance_settings()
    project = finance.find_project_dir(values["dir"] or None)
    return {**values, "resolved_dir": str(project) if project else None}


@app.put("/api/finance/settings")
async def update_finance_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    folder = str(payload.get("dir") or "").strip().strip('"')
    python = str(payload.get("python") or "").strip().strip('"')
    if folder and finance.find_project_dir(folder) is None:
        raise HTTPException(400, f"В папке «{folder}» нет opiu_all.py")
    draft_store.set_setting("finance_dir", folder)
    draft_store.set_setting("finance_python", python)
    draft_store.audit("finance_settings_updated", details=folder or "auto")
    return get_finance_settings()


@app.get("/api/finance/report")
def finance_report(cabinet: str = "") -> dict[str, object]:
    """cabinet: "" — все кабинеты, «Ozon» — площадка, «Ozon|Еврозоо» — кабинет (ключи из cabinets)."""
    project = finance_project()
    path = finance.latest_report(project)
    if path is None:
        raise HTTPException(404, "Отчёт ещё не собран: нажмите «Собрать из кэша» или «Обновить данные».")
    try:
        data = finance.report_view(finance.read_report(path), cabinet)
    except finance.FinanceError as exc:
        raise HTTPException(500, str(exc)) from exc
    data["cabinets"] = finance.cabinet_options(data["cabinets"], project, configured_cabinets())
    return {**data, "project_dir": str(project)}


@app.get("/api/finance/report/download")
def finance_report_download() -> FileResponse:
    path = finance.latest_report(finance_project())
    if path is None:
        raise HTTPException(404, "Отчёт ещё не собран")
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- номенклатура и себестоимость
nomenclature = Nomenclature(lambda: finance_project(), cabinets=lambda: configured_cabinets(), models=product_models,
                            product_names=lambda: analytics.product_names())


def _nomenclature_guard():
    if finance_runner.status().get("status") == "running":
        raise HTTPException(409, "Сейчас пересобирается ОПиУ — дождитесь окончания и повторите")


def _price_args(value: dict) -> dict:
    out = {}
    for key in ("cost", "prep", "multiplier"):
        if key in value and value[key] not in (None, ""):
            try:
                out[key] = float(str(value[key]).replace(",", ".").replace(" ", ""))
            except ValueError:
                raise HTTPException(400, "Цены — числами")
    if value.get("effective_from"):
        out["effective_from"] = str(value["effective_from"])
    for key in ("brand", "category"):
        if key in value and value[key] is not None:
            out[key] = str(value[key])
    return out


@app.get("/api/nomenclature")
def nomenclature_list() -> dict[str, object]:
    try:
        return {**nomenclature.items(), "finance": finance_runner.status().get("status")}
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/nomenclature/price")
def nomenclature_price(value: dict) -> dict[str, object]:
    _nomenclature_guard()
    article = str(value.get("article") or "").strip()
    args = _price_args(value)
    if not article or not args:
        raise HTTPException(400, "Нужны артикул и хотя бы одно значение")
    try:
        row = nomenclature.update_price(article, **args)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("cost_updated", article, ";".join(f"{k}={v}" for k, v in args.items()))
    return {"saved": row}


@app.put("/api/nomenclature/retro")
def nomenclature_retro(value: dict) -> dict[str, object]:
    """Ретро-бонус производителя по бренду: % от себестоимости проданного (pct=null — убрать)."""
    _nomenclature_guard()
    brand = str(value.get("brand") or "").strip()
    raw = value.get("pct")
    try:
        pct = None if raw in (None, "") else float(str(raw).replace(",", "."))
    except ValueError:
        raise HTTPException(400, "Ретро-бонус — число, например 5")
    try:
        rows = nomenclature.set_retro(brand, pct, value.get("effective_from") or None)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("retro_bonus_updated", brand, f"pct={pct};from={value.get('effective_from') or ''}")
    return {"brand": brand, "rows": rows}


@app.post("/api/nomenclature/bulk")
def nomenclature_bulk(value: dict) -> dict[str, object]:
    _nomenclature_guard()
    articles = [str(a).strip() for a in value.get("articles") or [] if str(a).strip()][:1000]
    args = _price_args(value)
    if not articles or not any(k in args for k in ("cost", "prep", "multiplier", "brand", "category")):
        raise HTTPException(400, "Выберите товары и укажите себестоимость, подготовку или кратность")
    done = 0
    try:
        for article in articles:
            nomenclature.update_price(article, **args)
            done += 1
    except NomenclatureError as exc:
        raise HTTPException(400, f"Сохранено {done} из {len(articles)}: {exc}")
    draft_store.audit("cost_bulk_updated", details=f"count={done};" + ";".join(f"{k}={v}" for k, v in args.items()))
    return {"updated": done}


@app.post("/api/nomenclature/fill-missing-cost")
def nomenclature_fill_missing_cost() -> dict[str, object]:
    """Установить 1000 ₽ только артикулам без действующей себестоимости."""
    _nomenclature_guard()
    try:
        done = nomenclature.fill_missing_cost(1000)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("cost_missing_filled", details=f"count={done};cost=1000")
    return {"updated": done, "cost": 1000}


@app.post("/api/nomenclature/set-prep-all")
def nomenclature_set_prep_all() -> dict[str, object]:
    """Установить подготовку 85 ₽ всем артикулам справочника."""
    _nomenclature_guard()
    try:
        done = nomenclature.set_prep_all(85)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("prep_all_updated", details=f"count={done};prep=85")
    return {"updated": done, "prep": 85}


@app.post("/api/nomenclature/import")
async def nomenclature_import(file: UploadFile = File(...)) -> dict[str, object]:
    """Единый справочник себестоимости и подготовки из Excel/CSV — один на все кабинеты и юрлица."""
    _nomenclature_guard()
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ")
    try:
        result = await asyncio.to_thread(nomenclature.import_file, file.filename or "", content)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("cost_imported", file.filename or "", f"added={result['added']};updated={result['updated']};"
                      f"skipped={len(result['skipped'])}")
    return result


@app.delete("/api/nomenclature/price")
def nomenclature_delete_price(article: str, effective_from: str = Query(..., alias="from")) -> dict[str, object]:
    _nomenclature_guard()
    try:
        nomenclature.delete_price(article, effective_from)
    except NomenclatureError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("cost_price_deleted", article, effective_from)
    return {"deleted": True}


def _opiu_wb_tokens() -> list[tuple[str, str]]:
    try:
        path = finance_project() / "Wildberries" / "wb_accounts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return [(a.get("name") or "WB", a.get("token") or "") for a in data.get("accounts", []) if a.get("enabled", True)]
    except Exception:
        return []


@app.post("/api/nomenclature/refresh")
async def nomenclature_refresh() -> dict[str, object]:
    try:
        sync_opiu_keys(finance_project())
        result = await asyncio.to_thread(nomenclature.refresh_cards, _opiu_wb_tokens())
    except (NomenclatureError, finance.FinanceError) as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("wb_cards_refreshed", details=f"cards={result['cards']}")
    return result


@app.get("/api/nomenclature/export")
def nomenclature_export(format: str = "xlsx"):
    data = nomenclature.items()
    rows = [["Артикул", "Название", "Бренд", "Категория", "WB ID", "Маркетплейсы", "Себестоимость, ₽", "Подготовка, ₽", "Кратность",
             "Действует с", "Будущие цены с", "Продано, шт", "Выручка, ₽", "Остаток WB, шт", "Цена WB, ₽"]]
    for i in data["items"]:
        rows.append([i["article"], i["name"], i["brand"], i["category"], ", ".join(map(str, i["nm_ids"])), ", ".join(i["marketplaces"]),
                     i["cost"], i["prep"], i["multiplier"], i["effective_from"], ", ".join(i["future"]), i["sold"], round(i["revenue"] or 0, 2),
                     i["stock"], i["price"]])
    return _table_download({"Номенклатура": rows}, format, f"nomenclature_{datetime.now():%Y-%m-%d}")


# ---------------------------------------------------------------- дашборд
kpi_dashboard = Dashboard(lambda: finance_project(), lambda: finance_settings()["python"] or finance.default_python(),
                      lambda: nomenclature.read_cost_rows()[1],
                      ad_spend=lambda start, end, cabinet: ads.spend(start, end, cabinet),   # ads создаётся ниже
                      retro_rows=lambda: nomenclature.retro_rows())
ozon_dashboard = Dashboard(lambda: finance_project(), lambda: finance_settings()["python"] or finance.default_python(),
                           lambda: nomenclature.read_cost_rows()[1], retro_rows=lambda: nomenclature.retro_rows(), mp="ozon")
DASHBOARDS = {"wb": kpi_dashboard, "ozon": ozon_dashboard}


def _dashboard(mp: str) -> Dashboard:
    if mp not in DASHBOARDS:
        raise HTTPException(400, "mp: wb или ozon")
    return DASHBOARDS[mp]


@app.get("/api/dashboard")
def dashboard_data(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "",
                   brand: str = "", category: str = "", article: str = "", prev_from: str = "", prev_to: str = "",
                   mp: str = "wb") -> dict[str, object]:
    from datetime import date as _date
    kpi_dashboard = _dashboard(mp)
    try:
        start, end = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
        previous = (_date.fromisoformat(prev_from), _date.fromisoformat(prev_to)) if prev_from and prev_to else None
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    try:
        if kpi_dashboard.stale():
            if not kpi_dashboard.dump_path().exists():
                return {"status": "building", "state": kpi_dashboard.refresh()}
            kpi_dashboard.refresh()                # данные есть — показываем их, свежие соберутся в фоне
        articles = {x.strip().upper().replace(" ", "") for x in article.split(",") if x.strip()} or None
        return {"status": "ok", "state": kpi_dashboard.state,
                **kpi_dashboard.summary(start, end, previous=previous, cabinet=cabinet or None, brand=brand or None, category=category or None, articles=articles)}
    except DashboardError as exc:
        return {"status": "error", "message": str(exc), "state": kpi_dashboard.state}


@app.get("/api/home")
def home_summary(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")) -> dict[str, object]:
    """Главная: магазины (юрлицо × маркетплейс) — факт за период, остатки и прогноз на конец месяца."""
    from datetime import date as _date
    try:
        start, end = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    building = []
    for board in DASHBOARDS.values():
        try:
            if board.stale():
                if not board.dump_path().exists():
                    board.refresh()
                    building.append(board.setup["title"])
                else:
                    board.refresh()            # данные есть — показываем их, свежие соберутся в фоне
        except DashboardError:
            pass                               # маркетплейс не подключён — строк по нему не будет
    try:
        project = finance.find_project_dir(finance_settings()["dir"] or None)
    except Exception:
        project = None
    names = [{"marketplace": b.setup["title"], "cabinet": cab} for b in DASHBOARDS.values() if b.dump_path().exists()
             for cab in _safe_cabinets(b)]
    legal = {(o["marketplace"], o["cabinet"]): o["legal_name"] for o in finance.cabinet_options(names, project, configured_cabinets())}
    try:
        data = home_module.summary(DASHBOARDS, start, end, legal)
    except DashboardError as exc:
        raise HTTPException(400, str(exc))
    connected = {c.marketplace for c in configured_cabinets() if c.enabled}
    data["not_supported"] = [finance.REPORT_MP[m] for m in ("yandex",) if m in connected]
    data["building"] = building
    return data


def _abc_book() -> dict[str, dict]:
    """Справочник себестоимости для ABC: {артикул: {article, name, brand, category, cost}} — действующая цена."""
    from app.services.nomenclature import art_key as _key, num as _num
    rows = defaultdict(list)
    for r in nomenclature.read_cost_rows()[1]:
        if _key(r.get("артикул")):
            rows[_key(r.get("артикул"))].append(r)
    out = {}
    for key, own in rows.items():
        cur = nomenclature.current_price(own) or own[-1]
        pick = lambda col: next((r.get(col) for r in own if r.get(col)), "")  # noqa: E731
        out[key] = {"article": own[0].get("артикул", key), "name": pick("название (справочно)"), "brand": pick("бренд"),
                    "category": pick("категория"), "cost": _num(cur.get("сс_за_ед"))}
    return out


def _abc_data(date_from: str, date_to: str, mp: str) -> dict:
    from datetime import date as _date
    try:
        start, end = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if mp not in ("all", *DASHBOARDS):
        raise HTTPException(400, "mp: all, wb или ozon")
    boards = {k: b for k, b in DASHBOARDS.items() if mp in ("all", k)}
    for board in boards.values():
        try:
            if board.stale():
                board.refresh()                    # свежая выгрузка соберётся в фоне
        except DashboardError:
            pass
    try:
        return abc_module.analysis(boards, start, end, _abc_book())
    except DashboardError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/abc")
def abc_report(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), mp: str = "all") -> dict[str, object]:
    """ABC-анализ продаж и неликвид по артикулам за период (как лист «ABC» таблицы продавца)."""
    return _abc_data(date_from, date_to, mp)


@app.get("/api/abc/export")
def abc_export(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), mp: str = "all", format: str = "xlsx"):
    return _abc_export(_abc_data(date_from, date_to, mp), date_from, date_to, format)


@app.post("/api/abc/export")
def abc_export_selected(value: dict) -> Response:
    """Выгрузка того, что на экране: keys — артикулы после фильтров, в порядке сортировки таблицы."""
    date_from, date_to = str(value.get("from") or ""), str(value.get("to") or "")
    d = _abc_data(date_from, date_to, str(value.get("mp") or "all"))
    keys = [str(k) for k in value.get("keys") or []]
    by_key = {r["key"]: r for r in d["rows"]}
    d["rows"] = [by_key[k] for k in dict.fromkeys(keys) if k in by_key]
    return _abc_export(d, date_from, date_to, str(value.get("format") or "xlsx"), suffix="_filtered")


def _abc_export(d: dict, date_from: str, date_to: str, format: str, suffix: str = "") -> Response:
    rows = [["Бренд", "Категория", "Артикул", "Наименование", "Маркетплейсы", f"Продажи {d['days']} дн, шт", "Процент выкупа, %", f"Выручка {d['days']} дн, ₽",
             "Доля выручки, %", "Накопительная доля, %", "ABC (выручка)", "ABC (оборачиваемость)", "ABC (прибыль)", "ABC", "Группа",
             "Прибыль, ₽", "Остаток, шт", "Себестоимость ед., ₽", "Себестоимость остатка, ₽", "Продажи в день, шт", "Запас, дн",
             "Статус", f"Подсорт ({abc_module.RESTOCK_DAYS} дн), шт"]]
    for r in d["rows"]:
        rows.append([r["brand"], r["category"], r["article"], r["name"], ", ".join(r["marketplaces"]), round(r["sold"], 2),
                     None if r["buyout"] is None else round(r["buyout"], 2), round(r["revenue"], 2), round(r["share"], 2), round(r["cum_share"], 2), r["abc_revenue"], r["abc_turnover"],
                     r["abc_profit"], r["abc"], r["group"], round(r["profit"], 2), round(r["stock"], 2), r["unit_cost"],
                     None if r["stock_cost"] is None else round(r["stock_cost"], 2), round(r["per_day"], 2), r["days_left"],
                     r["status"], r["restock"]])
    return _table_download({"ABC": rows}, format, f"abc_{date_from}_{date_to}{suffix}")


def _safe_cabinets(board) -> list[str]:
    try:
        return list(board.data()["cabinets"])
    except DashboardError:
        return []


@app.get("/api/dashboard/status")
def dashboard_status(mp: str = "wb") -> dict[str, object]:
    board = _dashboard(mp)
    return {**board.state, "ready": board.dump_path().exists() if dashboard_project_ok() else False}


def dashboard_project_ok() -> bool:
    try:
        finance_project()
        return True
    except HTTPException:
        return False


@app.post("/api/dashboard/refresh")
def dashboard_refresh(mp: str = "wb") -> dict[str, object]:
    board = _dashboard(mp)
    try:
        sync_opiu_keys(finance_project())
        return board.refresh()
    except (DashboardError, finance.FinanceError) as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- РНП
def _rnp_tokens() -> list[tuple[str, str, str]]:
    """(кабинет, ключ API, ключ API рекламы) — включённые кабинеты WB панели и кабинеты из расчёта ОПиУ."""
    cabinets = [c for c in configured_cabinets() if c.marketplace == "wildberries"]
    seen = {c.token for c in cabinets if not (c.enabled and c.stats)}   # выключенные и без статистики — не загружаем
    out = []
    for name, token, ads in [(c.name, c.token, c.ads_token) for c in cabinets if c.enabled and c.stats and c.token] + [(n, t, "") for n, t in _opiu_wb_tokens()]:
        if token and token not in seen:
            seen.add(token)
            out.append((name, token, ads or token))
    return out


def _rnp_ozon_accounts() -> list[tuple[str, str, str]]:
    """(кабинет, Client ID, ключ API) — включённые кабинеты Ozon панели со сбором статистики."""
    out, seen = [], set()
    for c in configured_cabinets():
        if c.marketplace == "ozon" and c.enabled and c.stats and c.client_id and c.token and c.client_id not in seen:
            seen.add(c.client_id)
            out.append((c.name, c.client_id, c.token))
    return out


def _rnp_economics_of(board: Dashboard):
    def economics():
        if board.stale() and board.dump_path().exists():
            board.refresh()                          # свежая выгрузка соберётся в фоне
        return board.data()
    return economics


_rnp_economics = _rnp_economics_of(kpi_dashboard)
rnp = RNP(draft_store, _rnp_tokens, _rnp_economics, lambda: nomenclature.read_cost_rows()[1], lambda: nomenclature.retro_rows())
rnp_ozon = OzonRNP(draft_store, _rnp_ozon_accounts, _rnp_economics_of(ozon_dashboard), lambda: nomenclature.read_cost_rows()[1],
                   lambda: nomenclature.retro_rows())
# (РНП, кабинеты с ключами, дашборд); кабинеты — через имя функции, чтобы подмена в тестах действовала
RNPS = {"wb": (rnp, lambda: _rnp_tokens(), kpi_dashboard), "ozon": (rnp_ozon, lambda: _rnp_ozon_accounts(), ozon_dashboard)}
RNP_SYNC_HOURS = 2


def _rnp_mps(mp: str) -> list[str]:
    if mp == "all":
        return list(RNPS)
    if mp not in RNPS:
        raise HTTPException(400, "mp: wb, ozon или all")
    return [mp]


async def rnp_loop() -> None:
    """Каждые 2 часа подтягивать заказы и рекламу из WB и Ozon (если есть ключи)."""
    await asyncio.sleep(20)
    while True:
        for service, tokens, _board in RNPS.values():
            try:
                if tokens():
                    await asyncio.to_thread(service.sync)
            except Exception as exc:
                draft_store.audit("rnp_sync_error", details=f"{service.MP}: {str(exc)[:300]}")
        await asyncio.sleep(RNP_SYNC_HOURS * 3600)


def _rnp_period(date_from: str, date_to: str) -> tuple[str, str]:
    from datetime import date as _date
    try:
        start, end = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if end < start or (end - start).days > 92:
        raise HTTPException(400, "Период — от 1 до 93 дней")
    return start.isoformat(), end.isoformat()


def _rnp_articles(article: str):
    return {x.strip().upper().replace(" ", "") for x in article.split(",") if x.strip()} or None


def _rnp_cabinets(mp: str = "wb") -> list[dict]:
    """Кабинеты РНП для фильтра: маркетплейс, название, юрлицо и все имена кабинета (в панели и в расчёте ОПиУ —
    заказы РНП записаны под первым, выгрузка дашборда — под вторым)."""
    project = finance.find_project_dir(finance_settings()["dir"] or None)
    out = []
    for key in _rnp_mps(mp):
        marketplace = "wildberries" if key == "wb" else key
        panel = [c for c in configured_cabinets() if c.marketplace == marketplace]
        for name, token, third in RNPS[key][1]():
            # у WB кабинет узнаём по токену, у Ozon — по Client ID
            cab = next((c for c in panel if (c.token == token if key == "wb" else c.client_id == token)), None)
            names = {name}
            if cab is not None:
                account = tax_module.opiu_rate(project, cab, len(panel))["account"]
                if account:
                    names.add(account)
            out.append({"mp": key, "name": name, "legal_name": cab.legal_name if cab else "", "names": sorted(names)})
    return out


def _rnp_cabinet_filter(cabinet: str, mp: str = "wb"):
    if not cabinet:
        return None
    names = {n for c in _rnp_cabinets(mp) if c["name"] == cabinet for n in c["names"]}
    return names or {cabinet}


def _rnp_parts(mp: str, start: str, end: str, brand: str, category: str, article: str, cabinet: str, with_cells: bool = False) -> list:
    cabinets = _rnp_cabinet_filter(cabinet, mp)
    return [RNPS[key][0].collect(start, end, brand=brand or None, category=category or None, articles=_rnp_articles(article),
                                 with_cells=with_cells, cabinets=cabinets) for key in _rnp_mps(mp)]


def _rnp_options(mp: str) -> dict:
    """Бренды, категории и артикулы для фильтров — из выгрузок дашбордов выбранных маркетплейсов."""
    out = {"brands": set(), "categories": set(), "articles": {}}
    for key in _rnp_mps(mp):
        try:
            opts = RNPS[key][2].options()
        except Exception:
            continue
        out["brands"] |= set(opts.get("brands") or [])
        out["categories"] |= set(opts.get("categories") or [])
        for a in opts.get("articles") or []:
            out["articles"].setdefault(a["key"], a)
    return {"brands": sorted(out["brands"]), "categories": sorted(out["categories"]),
            "articles": sorted(out["articles"].values(), key=lambda x: x["article"].lower())}


@app.get("/api/rnp")
def rnp_table(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), brand: str = "", category: str = "",
              article: str = "", cabinet: str = "", mp: str = "wb") -> dict[str, object]:
    start, end = _rnp_period(date_from, date_to)
    data = rnp_compose(_rnp_parts(mp, start, end, brand, category, article, cabinet), notes=rnp.notes(start, end))
    data["mp"] = mp
    data["options"] = _rnp_options(mp)
    data["cabinets"] = [{"mp": c["mp"], "name": c["name"], "legal_name": c["legal_name"]} for c in _rnp_cabinets("all")]
    has = {key: bool(RNPS[key][1]()) for key in RNPS}
    data["has_tokens_by_mp"] = has
    data["has_tokens"] = any(has[key] for key in _rnp_mps(mp))
    data["sync_hours"] = RNP_SYNC_HOURS
    return data


@app.get("/api/rnp/heatmap")
def rnp_heatmap(metric: str, date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), brand: str = "",
                category: str = "", article: str = "", cabinet: str = "", mp: str = "wb") -> dict[str, object]:
    start, end = _rnp_period(date_from, date_to)
    if metric not in RNP.HEAT_METRICS:
        raise HTTPException(400, "Неизвестный показатель")
    try:
        return rnp_heatmap_of(rnp_compose(_rnp_parts(mp, start, end, brand, category, article, cabinet, with_cells=True), with_cells=True), metric)
    except RNPError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/rnp/status")
def rnp_status(mp: str = "wb") -> dict[str, object]:
    keys = _rnp_mps(mp)
    states = {key: {**RNPS[key][0].state, "synced_at": draft_store.get_setting(RNPS[key][0].SYNCED_KEY, ""), "has_tokens": bool(RNPS[key][1]())}
              for key in keys}
    main = next((s for s in states.values() if s.get("status") == "running"), states[keys[0]])
    return {**main, "status": "running" if any(s.get("status") == "running" for s in states.values()) else main.get("status"),
            "has_tokens": any(s["has_tokens"] for s in states.values()), "by_mp": states}


@app.post("/api/rnp/sync")
def rnp_sync(days: int = 0, mp: str = "wb") -> dict[str, object]:
    keys = [key for key in _rnp_mps(mp) if RNPS[key][1]()]
    if not keys:
        raise HTTPException(400, "Нет кабинета с API-ключом — добавьте его в «Кабинеты маркетплейсов»" if mp == "all" else
                            f"Нет кабинета {'WB с API-токеном' if mp == 'wb' else 'Ozon с Client ID и API-ключом'} — добавьте его в «Кабинеты маркетплейсов»")
    days = max(0, min(int(days or 0), 90))
    started = False
    for key in keys:
        service = RNPS[key][0]
        if service.state.get("status") != "running":
            threading.Thread(target=service.sync, args=(days or None,), daemon=True).start()
            started = True
    if not started:
        return dict(RNPS[keys[0]][0].state)
    draft_store.audit("rnp_sync", details=f"mp={mp};days={days}")
    return {"status": "running", "message": "Загрузка началась"}


class RNPPlanRequest(BaseModel):
    article: str
    month: str
    orders_per_day: float | None = Field(default=None, ge=0, le=100000)
    margin: float | None = Field(default=None, ge=-100, le=100)
    mp: str = "wb"


@app.put("/api/rnp/plan")
def rnp_set_plan(body: RNPPlanRequest) -> dict[str, object]:
    if body.mp not in RNPS:
        raise HTTPException(400, "План задаётся отдельно для WB и для Ozon — выберите маркетплейс")
    try:
        RNPS[body.mp][0].set_plan(body.article, body.month, body.orders_per_day, body.margin)
    except RNPError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("rnp_plan", body.article, f"{body.mp} {body.month}: {body.orders_per_day} / {body.margin}")
    return {"saved": True}


class RNPNoteRequest(BaseModel):
    day: str
    article: str = ""
    text: str = Field(min_length=1, max_length=500)


@app.post("/api/rnp/notes")
def rnp_add_note(body: RNPNoteRequest) -> dict[str, object]:
    try:
        return rnp.add_note(body.day, body.article, body.text, current_actor.get() or "Владелец")
    except RNPError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/rnp/notes/{note_id}")
def rnp_delete_note(note_id: int) -> dict[str, object]:
    if not rnp.delete_note(note_id):
        raise HTTPException(404, "Заметка не найдена")
    return {"deleted": True}


# ---------------------------------------------------------------- перенос карточек между маркетплейсами
transfer = TransferService(draft_store, configured_cabinets)


def _transfer_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TransferError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/transfer/cabinets")
def transfer_cabinets() -> list[dict[str, object]]:
    return transfer.cabinet_list()


@app.get("/api/transfer/settings")
def transfer_settings_get() -> dict[str, object]:
    return {**transfer.settings(), "certificate_files": [Path(p).name for p in transfer.cert_files()]}


@app.put("/api/transfer/settings")
def transfer_settings_put(body: dict) -> dict[str, object]:
    saved = transfer.update_settings(body)
    draft_store.audit("transfer_settings", details=json.dumps({k: v for k, v in body.items() if k != "certificate"}, ensure_ascii=False)[:300])
    return {**saved, "certificate_files": [Path(p).name for p in transfer.cert_files()]}


@app.post("/api/transfer/certificate-file")
async def transfer_certificate_file(file: UploadFile = File(...)) -> dict[str, object]:
    name = Path(file.filename or "declaration.pdf").name
    if name.lower().rsplit(".", 1)[-1] not in ("pdf", "jpg", "jpeg", "png"):
        raise HTTPException(400, "Нужен файл PDF, JPG или PNG")
    folder = transfer.data_dir / "certificate"
    folder.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ")
    (folder / name).write_bytes(data)
    return {"files": [Path(p).name for p in transfer.cert_files()]}


@app.delete("/api/transfer/certificate-file/{name}")
def transfer_certificate_file_delete(name: str) -> dict[str, object]:
    path = transfer.data_dir / "certificate" / Path(name).name
    if path.exists():
        path.unlink()
    return {"files": [Path(p).name for p in transfer.cert_files()]}


@app.get("/api/transfer/categories")
def transfer_categories(cabinet: str, q: str = Query(..., min_length=2)) -> list[dict[str, object]]:
    return _transfer_call(transfer.search_categories, cabinet, q)


@app.put("/api/transfer/category")
def transfer_set_category(body: dict) -> dict[str, object]:
    if body.get("target_mp") not in ("wildberries", "ozon", "yandex") or not body.get("source_key"):
        raise HTTPException(400, "Нужны target_mp и source_key")
    transfer.set_category(body["target_mp"], body["source_key"], body.get("category"))
    return {"saved": True}


@app.put("/api/transfer/extra")
def transfer_set_extra(body: dict) -> dict[str, object]:
    if body.get("target_mp") not in ("wildberries", "ozon", "yandex") or not body.get("category") or not body.get("attr"):
        raise HTTPException(400, "Нужны target_mp, category и attr")
    value = None if body.get("skip") else str(body.get("value") or "").strip()
    transfer.set_extra(body["target_mp"], body["category"], body["attr"], value)
    return {"saved": True}


@app.post("/api/transfer/runs")
def transfer_run(body: dict) -> dict[str, object]:
    articles = body.get("articles") or []
    if isinstance(articles, str):
        articles = re.split(r"[\n,;\t]+", articles)
    result = _transfer_call(transfer.start, str(body.get("source") or ""), str(body.get("target") or ""), articles,
                            auto=bool(body.get("auto")), author=current_actor.get() or "Владелец")
    draft_store.audit("transfer_started", result["id"], f"{result['source_title']} → {result['target_title']}: {', '.join(result['articles'])[:200]}")
    return result


@app.get("/api/transfer/runs")
def transfer_runs() -> list[dict[str, object]]:
    return transfer.history()


@app.get("/api/transfer/runs/{run_id}")
def transfer_run_get(run_id: str) -> dict[str, object]:
    job = transfer.get(run_id)
    if job is None:
        raise HTTPException(404, "Перенос не найден")
    return transfer.public(job)


@app.post("/api/transfer/runs/{run_id}/upload")
def transfer_upload(run_id: str, body: dict | None = None) -> dict[str, object]:
    body = body or {}
    result = _transfer_call(transfer.upload, run_id, body.get("offer_ids"), bool(body.get("force")))
    draft_store.audit("transfer_upload", run_id, f"force={bool(body.get('force'))}")
    return result


@app.post("/api/transfer/runs/{run_id}/finalize")
def transfer_finalize(run_id: str) -> dict[str, object]:
    return _transfer_call(transfer.finalize, run_id)


@app.post("/api/transfer/runs/{run_id}/certificate")
def transfer_certificate(run_id: str) -> dict[str, object]:
    result = _transfer_call(transfer.bind_certificate, run_id)
    draft_store.audit("transfer_certificate", run_id)
    return result


# ---------------------------------------------------------------- репрайсер WB
def _repricer_catalog() -> dict:
    out = {}
    try:
        for i in nomenclature.items()["items"]:
            mult = i.get("multiplier") or 1
            out[i["key"]] = {"name": i.get("name") or "", "brand": i.get("brand") or "", "category": i.get("category") or "",
                             "stock": i.get("stock"), "cost": i["cost"] * mult if i.get("cost") is not None else None,
                             "prep": (i.get("prep") or 0) * mult}
    except Exception:
        pass
    return out


repricer = Repricer(draft_store, configured_cabinets, lambda: rnp.unit_economics(rnp._dash()), _repricer_catalog)


def _repricer_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except RepricerError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/repricer")
def repricer_table() -> dict[str, object]:
    stale = [c for c in repricer.wb_cabinets() if not (repricer.cache.get(c.id) or {}).get("at")
             or time.time() - repricer.cache[c.id]["at"] > 600]
    if stale:
        repricer.refresh()
    return {"rows": repricer.rows(), "state": repricer.state(), "settings": repricer.settings(), "now": time.time(),
            "cabinets": [{"id": c.id, "name": c.name} for c in repricer.wb_cabinets()]}


@app.post("/api/repricer/refresh")
def repricer_refresh() -> dict[str, object]:
    return {"result": _repricer_call(repricer.refresh, True), "state": repricer.state()}


@app.put("/api/repricer/settings")
def repricer_settings_put(body: dict) -> dict[str, object]:
    saved = _repricer_call(repricer.save_settings, body)
    draft_store.audit("repricer_settings", details=json.dumps(body, ensure_ascii=False)[:300])
    return saved


@app.put("/api/repricer/items")
def repricer_items_put(body: dict) -> dict[str, object]:
    ids = [str(x) for x in body.get("ids") or [] if ":" in str(x)]
    if not ids:
        raise HTTPException(400, "Нет артикулов")
    kw = {}
    if "included" in body:
        kw["included"] = bool(body["included"])
    for k in ("min_price", "max_price"):
        if k in body:
            v = body[k]
            kw[k] = None if v in (None, "") else float(v)
    repricer.set_items(ids, **kw)
    return {"saved": len(ids)}


@app.post("/api/repricer/preview")
def repricer_preview(body: dict) -> dict[str, object]:
    return {"items": _repricer_call(repricer.preview, body.get("changes"), body.get("mass"), body.get("ids"))}


@app.post("/api/repricer/apply")
def repricer_apply(body: dict) -> dict[str, object]:
    changes = [c for c in body.get("changes") or [] if c.get("id")]
    result = _repricer_call(repricer.apply, changes, bool(body.get("confirm")), current_actor.get() or "Владелец",
                            str(body.get("action") or "Изменение цены")[:60])
    draft_store.audit("repricer_apply", str(result["id"]), f"count={len(changes)}")
    return result


@app.get("/api/repricer/ops/{op_id}")
def repricer_op(op_id: int) -> dict[str, object]:
    return _repricer_call(repricer.op, op_id)


@app.get("/api/repricer/history")
def repricer_history(author: str = "", status: str = "", since: str = "") -> list[dict[str, object]]:
    return repricer.history(author, status, since)


@app.get("/api/repricer/items/{item_id}/history")
def repricer_item_history(item_id: str, days: int = 30) -> list[dict[str, object]]:
    if ":" not in item_id:
        raise HTTPException(400, "Неверный артикул")
    return repricer.item_history(item_id, max(1, min(days, 365)))


@app.post("/api/repricer/export")
def repricer_export(body: dict):
    rows = {r["id"]: r for r in repricer.rows()}
    ids = body.get("ids") or list(rows)
    table = [["Магазин", "Артикул МП", "Артикул продавца", "Название", "Бренд", "Цена до скидки", "Скидка, %", "Цена до СПП", "СПП, %",
              "Цена для клиента", "Маржа, %", "Нет в наличии", "Новая цена до СПП", "Новая цена для клиента"]]
    for rid in ids:
        r = rows.get(rid)
        if r:
            table.append([r["cabinet"], r["nm_id"], r["vendor"], r["name"], r["brand"], r["price"], r["discount"], r["price_before_spp"],
                          round(r["spp"], 1) if r["spp"] is not None else "", r["client_wallet"] or "", r.get("margin") if r.get("margin") is not None else "",
                          "" if r["in_stock"] else "да", "", ""])
    return _table_download({"Цены": table}, "xlsx", f"prices_wb_{datetime.now():%Y-%m-%d}")


@app.post("/api/repricer/import")
async def repricer_import(file: UploadFile = File(...)) -> dict[str, object]:
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 10 МБ")
    name = (file.filename or "").lower()
    try:
        if name.endswith(".csv"):
            import csv as _csv
            text = data.decode("utf-8-sig")
            delim = ";" if text.count(";") >= text.count(",") else ","
            table = list(_csv.reader(text.splitlines(), delimiter=delim))
        else:
            import io
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            table = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    except Exception as exc:
        raise HTTPException(400, f"Не удалось прочитать файл: {exc}")
    if not table:
        raise HTTPException(400, "Файл пустой")
    head = [str(h or "").strip().lower() for h in table[0]]
    col = lambda name: head.index(name) if name in head else None        # noqa: E731
    i_shop, i_nm, i_before, i_client = col("магазин"), col("артикул мп"), col("новая цена до спп"), col("новая цена для клиента")
    if i_nm is None or (i_before is None and i_client is None):
        raise HTTPException(400, "Нужны колонки «Магазин», «Артикул МП», «Новая цена до СПП» и/или «Новая цена для клиента»")
    names = {c.name.strip().lower(): c.id for c in repricer.wb_cabinets()}
    rows = repricer.rows()
    changes, skipped = [], 0
    for line in table[1:]:
        cell = lambda i: (line[i] if i is not None and i < len(line) else None)        # noqa: E731,B023
        nm = str(cell(i_nm) or "").strip().split(".")[0]
        before, client = cell(i_before), cell(i_client)
        if not nm or (before in (None, "") and client in (None, "")):
            skipped += 1
            continue
        shop = str(cell(i_shop) or "").strip().lower()
        cid = names.get(shop)
        match = [r for r in rows if str(r["nm_id"]) == nm and (cid is None or r["cabinet_id"] == cid)]
        if not match:
            changes.append({"id": f"?:{nm}", "error": "артикул не найден"})
            continue
        num = lambda v: float(str(v).replace(" ", "").replace(",", ".")) if v not in (None, "") else None   # noqa: E731
        try:
            ch = {"id": match[0]["id"]}
            if before not in (None, ""):
                ch["new_price"] = num(before)
            else:
                ch["new_client"] = num(client)
            changes.append(ch)
        except ValueError:
            changes.append({"id": match[0]["id"], "error": "цена — не число"})
    if len(changes) > 1000:
        raise HTTPException(400, "За раз можно загрузить до 1000 цен")
    good = [c for c in changes if not c.get("error")]
    items = _repricer_call(repricer.preview, good) + [{"id": c["id"], "errors": [c["error"]], "warnings": [], "nm_id": c["id"].split(":")[-1]}
                                                      for c in changes if c.get("error")]
    return {"items": items, "skipped": skipped}


# ---------------------------------------------------------------- остатки FBS
def _stocks_catalog() -> dict:
    out = {}
    try:
        for i in nomenclature.items()["items"]:
            out[i["key"]] = {"name": i.get("name") or "", "brand": i.get("brand") or "", "category": i.get("category") or "", "wb_stock": i.get("stock")}
    except Exception:
        pass
    return out


stocks = Stocks(draft_store, configured_cabinets, _stocks_catalog)


async def _stocks_notify(result: dict) -> int:
    cfg = notifier.config()
    if not result.get("messages") or not cfg["events"].get("stocks_low", True):
        return 0
    sent = 0
    for subject, text in result["messages"]:
        res = await notifier.send(subject, text, cfg)
        sent += int(any(res.values()))
    return sent


async def stocks_loop() -> None:
    """Проверять остатки FBS по расписанию и присылать оповещения о заканчивающихся размерах."""
    await asyncio.sleep(45)
    while True:
        cfg = stocks.settings()
        try:
            if cfg["monitor"] and stocks.wb_cabinets():
                await asyncio.to_thread(stocks.refresh)
                await _stocks_notify(await asyncio.to_thread(stocks.check_alerts))
        except Exception as exc:
            draft_store.audit("stocks_monitor_error", details=str(exc)[:300])
        await asyncio.sleep(max(5, int(cfg.get("interval_min") or 30)) * 60)


def _stocks_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StocksError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/stocks")
async def stocks_table() -> dict[str, object]:
    stale = [c.id for c in stocks.wb_cabinets() if not (stocks.state.get(c.id) or {}).get("at") or time.time() - stocks.state[c.id]["at"] > 600]
    if stale:
        await asyncio.to_thread(stocks.refresh, stale)
    ncfg = notifier.config()
    return {"rows": await asyncio.to_thread(stocks.rows), "state": stocks.state, "settings": stocks.settings(), "now": time.time(),
            "cabinets": [{"id": c.id, "name": c.name} for c in stocks.wb_cabinets()],
            "notify": {"telegram": ncfg["telegram"]["enabled"], "email": ncfg["email"]["enabled"], "event": ncfg["events"].get("stocks_low", True)},
            "orders_sync": {"status": rnp.state.get("status"), "message": rnp.state.get("message", ""), "has_tokens": bool(_rnp_tokens())}}


@app.post("/api/stocks/refresh")
async def stocks_refresh(body: dict | None = None) -> dict[str, object]:
    return {"result": await asyncio.to_thread(stocks.refresh, None, bool((body or {}).get("reload_cards"))), "state": stocks.state}


@app.put("/api/stocks")
async def stocks_update(body: dict) -> dict[str, object]:
    changes = [c for c in body.get("changes") or [] if isinstance(c, dict)]
    if len(changes) > 5000:
        raise HTTPException(400, "Слишком много изменений за раз")
    result = await asyncio.to_thread(_stocks_call, stocks.update, changes, current_actor.get() or "Владелец")
    draft_store.audit("fbs_stocks_updated", details=f"ok={result['ok']};errors={len(result['errors'])}")
    try:
        await _stocks_notify(await asyncio.to_thread(stocks.check_alerts))
    except Exception:
        pass
    return result


@app.put("/api/stocks/watch")
def stocks_watch(body: dict) -> dict[str, object]:
    ids = [str(x) for x in body.get("ids") or [] if str(x).count(":") == 2]
    if not ids:
        raise HTTPException(400, "Нет размеров")
    kw = {}
    if "threshold" in body:
        v = body["threshold"]
        if v not in (None, "") and not (0 <= int(v) <= 10000):
            raise HTTPException(400, "Порог — от 0 до 10000")
        kw["threshold"] = None if v in (None, "") else int(v)
    if "muted" in body:
        kw["muted"] = bool(body["muted"])
    stocks.set_watch(ids, **kw)
    return {"saved": len(ids)}


@app.put("/api/stocks/archive")
def stocks_archive(body: dict) -> dict[str, object]:
    keys = [str(k) for k in body.get("keys") or [] if str(k).count(":") == 1 and str(k).rsplit(":", 1)[1].isdigit()]
    if not keys:
        raise HTTPException(400, "Нет артикулов")
    stocks.set_archive(keys, bool(body.get("archived", True)), current_actor.get() or "Владелец")
    draft_store.audit("fbs_archive", details=f"{'в архив' if body.get('archived', True) else 'из архива'}: {', '.join(keys)[:200]}")
    return {"saved": len(keys)}


@app.put("/api/stocks/settings")
def stocks_settings_put(body: dict) -> dict[str, object]:
    return _stocks_call(stocks.save_settings, body)


@app.get("/api/stocks/history")
def stocks_history(days: int = 30, item: str = "") -> list[dict[str, object]]:
    if item and item.count(":") != 2:
        raise HTTPException(400, "Неверный размер")
    return stocks.history(max(1, min(days, 365)), item)


@app.post("/api/stocks/check")
async def stocks_check() -> dict[str, object]:
    """Проверить сейчас и прислать список всех размеров ниже порога (как тест оповещения)."""
    ncfg = notifier.config()
    if not (ncfg["telegram"]["enabled"] or ncfg["email"]["enabled"]):
        raise HTTPException(400, "Сначала подключите Telegram в «Настройках уведомлений»")
    await asyncio.to_thread(stocks.refresh)
    result = await asyncio.to_thread(stocks.check_alerts, True)
    if not result["messages"]:
        return {"sent": 0, "message": "Все отслеживаемые размеры выше порога — оповещать не о чем"}
    sent = 0
    for subject, text in result["messages"]:
        res = await notifier.send(subject, text, ncfg)
        sent += int(any(res.values()))
    return {"sent": sent, "low": result["low"], "zero": result["zero"], "message": "Отправлено" if sent else "Не удалось отправить — проверьте настройки Telegram"}


# ---------------------------------------------------------------- аналитика FBS
fbs = FbsAnalytics(draft_store, configured_cabinets, _stocks_catalog)
fbs_ozon = FbsOzon(draft_store, configured_cabinets, _stocks_catalog)


async def fbs_loop() -> None:
    """Раз в 30 минут подтягивать сборочные задания WB (статусы, поставки, выкупы) и отправления FBS Ozon."""
    await asyncio.sleep(90)
    while True:
        try:
            if fbs.wb_cabinets():
                await asyncio.to_thread(fbs.sync)
        except Exception as exc:
            draft_store.audit("fbs_sync_error", details=str(exc)[:300])
        try:
            if fbs_ozon.ozon_cabinets():
                await asyncio.to_thread(fbs_ozon.sync)
        except Exception as exc:
            draft_store.audit("fbs_ozon_sync_error", details=str(exc)[:300])
        await asyncio.sleep(30 * 60)


def _fbs_args(date_from: str, date_to: str, cabinet: str, warehouse: str, q: str, brand: str, category: str):
    from datetime import date as _date
    try:
        a, b = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if b < a or (b - a).days > 120:
        raise HTTPException(400, "Период — от 1 до 120 дней")
    return a.isoformat(), b.isoformat(), {"cabinet": cabinet, "warehouse": warehouse, "q": q, "brand": brand, "category": category}


@app.get("/api/fbs-analytics")
async def fbs_report(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                     q: str = "", brand: str = "", category: str = "", prev_from: str = "", prev_to: str = "") -> dict[str, object]:
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    prev = (prev_from, prev_to) if prev_from and prev_to else None
    if not draft_store.get_setting("fbs_analytics_synced_at", "") and fbs.state["status"] != "running" and fbs.wb_cabinets():
        threading.Thread(target=fbs.sync, daemon=True).start()
    return await asyncio.to_thread(fbs.report, a, b, f, prev)


@app.get("/api/fbs-analytics/orders")
async def fbs_feed(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                   q: str = "", brand: str = "", category: str = "", status: str = "", sort: str = "created_at", desc: bool = True,
                   limit: int = 100, offset: int = 0) -> dict[str, object]:
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    return await asyncio.to_thread(fbs.feed, a, b, f, status, sort, desc, max(1, min(limit, 500)), max(0, offset))


@app.get("/api/fbs-analytics/orders.csv")
async def fbs_feed_csv(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                       q: str = "", brand: str = "", category: str = "", status: str = ""):
    from app.services.fbs_analytics import STATUS_TITLES
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    data = await asyncio.to_thread(fbs.feed, a, b, f, status, "created_at", True, 100000, 0)
    table = [["Дата заказа", "Задание", "Кабинет", "Склад", "Артикул", "WB ID", "Бренд", "Сумма, ₽", "Поставка", "Статус", "Статус WB",
              "Приёмка WB (СЦ)", "Часов до СЦ", "Выкуп", "Дней от СЦ до выкупа", "Комиссия, п.п.", "Скидка/штраф, ₽"]]
    for r in data["items"]:
        table.append([r["created_at"], r["id"], r["cabinet"], r["warehouse"], r["article"], r["nm_id"], r["brand"], r["price"], r["supply_id"],
                      STATUS_TITLES.get(r["status"], r["status"]), r["wb_status"], r["scan_dt"] or "", r["hours_to_sc"] if r["hours_to_sc"] is not None else "",
                      r["sale_dt"] or "", r["days_to_buyout"] if r["days_to_buyout"] is not None else "",
                      r["effect_pct"] if r["effect_pct"] is not None else "", r["effect_rub"] if r["effect_rub"] is not None else ""])
    return _table_download({"FBS": table}, "csv", f"fbs_{a}_{b}")


@app.post("/api/fbs-analytics/sync")
def fbs_sync() -> dict[str, object]:
    if not fbs.wb_cabinets():
        raise HTTPException(400, "Нет кабинета WB с ключом API")
    if fbs.state["status"] != "running":
        threading.Thread(target=fbs.sync, daemon=True).start()
    return {"status": "running"}


@app.get("/api/fbs-analytics/status")
def fbs_status() -> dict[str, object]:
    return {**fbs.state, "synced_at": draft_store.get_setting("fbs_analytics_synced_at", "")}


@app.get("/api/fbs-analytics/ozon")
async def fbs_ozon_report(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                          q: str = "", brand: str = "", category: str = "", prev_from: str = "", prev_to: str = "") -> dict[str, object]:
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    prev = (prev_from, prev_to) if prev_from and prev_to else None
    if not draft_store.get_setting("fbs_ozon_synced_at", "") and fbs_ozon.state["status"] != "running" and fbs_ozon.ozon_cabinets():
        threading.Thread(target=fbs_ozon.sync, daemon=True).start()
    return await asyncio.to_thread(fbs_ozon.report, a, b, f, prev)


@app.get("/api/fbs-analytics/ozon/orders")
async def fbs_ozon_feed(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                        q: str = "", brand: str = "", category: str = "", status: str = "", sort: str = "created_at", desc: bool = True,
                        limit: int = 100, offset: int = 0) -> dict[str, object]:
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    return await asyncio.to_thread(fbs_ozon.feed, a, b, f, status, sort, desc, max(1, min(limit, 500)), max(0, offset))


@app.get("/api/fbs-analytics/ozon/orders.csv")
async def fbs_ozon_feed_csv(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", warehouse: str = "",
                            q: str = "", brand: str = "", category: str = "", status: str = ""):
    from app.services.fbs_ozon import STATUS_TITLES
    a, b, f = _fbs_args(date_from, date_to, cabinet, warehouse, q, brand, category)
    data = await asyncio.to_thread(fbs_ozon.feed, a, b, f, status, "created_at", True, 100000, 0)
    table = [["Дата заказа", "Отправление", "Кабинет", "Склад", "Артикул", "SKU", "Бренд", "Шт", "Сумма, ₽", "Статус", "Статус Ozon", "Причина отмены",
              "Срок отгрузки", "Передано в доставку", "Часов до передачи", "Опоздание, ч", "Просрочено", "Скорость отгрузки, %", "Скидка/наценка, ₽", "Штрафы, ₽"]]
    for r in data["items"]:
        table.append([r["created_at"], r["posting"], r["cabinet"], r["warehouse"], r["article"], r["sku"], r["brand"], r["qty"], r["price"],
                      STATUS_TITLES.get(r["status"], r["status"]), r["ozon_status"], r["cancel_reason"], r["shipment_date"] or "", r["shipped_at"] or "",
                      r["hours_to_ship"] if r["hours_to_ship"] is not None else "", r["late_hours"] if r["late_hours"] is not None else "",
                      "да" if r["overdue"] else "", r["effect_pct"] if r["effect_pct"] is not None else "",
                      r["effect_rub"] if r["effect_rub"] is not None else "", r["fine_rub"] or ""])
    return _table_download({"FBS Ozon": table}, "csv", f"fbs_ozon_{a}_{b}")


@app.post("/api/fbs-analytics/ozon/sync")
def fbs_ozon_sync() -> dict[str, object]:
    if not fbs_ozon.ozon_cabinets():
        raise HTTPException(400, "Нет кабинета Ozon с Client ID и API-ключом")
    if fbs_ozon.state["status"] != "running":
        threading.Thread(target=fbs_ozon.sync, daemon=True).start()
    return {"status": "running"}


@app.get("/api/fbs-analytics/ozon/status")
def fbs_ozon_status() -> dict[str, object]:
    return {**fbs_ozon.state, "synced_at": draft_store.get_setting("fbs_ozon_synced_at", "")}


# ---------------------------------------------------------------- аналитика штрафов
def _penalties_cache_dir():
    try:
        return finance_project() / "Wildberries" / "cache"
    except HTTPException:
        return None


penalties = Penalties(draft_store, _penalties_cache_dir, configured_cabinets)


async def penalties_loop() -> None:
    """Переиндексировать штрафы, когда расчёт ОПиУ скачал свежие отчёты реализации."""
    await asyncio.sleep(120)
    while True:
        try:
            if penalties.stale() and penalties._sources():
                await asyncio.to_thread(penalties.sync)
        except Exception as exc:
            draft_store.audit("penalties_sync_error", details=str(exc)[:300])
        await asyncio.sleep(10 * 60)


def _pen_args(date_from: str, date_to: str, cabinet: str, q: str, brand: str, category: str, group: str, reason: str, warehouse: str = ""):
    from datetime import date as _date
    try:
        a, b = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if b < a or (b - a).days > 400:
        raise HTTPException(400, "Период — от 1 до 400 дней")
    return a.isoformat(), b.isoformat(), {"cabinet": cabinet, "q": q, "brand": brand, "category": category, "group": group, "reason": reason, "warehouse": warehouse}


@app.get("/api/penalties")
async def penalties_report(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", q: str = "",
                           brand: str = "", category: str = "", group: str = "", reason: str = "", warehouse: str = "", prev_from: str = "", prev_to: str = "") -> dict[str, object]:
    a, b, f = _pen_args(date_from, date_to, cabinet, q, brand, category, group, reason, warehouse)
    if penalties.state["status"] != "running" and penalties.stale() and penalties._sources():
        await asyncio.to_thread(penalties.sync)
    data = await asyncio.to_thread(penalties.report, a, b, f, (prev_from, prev_to) if prev_from and prev_to else None)
    data["download"] = finance_runner.status()["status"]
    return data


@app.get("/api/penalties/items")
async def penalties_feed(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", q: str = "",
                         brand: str = "", category: str = "", group: str = "", reason: str = "", warehouse: str = "", sort: str = "day", desc: bool = True,
                         limit: int = 100, offset: int = 0) -> dict[str, object]:
    a, b, f = _pen_args(date_from, date_to, cabinet, q, brand, category, group, reason, warehouse)
    return await asyncio.to_thread(penalties.feed, a, b, f, sort, desc, max(1, min(limit, 500)), max(0, offset))


@app.get("/api/penalties/items.csv")
async def penalties_csv(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", q: str = "",
                        brand: str = "", category: str = "", group: str = "", reason: str = "", warehouse: str = ""):
    a, b, f = _pen_args(date_from, date_to, cabinet, q, brand, category, group, reason, warehouse)
    data = await asyncio.to_thread(penalties.feed, a, b, f, "day", True, 100000, 0)
    table = [["Дата", "Кабинет", "Причина", "Группа", "Сумма, ₽", "Артикул", "WB ID", "Бренд", "Предмет", "srid", "Отчёт WB",
              "Задание FBS", "Склад", "Заказ создан", "Статус WB"]]
    for r in data["items"]:
        fb = r.get("fbs") or {}
        table.append([r["day"], r["account"], r["reason"], r["group_title"], r["amount"], r["article"], r["nm_id"], r["brand"], r["subject"],
                      r["srid"], r["report_id"], fb.get("order_id", ""), fb.get("warehouse", ""), fb.get("created_at", ""), fb.get("wb_status", "")])
    return _table_download({"Штрафы": table}, "csv", f"penalties_{a}_{b}")


@app.post("/api/penalties/sync")
async def penalties_sync(request: Request) -> dict[str, object]:
    """download=true — скачать свежие отчёты реализации через расчёт ОПиУ, иначе только перечитать кэш."""
    payload = await request.json() if await request.body() else {}
    if (payload or {}).get("download"):
        project = finance_project()
        try:
            start_finance_report(project, "full")
        except finance.FinanceError as exc:
            raise HTTPException(409, str(exc)) from exc
        draft_store.audit("finance_update_started", details="full (penalties)")
        return {"status": "downloading"}
    if penalties.state["status"] != "running":
        threading.Thread(target=penalties.sync, kwargs={"force": True}, daemon=True).start()
    return {"status": "running"}


@app.get("/api/penalties/status")
def penalties_status() -> dict[str, object]:
    return {**penalties.state, "download": finance_runner.status()["status"], "stale": penalties.stale(),
            "synced_at": draft_store.get_setting("penalties_synced_at", "")}


# ---------------------------------------------------------------- аналитика рекламы
ads = AdsAnalytics(draft_store, _rnp_tokens, _stocks_catalog)


async def ads_loop() -> None:
    """Раз в час подтягивать кампании, статистику, поисковые кластеры и списания."""
    await asyncio.sleep(150)
    while True:
        try:
            if _rnp_tokens():
                await asyncio.to_thread(ads.sync)
        except Exception as exc:
            draft_store.audit("ads_sync_error", details=str(exc)[:300])
        await asyncio.sleep(60 * 60)


def _ads_args(date_from: str, date_to: str, **f):
    from datetime import date as _date
    try:
        a, b = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД")
    if b < a or (b - a).days > 400:
        raise HTTPException(400, "Период — от 1 до 400 дней")
    return a.isoformat(), b.isoformat(), f


@app.get("/api/ads")
async def ads_report(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), cabinet: str = "", campaign: str = "",
                     status: str = "", payment: str = "", brand: str = "", category: str = "", q: str = "",
                     prev_from: str = "", prev_to: str = "") -> dict[str, object]:
    a, b, f = _ads_args(date_from, date_to, cabinet=cabinet, campaign=campaign, status=status, payment=payment, brand=brand, category=category, q=q)
    if not draft_store.get_setting("ads_synced_at", "") and ads.state["status"] != "running" and _rnp_tokens():
        threading.Thread(target=ads.sync, daemon=True).start()
    return await asyncio.to_thread(ads.report, a, b, f, (prev_from, prev_to) if prev_from and prev_to else None)


@app.get("/api/ads/export.csv")
async def ads_export(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"), kind: str = "campaigns", cabinet: str = "",
                     campaign: str = "", status: str = "", payment: str = "", brand: str = "", category: str = "", q: str = ""):
    a, b, f = _ads_args(date_from, date_to, cabinet=cabinet, campaign=campaign, status=status, payment=payment, brand=brand, category=category, q=q)
    data = await asyncio.to_thread(ads.report, a, b, f, None)
    cols = [("views", "Показы"), ("clicks", "Клики"), ("ctr", "CTR, %"), ("cpc", "CPC, ₽"), ("cpm", "CPM, ₽"), ("atbs", "Корзины"), ("orders", "Заказы"),
            ("cr", "CR клик→заказ, %"), ("spend", "Расход, ₽"), ("cpo", "Цена заказа, ₽"), ("sum_price", "Сумма рекл. заказов, ₽"), ("drr", "ДРР рекл., %")]
    if kind == "articles":
        head, rows, key = ["Артикул", "WB ID", "Бренд", "Предмет"], data["articles"], lambda r: [r["article"], r["nm_id"], r["brand"], r["subject"]]
        cols += [("all_orders", "Все заказы"), ("all_orders_sum", "Сумма всех заказов, ₽"), ("drr_total", "ДРР общий, %")]
    elif kind == "clusters":
        head, rows, key = ["Запрос"], data["clusters"], lambda r: [r["query"]]
        cols += [("avg_pos", "Ср. позиция")]
    else:
        head, rows, key = ["Кампания", "ID", "Кабинет", "Статус", "Оплата"], data["campaigns"], lambda r: [r["name"], r["id"], r["account"], r["status_title"], r["payment_type"]]
        cols += [("budget", "Бюджет, ₽")]
    table = [head + [t for _k, t in cols]] + [key(r) + [r.get(k) if r.get(k) is not None else "" for k, _t in cols] for r in rows]
    return _table_download({"Реклама": table}, "csv", f"ads_{kind}_{a}_{b}")


@app.post("/api/ads/sync")
async def ads_sync(request: Request) -> dict[str, object]:
    payload = await request.json() if await request.body() else {}
    if not _rnp_tokens():
        raise HTTPException(400, "Нет кабинета WB с ключом API")
    if ads.state["status"] != "running":
        threading.Thread(target=ads.sync, kwargs={"full": bool((payload or {}).get("full"))}, daemon=True).start()
    return {"status": "running"}


@app.get("/api/ads/status")
def ads_status() -> dict[str, object]:
    return {**ads.state, "synced_at": draft_store.get_setting("ads_synced_at", "")}


# ---------------------------------------------------------------- план-факт
planfact = PlanFact(draft_store, rnp)


async def planfact_loop() -> None:
    """Каждое утро (в выбранный час по Москве) — сводка план-факта в Telegram, если включено."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    await asyncio.sleep(180)
    while True:
        try:
            cfg = planfact.settings()
            now = _dt.now(_tz(_td(hours=3)))
            if cfg["notify"] and now.hour >= cfg["hour"] and draft_store.get_setting("planfact_sent_day", "") != now.date().isoformat():
                msg = await asyncio.to_thread(planfact.digest)
                draft_store.set_setting("planfact_sent_day", now.date().isoformat())
                if msg:
                    await notifier.send(msg[0], msg[1], notifier.config())
        except Exception as exc:
            draft_store.audit("planfact_notify_error", details=str(exc)[:300])
        await asyncio.sleep(15 * 60)


@app.get("/api/planfact")
async def planfact_report(month: str = "", brand: str = "", category: str = "") -> dict[str, object]:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    month = month or _dt.now(_tz(_td(hours=3))).strftime("%Y-%m")
    try:
        data = await asyncio.to_thread(planfact.report, month, brand or None, category or None)
    except PlanFactError as exc:
        raise HTTPException(400, str(exc))
    try:
        data["options"] = kpi_dashboard.options()
    except Exception:
        data["options"] = {}
    data["has_tokens"] = bool(_rnp_tokens())
    return data


class PlanFactPlanItem(BaseModel):
    scope: str = ""
    metric: str
    value: float | None = Field(default=None, ge=-1e12, le=1e12)


class PlanFactPlanRequest(BaseModel):
    month: str
    items: list[PlanFactPlanItem] = Field(min_length=1, max_length=5000)


@app.put("/api/planfact/plan")
def planfact_set_plan(body: PlanFactPlanRequest) -> dict[str, object]:
    try:
        for it in body.items:
            planfact.set_plan(body.month, it.scope, it.metric, it.value)
    except PlanFactError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("planfact_plan", details=f"{body.month}: {len(body.items)}")
    return {"saved": len(body.items)}


class PlanFactAutofill(BaseModel):
    month: str
    growth: float = Field(default=0, ge=-90, le=500)
    target: str = "all"
    overwrite: bool = False


@app.post("/api/planfact/autofill")
def planfact_autofill(body: PlanFactAutofill) -> dict[str, object]:
    if body.target not in ("all", "total", "articles"):
        raise HTTPException(400, "target: all, total или articles")
    try:
        result = planfact.autofill(body.month, body.growth, body.target, body.overwrite)
    except PlanFactError as exc:
        raise HTTPException(400, str(exc))
    draft_store.audit("planfact_autofill", details=f"{body.month}: {result['written']}")
    return result


class PlanFactSettings(BaseModel):
    notify: bool = False
    hour: int = Field(default=10, ge=0, le=23)


@app.put("/api/planfact/settings")
def planfact_settings(body: PlanFactSettings) -> dict[str, object]:
    return planfact.save_settings(body.notify, body.hour)


@app.post("/api/planfact/test")
async def planfact_test() -> dict[str, object]:
    ncfg = notifier.config()
    if not (ncfg["telegram"]["enabled"] or ncfg["email"]["enabled"]):
        raise HTTPException(400, "Сначала подключите Telegram в «Настройках уведомлений»")
    msg = await asyncio.to_thread(planfact.digest)
    if not msg:
        raise HTTPException(400, "Сначала задайте планы на текущий месяц")
    res = await notifier.send(msg[0], msg[1], ncfg)
    return {"sent": any(res.values())}


# ---------------------------------------------------------------- A/B-тесты главного фото
abtests = ABTests(draft_store, _rnp_tokens, Path(__file__).resolve().parent.parent / "data" / "ab")
ab_hypotheses = ABHypotheses(draft_store, _rnp_tokens, card_loader=abtests.card, runner=abtests)


def _campaign_status(advert_id):
    with draft_store._connect() as c:
        try:
            r = c.execute("SELECT status FROM ads_campaigns WHERE id=?", (advert_id,)).fetchone()
        except Exception:
            return None
    return r[0] if r else None


async def ab_loop() -> None:
    """Каждые 10 минут: статистика вариантов, вердикт, смена главного фото по расписанию."""
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(abtests.tick, _campaign_status)
        except Exception as exc:
            draft_store.audit("ab_tick_error", details=str(exc)[:300])
        await asyncio.sleep(10 * 60)


def _ab(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ABError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/ab")
def ab_list() -> dict[str, object]:
    with draft_store._connect() as c:
        try:
            camps = [{"id": r[0], "name": r[1], "status": r[2], "payment_type": r[3], "account": r[4], "nms": [n.get("nm_id") for n in json.loads(r[5] or "[]")]}
                     for r in c.execute("SELECT id, name, status, payment_type, account, nms FROM ads_campaigns WHERE status IN (9, 11) ORDER BY account, name")]
        except Exception:
            camps = []
    return {"tests": abtests.list(), "campaigns": camps, "has_tokens": bool(_rnp_tokens()), "ads_synced_at": draft_store.get_setting("ads_synced_at", "")}


@app.get("/api/ab/card")
async def ab_card(nm: int) -> dict[str, object]:
    return await asyncio.to_thread(_ab, abtests.card, nm)


class ABCreate(BaseModel):
    nm_id: int
    advert_id: int
    photos: list[int] = Field(default_factory=list, max_length=30)
    mode: str = "clusters"
    interval_hours: float = Field(default=6, ge=2, le=72)
    views_per_step: float = Field(default=100, ge=100, le=1000000)
    clusters: list[str] = Field(default_factory=lambda: ["Джинсы мужские классические", "Джинсы мужские прямые", "Джинсы мужские прямые классические"], max_length=50)
    metric: str = "ctr"
    max_days: float = Field(default=14, ge=1, le=90)


@app.post("/api/ab/tests")
async def ab_create(body: ABCreate) -> dict[str, object]:
    result = await asyncio.to_thread(_ab, abtests.create, body.nm_id, body.advert_id, body.photos, body.mode, body.interval_hours, body.views_per_step,
                                     body.metric, body.max_days, body.clusters)
    draft_store.audit("ab_create", str(body.nm_id), f"test={result['id']}")
    return result


@app.get("/api/ab/tests/{test_id}")
def ab_detail(test_id: int) -> dict[str, object]:
    return _ab(abtests.detail, test_id)


@app.post("/api/ab/tests/{test_id}/upload")
async def ab_upload(test_id: int, file: UploadFile = File(...)) -> dict[str, object]:
    content = await file.read()
    return _ab(abtests.add_upload, test_id, file.filename or "photo.jpg", content)


@app.delete("/api/ab/tests/{test_id}/variants/{variant_id}")
def ab_remove_variant(test_id: int, variant_id: int) -> dict[str, object]:
    return _ab(abtests.remove_variant, test_id, variant_id)


@app.get("/api/ab/file/{variant_id}")
def ab_file(variant_id: int):
    with draft_store._connect() as c:
        r = c.execute("SELECT local_file FROM ab_variants WHERE id=?", (variant_id,)).fetchone()
    path = Path(r[0]) if r and r[0] else None
    if not path or not path.exists() or abtests.files_dir.resolve() not in path.resolve().parents:
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path)


@app.post("/api/ab/tests/{test_id}/start")
async def ab_start(test_id: int) -> dict[str, object]:
    result = await asyncio.to_thread(_ab, abtests.start, test_id)
    draft_store.audit("ab_start", details=f"test={test_id}")
    return result


@app.post("/api/ab/tests/{test_id}/pause")
async def ab_pause(test_id: int) -> dict[str, object]:
    return await asyncio.to_thread(_ab, abtests.pause, test_id)


@app.post("/api/ab/tests/{test_id}/refresh")
async def ab_refresh(test_id: int) -> dict[str, object]:
    result = await asyncio.to_thread(_ab, abtests.refresh, test_id)
    draft_store.audit("ab_refresh", details=f"test={test_id}")
    return result


@app.post("/api/ab/tests/{test_id}/variants/{variant_id}/exclude")
async def ab_exclude(test_id: int, variant_id: int) -> dict[str, object]:
    return await asyncio.to_thread(_ab, abtests.exclude, test_id, variant_id)


class ABFinish(BaseModel):
    winner_id: int | None = None
    remove_uploaded: bool = True


@app.post("/api/ab/tests/{test_id}/finish")
async def ab_finish(test_id: int, body: ABFinish) -> dict[str, object]:
    result = await asyncio.to_thread(_ab, abtests.finish, test_id, body.winner_id, body.remove_uploaded)
    draft_store.audit("ab_finish", details=f"test={test_id}; winner={result.get('winner')}")
    return result


@app.delete("/api/ab/tests/{test_id}")
def ab_delete(test_id: int) -> dict[str, object]:
    _ab(abtests.delete, test_id)
    return {"deleted": True}


# ---------------------------------------------------------------- A/B-тесты Гипотезы (изолированный безопасный workflow)
def _abh(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ABHypothesisError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/ab-hypotheses")
def ab_hypotheses_list() -> dict[str, object]:
    with draft_store._connect() as c:
        try:
            campaigns = [{"id": r[0], "name": r[1], "status": r[2], "payment_type": r[3], "account": r[4],
                          "nms": [n.get("nm_id") for n in json.loads(r[5] or "[]") if isinstance(n, dict)]}
                         for r in c.execute("SELECT id,name,status,payment_type,account,nms FROM ads_campaigns ORDER BY account,name")]
        except Exception:
            campaigns = []
    cabinets = [{"id": c.id, "name": c.name} for c in configured_cabinets() if c.marketplace == "wildberries" and c.enabled]
    return {"hypotheses": ab_hypotheses.list(), "campaigns": campaigns, "cabinets": cabinets,
            "metrics": list(PRIMARY_METRICS), "has_tokens": bool(_rnp_tokens())}


@app.get("/api/ab-hypotheses/preflight")
def ab_hypotheses_preflight(nm: int, advert: int, account: str = "") -> dict[str, object]:
    return _abh(ab_hypotheses.preflight, nm, advert, account or None)


@app.get("/api/ab-hypotheses/card")
def ab_hypotheses_card(nm: int, account: str = "") -> dict[str, object]:
    return _abh(ab_hypotheses._card, nm, account or None)


class ABHypothesisCreate(BaseModel):
    nm_id: int
    advert_id: int
    hypothesis: str = Field(min_length=3, max_length=2000)
    owner: str = Field(min_length=1, max_length=200)
    account: str = ""
    primary_metric: str = "ctr"
    guardrails: dict[str, object] = Field(default_factory=dict)
    variants: list[dict[str, object]] = Field(default_factory=list, max_length=20)
    mde: float = Field(default=10, gt=0, le=100)
    power: float = Field(default=80, ge=50, le=99.9)
    alpha: float = Field(default=5, gt=0, le=20)
    min_days: float = Field(default=7, ge=1, le=365)
    min_views: float = Field(default=5000, ge=100, le=100000000)
    max_days: float = Field(default=30, ge=1, le=365)


@app.post("/api/ab-hypotheses")
async def ab_hypotheses_create(body: ABHypothesisCreate) -> dict[str, object]:
    result = await asyncio.to_thread(_abh, ab_hypotheses.create, **body.model_dump())
    draft_store.audit("ab_hypothesis_create", details=f"id={result['id']};nm={body.nm_id}")
    return result


@app.get("/api/ab-hypotheses/{hypothesis_id}")
def ab_hypotheses_detail(hypothesis_id: int) -> dict[str, object]:
    return _abh(ab_hypotheses.detail, hypothesis_id)


@app.get("/api/ab-hypotheses/{hypothesis_id}/report")
def ab_hypotheses_report(hypothesis_id: int) -> dict[str, object]:
    """Отчёт гипотезы: те же данные detail в стабильной ручке отчётности."""
    return _abh(ab_hypotheses.detail, hypothesis_id)


@app.post("/api/ab-hypotheses/{hypothesis_id}/start")
async def ab_hypotheses_start(hypothesis_id: int) -> dict[str, object]:
    result = await asyncio.to_thread(_abh, ab_hypotheses.start, hypothesis_id)
    draft_store.audit("ab_hypothesis_start", details=f"id={hypothesis_id}")
    return result


class ABHypothesisPause(BaseModel):
    reason: str = "Пауза вручную"


@app.post("/api/ab-hypotheses/{hypothesis_id}/pause")
async def ab_hypotheses_pause(hypothesis_id: int, body: ABHypothesisPause | None = None) -> dict[str, object]:
    return await asyncio.to_thread(_abh, ab_hypotheses.pause, hypothesis_id, (body or ABHypothesisPause()).reason)


class ABHypothesisFinish(BaseModel):
    decision_note: str = ""
    winner_variant: int | None = None
    force: bool = False


@app.post("/api/ab-hypotheses/{hypothesis_id}/finish")
async def ab_hypotheses_finish(hypothesis_id: int, body: ABHypothesisFinish) -> dict[str, object]:
    result = await asyncio.to_thread(_abh, ab_hypotheses.finish, hypothesis_id, body.decision_note, body.winner_variant, body.force)
    draft_store.audit("ab_hypothesis_finish", details=f"id={hypothesis_id}")
    return result


class ABHypothesisDecision(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@app.post("/api/ab-hypotheses/{hypothesis_id}/no-decision")
async def ab_hypotheses_no_decision(hypothesis_id: int, body: ABHypothesisDecision) -> dict[str, object]:
    result = await asyncio.to_thread(_abh, ab_hypotheses.no_decision, hypothesis_id, body.reason)
    draft_store.audit("ab_hypothesis_no_decision", details=f"id={hypothesis_id}")
    return result


@app.post("/api/ab-hypotheses/{hypothesis_id}/rollback")
async def ab_hypotheses_rollback(hypothesis_id: int, body: ABHypothesisDecision | None = None) -> dict[str, object]:
    reason = (body.reason if body else "Откат к исходной карточке")
    result = await asyncio.to_thread(_abh, ab_hypotheses.rollback, hypothesis_id, reason)
    draft_store.audit("ab_hypothesis_rollback", details=f"id={hypothesis_id}")
    return result


@app.get("/api/finance/update")
def finance_update_status() -> dict[str, object]:
    return finance_runner.status()


@app.post("/api/finance/update")
async def finance_update(request: Request) -> dict[str, object]:
    payload = await request.json() if await request.body() else {}
    mode = (payload or {}).get("mode", "cache")
    if mode not in ("cache", "full", "reload"):
        raise HTTPException(400, "mode: cache, full или reload")
    project = finance_project()
    try:
        result = start_finance_report(project, mode)
    except finance.FinanceError as exc:
        raise HTTPException(409, str(exc)) from exc
    draft_store.audit("finance_update_started", details=mode)
    return result


@app.get("/api/recommendations")
def list_recommendations() -> list[dict[str, object]]:
    return recommendation_store.all()


@app.post("/api/recommendations")
async def create_recommendation(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict) or not str(payload.get("name") or "").strip():
        raise HTTPException(400, "Укажите название рекомендации")
    result = recommendation_store.create(payload)
    draft_store.audit("recommendation_created", details=str(result.get("id")))
    return result


@app.put("/api/recommendations/{recommendation_id}")
async def update_recommendation(recommendation_id: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    try:
        result = recommendation_store.update(recommendation_id, payload if isinstance(payload, dict) else {})
    except KeyError as exc:
        raise HTTPException(404, "Рекомендация не найдена") from exc
    return result


@app.delete("/api/recommendations/{recommendation_id}")
def delete_recommendation(recommendation_id: str) -> dict[str, bool]:
    try:
        recommendation_store.delete(recommendation_id)
    except KeyError as exc:
        raise HTTPException(404, "Рекомендация не найдена") from exc
    return {"deleted": True}


@app.get("/api/reports/export")
@app.get("/api/fbs-reports/export")
async def export_reports() -> FileResponse:
    importer = ReportImporter(settings.fbs_report_root)
    reports = importer.scan()
    rows = importer.rows()
    report_names = {report.report_id: report.filename for report in reports}
    payload = {"reports": [report.model_dump() for report in reports], "rows": [dict(row.model_dump(), pdf=report_names.get(row.report_id, "")) for row in rows]}
    data_dir = Path("data")
    export_dir = Path("exports")
    data_dir.mkdir(exist_ok=True)
    export_dir.mkdir(exist_ok=True)
    payload_path = data_dir / "fbs_supply_payload.json"
    output_path = export_dir / "fbs_supply_report.xlsx"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    script_path = Path("scripts") / "export_fbs_supply.mjs"
    process = await asyncio.to_thread(subprocess.run, [settings.node_path, str(script_path), str(payload_path), str(output_path)], capture_output=True, text=True)
    if process.returncode != 0 or not output_path.exists():
        raise HTTPException(status_code=500, detail=f"Не удалось собрать Excel: {process.stderr[-500:]}")
    draft_store.audit("report_exported", details=f"rows={len(rows)}")
    return FileResponse(output_path, filename="fbs_supply_report.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
