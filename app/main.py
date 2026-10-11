import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from urllib.parse import urlsplit

from app.config import get_settings
from app.models import AppModuleSettings, AutoReplySettings, AutoReplyRule, AutoSyncUpdate, ChatMessageCreate, ChatSyncRequest, ComplaintCase, ComplaintDetails, ComplaintStatus, ComplaintUpdate, DraftUpdate, GroupActionRequest, InboxItem, ModerationUpdate, OrderStatus, QuestionReplySettings, ReplyDraft, TemplateCreate
from app.services.cabinets import load_cabinets
from app.services.complaints import ComplaintService
from app.services.replies import ReplyService
from app.services.publishing import ReplyPublisher
from app.services.bulk_replies import BulkPreviewRequest, BulkReplySender
from app.services.complaint_browser import ComplaintBrowser, ComplaintPrepareRequest, ComplaintSubmitRequest
from app.services.reports import ReportImporter
from app.services.product_knowledge import ProductKnowledge
from app.services.review_analysis import analyze_review, topic_label
from app.services.recommendations import RecommendationStore
from app.services.telegram import send_telegram_message
from app.services.templates import TemplateStore
from app.services.chats import ChatStore
from app.services.knowledge_groups import KnowledgeGroupStore
from app.services.wb_client import WBApiError, WBFeedbackClient
from app.storage import DraftStore

settings = get_settings()
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
manual_send_file = Path("data/manual_send.json")
manual_send_enabled = manual_send_file.exists() and json.loads(manual_send_file.read_text(encoding="utf-8")).get("enabled") is True
reply_publisher = ReplyPublisher(draft_store, settings, load_cabinets, manual_send_enabled=manual_send_enabled)
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
        if selected.get("signature"):
            generated.text = f"{generated.text.rstrip()}\n\n{template_store.render(str(selected['signature']), variables)}"
    if settings_value.signature:
        generated.text = f"{generated.text.rstrip()}\n\n{settings_value.signature.strip()}"
    draft = draft_store.save_generated(generated)
    warnings = reply_service.validate(draft.text, item)
    status = "moderation" if warnings or settings_value.require_moderation else "ready"
    draft_store.audit("auto_question_generated", item.id, f"status={status};template={selected.get('name') if selected else 'knowledge'}")
    return {"item_id": item.id, "status": status, "warnings": warnings, "text": draft.text}


@app.middleware("http")
async def guard_complaint_browser_origin(request: Request, call_next):
    if not settings.allow_remote and request.client and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return JSONResponse({"detail": "Панель доступна только локально"}, status_code=403)
    if request.method == "POST" and request.url.path.startswith("/api/complaint-browser/"):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != request.url.port:
                return JSONResponse({"detail": "Операция доступна только из локальной панели сервиса"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def inject_knowledge_workspace(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").split(";")[0] != "text/html":
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    html = body.decode("utf-8")
    if request.url.path in {"/", "/knowledge"}:
        script = Path("app/static/knowledge.js").read_text(encoding="utf-8")
        html = html.replace("</body>", f"<script>{script}</script></body>")
    page = request.url.path.strip("/")
    if page and page not in {"docs"}:
        html = html.replace("</body>", f'''<script>(()=>{{const page="{page}";const routes={{home:"/",products:"/products",cabinets:"/cabinets",knowledge:"/knowledge",analytics:"/analytics",reviews:"/reviews",templates:"/templates",recommendations:"/recommendations",settings:"/settings",ai:"/ai",import:"/import",questions:"/questions",chats:"/chats",messengers:"/messengers",profile:"/profile",finances:"/finances"}};document.querySelectorAll("[data-nav]").forEach(x=>{{const key=x.dataset.nav;if(routes[key]){{x.href=routes[key];x.onclick=null}}}});if(page!=="home"){{document.querySelectorAll(".shell>section,.shell>.grid,.shell>.toolbar").forEach(x=>x.style.display="none");const target=document.getElementById(page)||document.querySelector(".shell>section.panel");if(target){{target.style.display="block";target.scrollIntoView()}}}}}})() </script></body>''')
    return Response(content=html, status_code=response.status_code, headers={"content-type": "text/html; charset=utf-8"})


async def perform_sync(cabinet_id: str) -> dict[str, object]:
    cabinets = {cabinet.id: cabinet for cabinet in load_cabinets(settings.cabinets_json)}
    cabinet = cabinets.get(cabinet_id)
    if cabinet is None:
        raise WBApiError("Кабинет не найден")
    items = await WBFeedbackClient(cabinet.token, settings.feedbacks_api_base_url).fetch_unanswered()
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
    telegram = get_module_settings("telegram")["values"]
    if isinstance(telegram, dict) and telegram.get("enabled") and items:
        try:
            reviews_count = sum(1 for item in items if item.item_type.value == "review")
            questions_count = sum(1 for item in items if item.item_type.value == "question")
            messages = []
            if reviews_count and telegram.get("notify_new_reviews"):
                messages.append(f"новых отзывов — {reviews_count}")
            if questions_count and telegram.get("notify_new_questions"):
                messages.append(f"новых вопросов — {questions_count}")
            if messages:
                await send_telegram_message(telegram, f"WB OPS: {', '.join(messages)}. Кабинет: {cabinet.name}.")
        except Exception as exc:
            draft_store.audit("telegram_error", cabinet.id, str(exc)[:300])
    return {"cabinet_id": cabinet.id, "received": len(items), "drafts_created": len(items), "published": 0}


async def auto_sync_loop() -> None:
    global auto_sync_enabled
    while True:
        if auto_sync_enabled:
            for cabinet in load_cabinets(settings.cabinets_json):
                try:
                    await perform_sync(cabinet.id)
                except Exception as exc:
                    # Errors remain isolated to this cycle; manual sync exposes details.
                    draft_store.set_setting("sync_last_error", str(exc)[:500])
                    draft_store.audit("sync_error", cabinet.id, str(exc)[:500])
                    continue
        await asyncio.sleep(auto_sync_interval_minutes * 60)


@app.on_event("startup")
async def start_scheduler() -> None:
    global auto_sync_task
    upgrade_file = Path("data/order_status_upgrade.json")
    if upgrade_file.exists() and not json.loads(upgrade_file.read_text(encoding="utf-8")).get("completed"):
        from finish_order_status_upgrade import main as finish_upgrade
        await finish_upgrade()
    auto_sync_task = asyncio.create_task(auto_sync_loop())


@app.on_event("shutdown")
async def stop_scheduler() -> None:
    if auto_sync_task:
        auto_sync_task.cancel()
    tasks = list(bulk_sender.tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await complaint_browser.close()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WB Operations</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#1f2937;background:#f6f7fb}*{box-sizing:border-box}body{margin:0}.shell{max-width:1180px;margin:auto;padding:28px 20px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:28px}.brand{display:flex;align-items:center;gap:12px}.logo{width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#7c3aed,#ec4899);color:#fff;display:grid;place-items:center;font-weight:800;box-shadow:0 8px 20px #7c3aed33}.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#8b5cf6;font-weight:700}.title{font-size:25px;font-weight:800;margin:2px 0}.muted{color:#6b7280}.status{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:700}.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}.btn{border:0;border-radius:10px;padding:10px 14px;font-weight:700;cursor:pointer;background:#6d28d9;color:#fff;box-shadow:0 5px 12px #6d28d933}.btn:disabled{opacity:.55;cursor:wait}.btn.secondary{background:#fff;color:#5b21b6;border:1px solid #ddd6fe;box-shadow:none}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}.card,.panel{background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px #1118270a}.card{padding:18px}.metric{font-size:28px;font-weight:800;margin-top:6px}.panel{padding:20px;margin-top:18px}.panel h2{font-size:17px;margin:0 0 14px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid #f0f1f5;vertical-align:top}th{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}textarea{width:100%;min-width:300px;min-height:74px;padding:10px;border:1px solid #d1d5db;border-radius:9px;font:inherit;resize:vertical}small.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#f3e8ff;color:#6b21a8;font-weight:700}.report-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.report{padding:12px;border:1px solid #eee;border-radius:10px;background:#fafafa}.toast{position:fixed;right:20px;bottom:20px;background:#111827;color:#fff;padding:12px 16px;border-radius:10px;display:none}@media(max-width:760px){.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
.bulk-overlay{position:fixed;inset:0;background:#11182799;z-index:100;display:grid;place-items:center;padding:20px}.bulk-dialog{background:#fff;border-radius:16px;width:min(980px,100%);max-height:90vh;overflow:auto;padding:24px}.bulk-top{position:sticky;top:-24px;background:#fff;padding:12px 0;z-index:1}.bulk-item{border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin:12px 0}.bulk-item textarea{min-height:100px;min-width:0}.bulk-overlay[hidden]{display:none}.bulk-actions{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}.bulk-dialog h2{margin:0 0 10px}
.purchase{display:inline-block;padding:5px 9px;border-radius:7px;background:#f3f4f6;color:#4b5563;font-size:12px;font-weight:700;white-space:nowrap}.purchase.buyout{background:#d1fae5;color:#065f46}.purchase.rejected{background:#ffedd5;color:#9a3412}.purchase.returned{background:#fee2e2;color:#991b1b}

.complaint-card{appearance:none;text-align:left;font:inherit;cursor:pointer;width:100%;border:1px solid #e5e7eb;background:#fff;border-radius:12px;padding:16px;transition:border-color .15s,box-shadow .15s}.complaint-card:hover{border-color:#8b5cf6;box-shadow:0 4px 12px #7c3aed18}.complaint-card:focus-visible{outline:3px solid #c4b5fd;outline-offset:2px}.complaint-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px}.complaint-state{font-size:12px;font-weight:700;padding:5px 9px;border-radius:7px;background:#f3f4f6;color:#4b5563}.complaint-state.ready_manual{background:#fff7ed;color:#9a3412}.complaint-state.submitted_manual{background:#ecfdf5;color:#065f46}.complaint-state.dismissed{background:#eef2ff;color:#3730a3}.complaint-excerpt{line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;margin:10px 0;color:#374151}.complaint-section{padding:16px;border:1px solid #e5e7eb;border-radius:12px;margin:14px 0}.complaint-section p{white-space:pre-wrap;line-height:1.6}.complaint-section textarea{min-width:0;min-height:140px}.complaint-section label{display:block}.complaint-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}.complaint-controls a{text-decoration:none}.complaint-id{overflow-wrap:anywhere;font-family:monospace;font-size:13px}.complaint-notice{padding:12px;background:#fff7ed;color:#9a3412;border-radius:9px;font-size:13px;line-height:1.6}.complaint-dialog-title{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.complaint-dialog-title h2{margin-top:0}#complaintModal{z-index:110}@media(max-width:500px){.bulk-overlay{padding:10px}.bulk-dialog{padding:16px}.complaint-meta{align-items:flex-start;flex-direction:column}}

</style></head><body><main class="shell"><aside class="sidebar"><div class="sidebar-brand"><div class="logo">WB</div><div><strong>WB OPS</strong><span>рабочая панель</span></div></div><nav class="side-nav"><a href="#home" class="nav-link active" data-nav="home">⌂ <span>Главная</span></a><div class="nav-label">ОБЩЕЕ</div><a href="#products" class="nav-link" data-nav="products">▣ <span>Товары</span></a><a href="#cabinets" class="nav-link" data-nav="cabinets">▤ <span>Кабинеты маркетплейсов</span></a><a href="#knowledge" class="nav-link" data-nav="knowledge">▥ <span>База знаний</span></a><a href="#analytics" class="nav-link" data-nav="analytics">▥ <span>Аналитика</span><span class="nav-plus">+</span></a><div class="nav-label">СЕРВИСЫ</div><div class="nav-group-title">▧ <span>Управление отзывами</span><span>−</span></div><a href="#inbox-panel" class="nav-link sub active" data-nav="reviews">Все отзывы</a><a href="#templatePanel" class="nav-link sub" data-nav="templates">Шаблоны ответов</a><a href="#productKnowledgePanel" class="nav-link sub" data-nav="recommendations">Рекомендации товаров</a><a href="#templatePanel" class="nav-link sub" data-nav="settings">Настройки ответов</a><a href="#templatePanel" class="nav-link sub" data-nav="ai">Настройки ИИ <em>Beta</em></a><a href="#inbox-panel" class="nav-link sub" data-nav="import">Импорт отзывов</a><div class="nav-group-title">◌ <span>Управление вопросами</span><span>+</span></div><a href="#chats" class="nav-link" data-nav="chats">♧ <span>Управление чатами</span></a><a href="#messengers" class="nav-link" data-nav="messengers">▧ <span>Агенты в мессенджерах</span></a><div class="nav-label">ПАРАМЕТРЫ И НАСТРОЙКИ</div><a href="#profile" class="nav-link" data-nav="profile">♙ <span>Личный кабинет</span><span class="nav-plus">+</span></a><a href="#finances" class="nav-link" data-nav="finances">▱ <span>Финансы</span></a></nav><div class="sidebar-footer">WB Operations <span>v0.1</span></div></aside><header class="top" id="home"><div class="brand"><div class="logo">WB</div><div><div class="eyebrow">Operations</div><div class="title">Отзывы, вопросы и поставки FBS</div><div class="muted">Рабочая панель продавца</div></div></div><div class="status" id="publishStatus">Проверка режима публикации…</div></header>
<div class="toolbar" id="cabinet"></div><div class="toolbar"><button class="btn secondary" onclick="downloadReport()">⇩ Скачать Excel</button><label class="btn secondary">Автосинхронизация <input id="auto" type="checkbox" onchange="setAuto()"></label><select id="interval" class="btn secondary" onchange="setAuto()"><option value="5">каждые 5 мин</option><option value="15">каждые 15 мин</option><option value="30">каждые 30 мин</option><option value="60">каждый час</option></select></div><section class="grid" id="cards"><div class="card">Загрузка…</div></section>
<section class="panel"><h2>Входящие вопросы и отзывы</h2><div class="toolbar"><button id="bulkButton" class="btn" onclick="previewBulk()">Отправить отзывы по фильтрам</button><button class="btn secondary" onclick="toggleGroupBar()">Групповые действия</button><button class="btn secondary" onclick="load()">Обновить список</button></div><p class="muted" style="font-size:13px">Фильтруйте по статусу, рейтингу, дате, теме и тональности. Автоматическая разметка используется для поиска и правил обработки.</p><div class="toolbar"><select id="typeFilter" class="btn secondary" onchange="load()"><option value="">Все типы</option><option value="review">Отзывы</option><option value="question">Вопросы</option></select><select id="ratingFilter" class="btn secondary" onchange="load()"><option value="">Любой рейтинг</option><option value="5">5 звёзд</option><option value="4">4 звезды</option><option value="3">3 звезды</option><option value="2">2 звезды</option><option value="1">1 звезда</option></select><select id="orderFilter" aria-label="Статус покупки" class="btn secondary" onchange="load()"><option value="">Все статусы покупки</option><option value="buyout">Выкуп · клиент забрал</option><option value="rejected">Отказ · клиент не забрал</option><option value="returned">Возврат</option><option value="notSpecified">Статус не указан</option></select><select id="topicFilter" class="btn secondary" onchange="load()"><option value="">Все темы</option><option value="quality">Качество</option><option value="size">Размер</option><option value="delivery">Доставка</option><option value="packaging">Упаковка</option><option value="material">Материал</option><option value="color">Цвет</option><option value="price">Цена</option><option value="texture">Текстура</option></select><select id="sentimentFilter" class="btn secondary" onchange="load()"><option value="">Любая тональность</option><option value="positive">Позитивные</option><option value="neutral">Нейтральные</option><option value="negative">Негативные</option></select><input id="productFilter" placeholder="WB ID товара" oninput="load()" style="padding:10px;border:1px solid #ddd6fe;border-radius:10px"><input id="dateFromFilter" type="date" title="Дата от" onchange="load()"><input id="dateToFilter" type="date" title="Дата до" onchange="load()"><select id="sortFilter" class="btn secondary" onchange="load()"><option value="desc">Сначала новые</option><option value="asc">Сначала старые</option></select></div><div id="groupBar" class="toolbar" hidden><span class="muted" data-selected-count>Выбрано: 0</span><button class="btn secondary" onclick="groupAction('confirm')">Подтвердить</button><button class="btn secondary" onclick="groupAction('rework')">На доработку</button><button class="btn secondary" onclick="groupAction('regenerate')">Перегенерировать</button><button class="btn secondary" onclick="groupAction('reject')">Отклонить</button></div><div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="selectAll" onchange="selectAllRows(this.checked)"></th><th>Тип / дата</th><th>Товар и отзыв</th><th>Рейтинг / разметка</th><th>Черновик ответа</th><th>Действия</th></tr></thead><tbody id="inbox"><tr><td colspan="6" class="muted">Загрузка…</td></tr></tbody></table></div></section>
<section class="panel"><h2>Отчёты Честного знака</h2><div id="reports" class="report-list">Загрузка…</div></section><section class="panel"><h2>Очередь жалоб на отзывы 1–2★</h2><p class="muted">Нажмите на карточку, чтобы проверить отзыв и подготовить жалобу. Подача через сервис выполняется в отдельном Chrome после проверки ID и подтверждения текста.</p><div class="toolbar"><button class="btn" onclick="connectComplaintBrowser()">Подключить WB для подачи жалоб</button><button class="btn secondary" onclick="checkComplaintBrowser()">Проверить подключение</button><span id="complaintBrowserStatus" class="muted">Отправка жалоб через браузер требует настройки.</span></div><div class="toolbar"><select id="complaintFilter" class="btn secondary" aria-label="Статус жалобы" onchange="loadComplaints()"><option value="">Все статусы</option><option value="ready_manual">На ручной проверке</option><option value="not_eligible">Нет явных оснований</option><option value="submitted_manual">Подана вручную</option><option value="submitted_browser">Подана через сервис</option><option value="dismissed">Закрыта без жалобы</option></select><button class="btn secondary" onclick="loadComplaints()">Обновить очередь</button><span id="complaintCount" class="muted"></span></div><div id="complaints" class="report-list">Загрузка…</div></section><section class="panel"><h2>Журнал действий</h2><div id="audit" class="muted">Загрузка…</div></section></main><div id="toast" class="toast"></div>
<div id="bulkModal" class="bulk-overlay" hidden><section class="bulk-dialog" role="dialog" aria-modal="true" aria-labelledby="bulkTitle"><div class="bulk-top"><h2 id="bulkTitle">Проверка пакета отзывов</h2><div id="bulkSummary"></div><div class="bulk-actions"><button id="bulkConfirm" class="btn" onclick="confirmBulk()">Подтвердить отправку</button><button id="bulkClose" class="btn secondary" onclick="closeBulk()">Закрыть</button></div><div id="bulkProgress" role="status" aria-live="polite"></div></div><div id="bulkItems"></div></section></div>
<div id="complaintModal" class="bulk-overlay" hidden><section class="bulk-dialog" role="dialog" aria-modal="true" aria-labelledby="complaintTitle" tabindex="-1"><header class="complaint-dialog-title"><div><h2 id="complaintTitle">Проверка отзыва</h2><div id="complaintHeader" class="muted"></div></div><button class="btn secondary" onclick="closeComplaint()">Закрыть</button></header><div id="complaintBody">Загрузка…</div></section></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const orderLabels={buyout:'Выкуп · забрал',rejected:'Отказ · не забрал',returned:'Возврат',notSpecified:'Статус не указан'};
function toast(text){const x=document.querySelector('#toast');x.textContent=text;x.style.display='block';setTimeout(()=>x.style.display='none',2600)}
async function sync(id){const r=await fetch('/api/sync/'+encodeURIComponent(id),{method:'POST'});const d=await r.json();toast(r.ok?`Загружено: ${d.received}. Черновиков: ${d.drafts_created}.`:d.detail||'Ошибка синхронизации');if(r.ok){await load();await loadComplaints()}}
const sending=new Set(),pendingText=new Map();
async function sendDraft(id,button){
if(sending.has(id))return;
const field=document.querySelector(`[data-draft="${CSS.escape(id)}"], [data-question-draft="${CSS.escape(id)}"]`);
const text=field.value.trim();
if(text.length<2){toast('Введите ответ: минимум 2 символа');return}
pendingText.set(id,text);sending.add(id);button.disabled=true;button.textContent='Отправка…';field.disabled=true;
let published=false;
try{
const r=await fetch('/api/replies/send/'+encodeURIComponent(id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
const d=await r.json();
if(!r.ok){toast(d.detail||'WB не принял ответ');return}
published=true;pendingText.delete(id);toast('Ответ отправлен в Wildberries');
field.closest('tr')?.remove();
await load();
}catch(e){toast(published?'Ответ отправлен, но список не обновился. Обновите страницу.':'Нет подтверждения отправки. Проверьте ответ в WB перед повторной попыткой.')}
finally{sending.delete(id);button.disabled=false;button.textContent='Отправить';field.disabled=false}
}
async function confirmDraft(id){const r=await fetch('/api/replies/drafts/'+encodeURIComponent(id)+'/confirm',{method:'POST'});toast(r.ok?'Черновик подтверждён':(await r.json()).detail||'Ошибка подтверждения');if(r.ok)load()}
async function setAuto(){const r=await fetch('/api/settings/auto-sync',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:document.querySelector('#auto').checked,interval_minutes:Number(document.querySelector('#interval').value)})});toast(r.ok?'Настройки автосинхронизации сохранены':'Не удалось сохранить настройки')}
async function confirmDraft(id){const r=await fetch('/api/replies/drafts/'+encodeURIComponent(id)+'/confirm',{method:'POST'});toast(r.ok?'Черновик подтверждён':(await r.json()).detail||'Ошибка подтверждения');if(r.ok)load()}

let bulkToken=null,bulkRunning=false;
function closeBulk(){document.querySelector('#bulkModal').hidden=true}
function currentReviewFilters(){const rating=document.querySelector('#ratingFilter').value,order=document.querySelector('#orderFilter').value,product=document.querySelector('#productFilter').value.trim();if(product&&(!/^[1-9][0-9]*$/.test(product)||!Number.isSafeInteger(Number(product))))throw new Error('WB ID должен быть положительным целым числом');return {rating:rating?Number(rating):null,order_status:order||null,product_id:product?Number(product):null}}
async function previewBulk(){
if(bulkRunning){document.querySelector('#bulkModal').hidden=false;return}
if(document.querySelector('#typeFilter').value==='question'){toast('Выберите отзывы или все типы. Вопросы пакетно не отправляются.');return}
const button=document.querySelector('#bulkButton');button.disabled=true;
try{
const texts=Object.fromEntries(pendingText);document.querySelectorAll('[data-draft]').forEach(x=>texts[x.dataset.draft]=x.value);
const r=await fetch('/api/replies/bulk/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filters:currentReviewFilters(),texts})}),d=await r.json();
if(!r.ok){toast(d.detail||'Не удалось подготовить пакет');return}
bulkToken=d.token;
const f=d.filters;const summary=`Отзывы: ${d.total}. Рейтинг: ${f.rating??'любой'}. Статус: ${f.order_status?orderLabels[f.order_status]:'любой'}. WB ID: ${f.product_id??'все'}. Исключено: ${d.skipped.length}.`;
document.querySelector('#bulkSummary').textContent=summary;
document.querySelector('#bulkTitle').textContent='Проверьте ответы перед отправкой в Wildberries';
document.querySelector('#bulkConfirm').textContent=`Отправить ${d.total} ответов в WB`;
document.querySelector('#bulkConfirm').disabled=!d.total;
document.querySelector('#bulkProgress').textContent='Будут отправлены только перечисленные ниже тексты. Новые отзывы в этот пакет не добавляются.';
document.querySelector('#bulkItems').innerHTML=d.items.map(x=>`<article class="bulk-item"><b>Кабинет: ${esc(x.cabinet_id)} · WB ID: ${esc(x.product_id??'—')}</b><div class="muted">Отзыв: ${esc(x.item_id)} · ${x.rating??'—'} ★ · ${esc(orderLabels[x.order_status]||'Статус не указан')}</div><p style="white-space:pre-wrap">${esc(x.review_text)||'Без текста'}</p><label>Ответ для публикации<textarea readonly>${esc(x.text)}</textarea></label></article>`).join('')||'<p>Подходящих отзывов для отправки нет.</p>';
document.querySelector('#bulkModal').hidden=false;
}catch(e){toast(e.message==='WB ID должен быть положительным целым числом'?e.message:'Не удалось подготовить пакет. Ничего не отправлено.')}finally{button.disabled=false}
}
async function confirmBulk(){
if(!bulkToken||bulkRunning)return;
const button=document.querySelector('#bulkConfirm');button.disabled=true;
try{
const r=await fetch('/api/replies/bulk/'+encodeURIComponent(bulkToken)+'/send',{method:'POST'}),d=await r.json();
if(!r.ok){toast(d.detail||'Не удалось запустить отправку');button.disabled=false;return}
bulkRunning=true;document.querySelector('#bulkButton').textContent='Показать ход отправки';
document.querySelector('#bulkTitle').textContent='Отправка подтверждённого пакета';
pollBulk();
}catch(e){document.querySelector('#bulkProgress').textContent='Запрос отправлен, но результат запуска неясен. Проверяю состояние пакета…';pollBulk()}
}
async function pollBulk(){
try{
const r=await fetch('/api/replies/bulk/'+encodeURIComponent(bulkToken)),d=await r.json();
if(!r.ok)throw new Error();
if(d.status==='preview'){bulkRunning=false;document.querySelector('#bulkConfirm').disabled=false;document.querySelector('#bulkProgress').textContent='Пакет ещё не запущен. Можно подтвердить отправку.';return}
bulkRunning=d.status==='running';
document.querySelector('#bulkProgress').textContent=`Обработано ${d.processed||0} из ${d.total}. Отправлено: ${d.sent}. Ошибок: ${d.failed}. ${d.reason||''}`;
if(bulkRunning){setTimeout(pollBulk,1500);return}
document.querySelector('#bulkButton').textContent='Отправить отзывы по фильтрам';
document.querySelector('#bulkConfirm').disabled=true;
document.querySelector('#bulkTitle').textContent=d.status==='completed'?'Отправка завершена':'Отправка остановлена';
document.querySelector('#bulkItems').innerHTML+=(d.results||[]).filter(x=>x.status!=='sent').map(x=>`<p><b>${esc(x.item_id)}</b>: ${esc(x.reason)}</p>`).join('');
for(const x of d.results||[])if(x.status==='sent'){pendingText.delete(x.item_id);document.querySelector(`[data-draft="${CSS.escape(x.item_id)}"]`)?.closest('tr')?.remove()}
await load();
}catch(e){document.querySelector('#bulkProgress').textContent='Не удалось получить состояние пакета. Отправка может продолжаться; повторно не запускайте её.';setTimeout(pollBulk,3000)}
}

async function downloadReport(){window.location='/api/fbs-reports/export'}
async function load(){
document.querySelectorAll('[data-draft]').forEach(x=>{const id=x.dataset.draft;if(x.value!==(x.dataset.original??x.value))pendingText.set(id,x.value)});
const health=await (await fetch('/health')).json();
document.querySelector('#publishStatus').textContent=health.dry_run&&!health.manual_send_enabled?'Черновики · публикация выключена':'Отправка в WB · только по кнопке «Отправить»';
const params=new URLSearchParams();const type=document.querySelector('#typeFilter')?.value,rating=document.querySelector('#ratingFilter')?.value,product=document.querySelector('#productFilter')?.value.trim();if(type)params.set('item_type',type);if(rating)params.set('rating',rating);if(product)params.set('product_id',product);const order=document.querySelector('#orderFilter')?.value;if(order)params.set('order_status',order);const [c,r,i,d,a,s]=await Promise.all([fetch('/api/cabinets'),fetch('/api/reports'),fetch('/api/inbox?'+params),fetch('/api/replies/drafts'),fetch('/api/audit'),fetch('/api/settings/auto-sync')]);const cabinets=await c.json(),reports=await r.json(),inbox=await i.json(),drafts=await d.json(),audit=await a.json(),auto=await s.json();document.querySelector('#cabinet').innerHTML=cabinets.map(x=>`<button class="btn" onclick="sync('${esc(x.id)}')">↻ Синхронизировать · ${esc(x.name)}</button>`).join('');document.querySelector('#auto').checked=auto.enabled;document.querySelector('#interval').value=String(auto.interval_minutes);document.querySelector('#cards').innerHTML=`<div class="card"><div class="muted">PDF-отчёты</div><div class="metric">${reports.count}</div></div><div class="card"><div class="muted">Строк товаров</div><div class="metric">${reports.row_count}</div></div><div class="card"><div class="muted">Количество</div><div class="metric">${reports.quantity_total.toLocaleString('ru-RU')}</div></div>`;const dm=Object.fromEntries(drafts.map(x=>[x.item_id,x]));document.querySelector('#inbox').innerHTML=inbox.length?inbox.map(x=>`<tr><td><small class="badge">${x.item_type==='review'?'Отзыв':'Вопрос'}</small></td><td>${esc(x.text)||'<span class="muted">Без текста</span>'}<br><span class="muted">WB ID: ${esc(x.product_id||'—')}</span></td><td>${x.rating??'—'}</td><td>${x.item_type==='review'?`<small class="purchase ${esc(x.order_status)}">${esc(orderLabels[x.order_status]||orderLabels.notSpecified)}</small>`:'<span class="muted">Не применимо</span>'}</td><td><textarea ${sending.has(x.id)?'disabled':''} data-draft="${esc(x.id)}" data-original="${esc(dm[x.id]?.text||'')}">${esc(pendingText.get(x.id)??dm[x.id]?.text??'')}</textarea></td><td><button class="btn secondary" onclick="sendDraft('${esc(x.id)}',this)" ${sending.has(x.id)?'disabled':''}>${sending.has(x.id)?'Отправка…':'Отправить'}</button></td></tr>`).join(''):'<tr><td colspan="6" class="muted">По текущим фильтрам ничего не найдено.</td></tr>';document.querySelector('#reports').innerHTML=reports.reports.map(x=>`<div class="report"><b>${esc(x.warehouse||'Склад')}</b><br><span class="muted">Отчёт №${esc(x.report_id)} · ${Math.round(x.size_bytes/1024)} КБ</span></div>`).join('')||'<span class="muted">Отчёты не найдены</span>';document.querySelector('#audit').innerHTML=audit.slice(0,10).map(x=>`<div style="padding:8px 0;border-bottom:1px solid #f0f1f5"><b>${esc(x.event)}</b> <span class="muted">${esc(x.item_id||'')} · ${new Date(x.created_at).toLocaleString('ru-RU')}</span></div>`).join('')||'Пока нет действий'}load();
const complaintLabels={ready_manual:'На ручной проверке',not_eligible:'Нет явных оснований',submitted_manual:'Подана вручную',submitted_browser:'Подана через сервис',dismissed:'Закрыта без жалобы'};
let complaintCases=[],currentComplaint=null,complaintFocus=null;
async function loadComplaints(){
const target=document.querySelector('#complaints');
try{
const value=document.querySelector('#complaintFilter').value,r=await fetch('/api/complaints'+(value?'?status='+encodeURIComponent(value):''));
if(!r.ok)throw new Error();
complaintCases=await r.json();
document.querySelector('#complaintCount').textContent='В списке: '+complaintCases.length;
target.innerHTML=complaintCases.length?complaintCases.map(x=>`<button type="button" class="complaint-card" data-complaint="${esc(x.item_id)}"><div class="complaint-meta"><b>${x.rating??'—'} ★ · WB ID ${esc(x.product_id??'—')}</b><span class="complaint-state ${esc(x.status)}">${esc(complaintLabels[x.status]||x.status)}</span></div><b>${esc(x.reason_title||'Ручная проверка')}</b><p class="complaint-excerpt">${esc(x.review_text)||'Текст отзыва не найден'}</p><span class="muted">${esc(orderLabels[x.order_status]||'Статус не указан')} · Кабинет ${esc(x.cabinet_id)}</span><div style="color:#6d28d9;font-weight:700;margin-top:12px">Открыть отзыв и действия →</div></button>`).join(''):'<div class="muted">По выбранному статусу отзывов нет.</div>';
target.querySelectorAll('[data-complaint]').forEach(button=>button.addEventListener('click',()=>openComplaint(button.dataset.complaint)));
}catch(e){target.innerHTML='<p class="muted">Не удалось загрузить очередь. Нажмите «Обновить очередь».</p>'}
}
async function openComplaint(id){
complaintFocus=document.activeElement;const modal=document.querySelector('#complaintModal');modal.hidden=false;document.querySelector('#complaintBody').textContent='Загрузка…';
modal.querySelector('[role=dialog]').focus();
try{
const r=await fetch('/api/complaints/'+encodeURIComponent(id));if(!r.ok)throw new Error();
currentComplaint=await r.json();renderComplaint();
}catch(e){document.querySelector('#complaintBody').textContent='Не удалось открыть отзыв. Закройте окно и повторите попытку.'}
}
function renderComplaint(){
const x=currentComplaint;
document.querySelector('#complaintHeader').textContent=`${x.rating??'—'} ★ · WB ID: ${x.product_id??'—'} · ${orderLabels[x.order_status]||'Статус не указан'} · Кабинет: ${x.cabinet_id}`;
document.querySelector('#complaintBody').innerHTML=`
<section class="complaint-section"><b>Отзыв покупателя</b><p>${esc(x.review_text)||(x.review_exists?'Отзыв без текста':'Исходный отзыв не найден в базе')}</p><div class="complaint-id">ID: ${esc(x.item_id)}</div><div class="muted">Ответ покупателю: ${x.reply_published?'уже отправлен':'не отправлен'}</div><div class="complaint-controls"><button class="btn secondary" onclick="copyComplaint('id')">Скопировать ID</button><button class="btn secondary" onclick="copyComplaint('product')" ${x.product_id?'':'disabled'}>Скопировать WB ID</button>${x.product_id?`<a class="btn secondary" href="https://www.wildberries.ru/catalog/${encodeURIComponent(x.product_id)}/detail.aspx" target="_blank" rel="noopener noreferrer">Карточка товара</a>`:''}</div></section>
<section class="complaint-section"><b>Результат предварительной проверки</b><p>${esc(x.reason_title||'Основание не указано')}</p>${x.evidence?`<p><b>Найденный фрагмент:</b><br>${esc(x.evidence)}</p>`:'<div class="complaint-notice">Явные признаки нарушения не найдены. Низкая оценка, отказ от заказа или претензия к товару сами по себе не доказывают нарушение.</div>'}<p><b>Пункты для ручной проверки</b></p><ul>${(x.checklist||[]).map(y=>`<li>${esc(y)}</li>`).join('')}</ul></section>
<section class="complaint-section"><label for="complaintDraft"><b>Черновик обращения в модерацию</b></label><p class="muted">Проверьте факты перед копированием. Для подачи через сервис требуется 50–1000 символов. Без вашего подтверждения текст не отправляется.</p><textarea id="complaintDraft" maxlength="1000" placeholder="Если вы нашли нарушение, опишите его и приведите конкретный фрагмент отзыва.">${esc(x.complaint_draft)}</textarea><div class="complaint-controls"><button class="btn secondary" onclick="copyComplaint('draft')">Скопировать текст жалобы</button><a class="btn" href="https://seller.wildberries.ru/" target="_blank" rel="noopener noreferrer">Открыть личный кабинет WB</a><a class="btn secondary" href="https://seller.wildberries.ru/instructions/ru/tj/material/customer-reviews" target="_blank" rel="noopener noreferrer">Инструкция WB</a></div><p class="muted">В личном кабинете выберите нужный кабинет и раздел «Отзывы и вопросы». Найдите товар по WB ID и нужный отзыв по его тексту; в меню отзыва выберите «Пожаловаться на отзыв».</p></section>
<section class="complaint-section"><b>Подача через сервис</b><p id="complaintBrowserModalStatus" class="muted">Нужен вход в отдельный Chrome для WB. Основной браузер и его пароли не используются.</p><p class="muted">Причина WB: «Другое», с вашим полным текстом обращения. Вложения подаются вручную. После подготовки проверьте кабинет, ID, текст отзыва и жалобу.</p><div class="complaint-controls"><button class="btn secondary" onclick="connectComplaintBrowser()">Подключить браузер WB</button><button class="btn secondary" onclick="checkComplaintBrowser()">Проверить подключение</button><button id="complaintBrowserSend" class="btn" onclick="prepareComplaintBrowser(false)" ${['submitted_manual','submitted_browser','dismissed'].includes(x.status)?'disabled':''}>Отправить жалобу через сервис</button><button id="complaintBrowserSelected" class="btn secondary" onclick="prepareComplaintBrowser(true)" ${['submitted_manual','submitted_browser','dismissed'].includes(x.status)?'disabled':''}>Проверить отзыв, открытый в Chrome</button></div><p class="muted">Если автопоиск не нашёл отзыв, откройте его вручную в отдельном Chrome и нажмите «Ещё», чтобы показать ID. Проверка открытого отзыва не отправляет жалобу.</p><div id="complaintSendPreview" aria-live="polite"></div></section>
<section class="complaint-section"><label for="complaintState"><b>Статус обработки в этой панели</b></label><div class="complaint-controls"><select id="complaintState" class="btn secondary">${Object.entries(complaintLabels).filter(([key])=>key!=='submitted_browser'||x.status===key).map(([key,label])=>`<option value="${key}" ${x.status===key?'selected':''}>${esc(label)}</option>`).join('')}</select><button id="complaintSave" class="btn" onclick="saveComplaintState()">Сохранить статус</button></div><p class="muted">«Подана вручную» выбирайте только после подачи в WB. Изменение статуса здесь не отправляет жалобу.</p></section>`;
}
function closeComplaint(){document.querySelector('#complaintModal').hidden=true;currentComplaint=null;complaintSendPreview=null;complaintFocus?.focus()}
async function copyComplaint(kind){
if(!currentComplaint)return;
const text=kind==='id'?currentComplaint.item_id:kind==='product'?String(currentComplaint.product_id||''):document.querySelector('#complaintDraft').value.trim();
if(!text){toast('Сначала заполните текст жалобы');return}
try{await navigator.clipboard.writeText(text);toast('Скопировано')}
catch(e){const field=document.querySelector('#complaintDraft');if(kind==='draft'){field.focus();field.select()}else{window.prompt('Скопируйте значение:',text)}toast('Автокопирование недоступно. Скопируйте выделенный текст вручную.')}
}
async function saveComplaintState(){
if(!currentComplaint)return;const button=document.querySelector('#complaintSave'),status=document.querySelector('#complaintState').value;
button.disabled=true;
try{const r=await fetch('/api/complaints/'+encodeURIComponent(currentComplaint.item_id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})}),d=await r.json();if(!r.ok){toast(d.detail||'Не удалось сохранить статус');return}currentComplaint.status=d.status;toast('Статус сохранён. Жалоба в WB не отправлялась.');await loadComplaints()}catch(e){toast('Не удалось сохранить статус')}finally{button.disabled=false}
}
document.querySelector('#complaintModal').addEventListener('click',event=>{if(event.target.id==='complaintModal')closeComplaint()});
document.addEventListener('keydown',event=>{if(document.querySelector('#complaintModal').hidden)return;if(event.key==='Escape'){closeComplaint();return}if(event.key==='Tab'){const elements=Array.from(document.querySelectorAll('#complaintModal button:not(:disabled),#complaintModal a,#complaintModal textarea,#complaintModal select'));const first=elements[0],last=elements.at(-1);if(event.shiftKey&&(document.activeElement===first||document.activeElement.getAttribute('role')==='dialog')){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});

let complaintBrowserPoll=null,complaintSendPreview=null,complaintSending=false;
function showComplaintBrowserState(data){const text=data.message||'Браузер не подключён';document.querySelector('#complaintBrowserStatus').textContent=text;const modal=document.querySelector('#complaintBrowserModalStatus');if(modal)modal.textContent=text}
async function connectComplaintBrowser(){
try{const r=await fetch('/api/complaint-browser/connect',{method:'POST'}),d=await r.json();if(!r.ok){toast(d.detail||'Не удалось подключить браузер');return}showComplaintBrowserState(d);pollComplaintBrowser()}catch(e){toast('Не удалось подключить браузер WB')}
}
async function pollComplaintBrowser(){
clearTimeout(complaintBrowserPoll);
try{const r=await fetch('/api/complaint-browser/status'),d=await r.json();showComplaintBrowserState(d);if(['starting','installing','opening_browser'].includes(d.status))complaintBrowserPoll=setTimeout(pollComplaintBrowser,2000)}catch(e){showComplaintBrowserState({message:'Не удалось получить состояние браузера'})}
}
async function checkComplaintBrowser(){
try{const r=await fetch('/api/complaint-browser/inspect'),d=await r.json();if(!r.ok){toast(d.detail||'Не удалось проверить подключение');return}showComplaintBrowserState(d);toast(d.status==='waiting_login'?'Завершите вход в браузере':'Браузер WB подключён. Можно подготовить конкретную жалобу.')}catch(e){toast('Не удалось проверить браузер')}
}

async function prepareComplaintBrowser(useSelected){
if(!currentComplaint||complaintSending)return;
const text=document.querySelector('#complaintDraft').value.trim();
if(text.length<50||text.length>1000){toast('Заполните фактическое основание жалобы: 50–1000 символов');return}
const item=currentComplaint.item_id,box=document.querySelector('#complaintSendPreview');
complaintSending=true;complaintSendPreview=null;
document.querySelector('#complaintBrowserSend').disabled=true;document.querySelector('#complaintBrowserSelected').disabled=true;
box.textContent='Проверяем выбранный отзыв и подготавливаем форму WB. Жалоба ещё не отправляется…';
try{
const r=await fetch('/api/complaint-browser/prepare/'+encodeURIComponent(item),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,use_selected:useSelected})}),d=await r.json();
if(!currentComplaint||currentComplaint.item_id!==item)return;
if(!r.ok){box.textContent=d.detail||'Не удалось подготовить форму. Жалоба не отправлена.';return}
if(!d.token){box.textContent=d.message||'Подключите браузер WB на локальном компьютере.';return}
complaintSendPreview=d;
box.innerHTML=`<section class="complaint-section"><h3>Подтвердите подачу жалобы</h3><p><b>Получатель:</b> ${esc(d.destination)}<br><b>Кабинет WB:</b> ${esc(d.cabinet)}<br><b>WB ID:</b> ${esc(d.product_id)}<br><b>ID отзыва:</b> ${esc(d.item_id)}<br><b>Причина:</b> ${esc(d.reason)}</p><b>Отзыв покупателя</b><p>${esc(d.review_text)}</p><b>Полный текст жалобы</b><p>${esc(d.text)}</p><div class="complaint-notice">Низкая оценка или отказ не являются сами по себе основанием для удаления. Подтверждая отправку, вы подтверждаете указанные факты и выбранный кабинет. Предпросмотр действует 5 минут.</div><label style="margin-top:14px"><input id="complaintConfirmCheck" type="checkbox" onchange="document.querySelector('#complaintFinalSend').disabled=!this.checked"> Проверил кабинет, отзыв и весь текст жалобы</label><div class="complaint-controls"><button id="complaintFinalSend" class="btn" disabled onclick="submitComplaintBrowser()">Подтвердить и отправить в WB</button><button class="btn secondary" onclick="cancelComplaintPreview()">Отмена</button></div></section>`;
box.scrollIntoView({behavior:'smooth',block:'nearest'});
}catch(e){if(currentComplaint?.item_id===item)box.textContent='Ошибка подготовки. Жалоба не отправлена.'}
finally{complaintSending=false;if(currentComplaint?.item_id===item){document.querySelector('#complaintBrowserSend').disabled=false;document.querySelector('#complaintBrowserSelected').disabled=false}}
}
function cancelComplaintPreview(){complaintSendPreview=null;document.querySelector('#complaintSendPreview').textContent='Отправка отменена. Форма в Chrome не отправлена.'}
async function submitComplaintBrowser(){
if(!complaintSendPreview||complaintSending||!document.querySelector('#complaintConfirmCheck')?.checked)return;
const approved=complaintSendPreview,item=approved.item_id;
if(document.querySelector('#complaintDraft').value.trim()!==approved.text){toast('Текст изменён. Подготовьте жалобу заново.');cancelComplaintPreview();return}
complaintSending=true;
const box=document.querySelector('#complaintSendPreview');
box.querySelectorAll('button,input').forEach(e=>e.disabled=true);
try{
const r=await fetch('/api/complaint-browser/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:approved.token,confirmed:true})}),d=await r.json();
complaintSendPreview=null;
if(!currentComplaint||currentComplaint.item_id!==item)return;
box.textContent=d.message||d.detail||'Проверьте результат в «Мои жалобы» WB.';
if(r.ok&&d.status==='sent'){currentComplaint.status='submitted_browser';document.querySelector('#complaintState').innerHTML+='<option value="submitted_browser" selected>Подана через сервис</option>';toast('WB подтвердил подачу жалобы')}
else if(r.ok&&d.status==='uncertain'){toast('Результат требует проверки в WB. Повтор заблокирован.')}
else{toast(d.detail||'Отправка не выполнена')}
await loadComplaints();
}catch(e){complaintSendPreview=null;if(currentComplaint?.item_id===item)box.textContent='Соединение прервано. Не повторяйте отправку, пока не проверите «Мои жалобы» WB.'}
finally{complaintSending=false}
}
pollComplaintBrowser();

loadComplaints();

/* Усиленный рабочий слой панели: не меняет безопасную серверную публикацию, только делает состояния видимыми. */
const dashboardStyle=document.createElement('style');dashboardStyle.textContent='.dashboard-extra{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#6b7280}.dashboard-search{padding:10px;border:1px solid #ddd6fe;border-radius:10px;min-width:220px}.draft-saved{background:#ecfdf5!important;border-color:#a7f3d0!important}.metric-note{font-size:12px;color:#6b7280;margin-top:3px}.toolbar{align-items:center}.toolbar .btn,.toolbar select,.toolbar input{height:40px}.toolbar label.btn{display:inline-flex;align-items:center;gap:8px}.toolbar input[type=checkbox]{height:auto;width:auto;margin:0}.toolbar select.btn{appearance:auto;background:#fff;color:#4c1d95;padding:0 34px 0 13px;min-width:150px}.toolbar input{font:inherit;background:#fff}.panel{overflow:hidden}.table-wrap{border:1px solid #eef0f5;border-radius:12px}.table-wrap table{table-layout:fixed}.table-wrap th:nth-child(1){width:105px}.table-wrap th:nth-child(2){width:28%}.table-wrap th:nth-child(3){width:70px}.table-wrap th:nth-child(4){width:155px}.table-wrap th:nth-child(5){width:37%}.table-wrap th:nth-child(6){width:112px}.table-wrap td{padding:14px 10px}.table-wrap tbody tr{transition:background .15s}.table-wrap tbody tr:hover{background:#faf8ff}.table-wrap textarea{display:block;width:100%;min-width:0;min-height:88px;line-height:1.45;border-color:#d8d4e3;transition:border-color .15s,box-shadow .15s}.table-wrap textarea:focus{border-color:#8b5cf6;outline:3px solid #ede9fe;box-shadow:none}.draft-actions{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin-top:9px}.draft-actions .btn,.table-wrap td:last-child .btn{height:36px;padding:7px 11px;font-size:12px;white-space:nowrap}.table-wrap td:last-child{vertical-align:middle;text-align:center}.status-chip{display:inline-flex;align-items:center;min-height:27px;padding:4px 9px;border-radius:999px;background:#f3f4f6;color:#4b5563;font-size:11px;font-weight:800;white-space:nowrap}.status-chip.sent{background:#dcfce7;color:#166534}.status-chip:not(.sent){background:#ede9fe;color:#5b21b6}.badge{white-space:nowrap}@media(max-width:900px){.table-wrap{overflow-x:auto}.table-wrap table{min-width:980px}}@media(max-width:600px){.shell{padding:18px 12px 40px}.panel{padding:14px}.toolbar{gap:8px}.toolbar .btn,.toolbar select,.toolbar input{width:100%}.toolbar input[type=checkbox]{width:auto}.toolbar label.btn{display:flex;justify-content:space-between;align-items:center}.title{font-size:21px}}';document.head.appendChild(dashboardStyle);
function readableEvent(value){return ({sync:'Синхронизация завершена',sync_error:'Ошибка синхронизации',published:'Ответ опубликован',draft_confirmed_by_send:'Черновик подтверждён отправкой',draft_updated:'Черновик сохранён',complaint_ready:'Жалоба требует проверки',complaint_not_eligible:'Жалоба без явного основания',complaint_status_updated:'Статус жалобы изменён',report_exported:'Отчёт выгружен',auto_sync_updated:'Настройки автосинхронизации изменены',publish_failed:'Ошибка публикации',publish_uncertain:'Публикация требует проверки'})[value]||value}
async function saveDraft(id,field,button){const text=field.value.trim();if(text.length<2){toast('Черновик должен содержать минимум 2 символа');return}button.disabled=true;try{const r=await fetch('/api/replies/drafts/'+encodeURIComponent(id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})}),d=await r.json();if(!r.ok){toast(d.detail||'Не удалось сохранить черновик');return}field.dataset.original=text;field.classList.add('draft-saved');button.textContent='Сохранено';toast('Черновик сохранён. В WB не отправлялся.');setTimeout(()=>{field.classList.remove('draft-saved');button.textContent='Сохранить'},1800)}catch(e){toast('Нет соединения с сервисом')}finally{button.disabled=false}}
function enhanceInbox(){const body=document.querySelector('#inbox');if(!body)return;const rows=[...body.querySelectorAll('tr')];const search=document.querySelector('#dashboardSearch')?.value.trim().toLowerCase()||'';let visible=0;rows.forEach(row=>{if(!row.querySelector('textarea'))return;const field=row.querySelector('textarea'),id=field.dataset.draft;const text=row.innerText.toLowerCase();const matches=!search||text.includes(search);row.style.display=matches?'':'none';if(matches)visible++;if(!row.querySelector('[data-save-draft]')){const cell=field.closest('td'),actions=document.createElement('div');actions.className='draft-actions';const save=document.createElement('button');save.className='btn secondary';save.dataset.saveDraft='1';save.textContent='Сохранить';save.onclick=()=>saveDraft(id,field,save);const state=document.createElement('span');state.className='dashboard-extra';state.textContent='Черновик';actions.append(save,state);cell.append(actions)}});const counter=document.querySelector('#inboxCount');if(counter)counter.textContent=`Показано: ${visible} · поиск работает по тексту и ID`}
async function updateSyncStatus(){try{const d=await (await fetch('/api/sync-status')).json(),box=document.querySelector('#syncState');if(!box)return;box.className='sync-state '+(d.last_error?'error':'ok');if(d.last_error)box.textContent='Ошибка синхронизации: '+d.last_error;else if(d.last_at){const when=new Date(d.last_at).toLocaleString('ru-RU');box.textContent=`Синхронизация: ${when} · новых ${d.last_received}` }else box.textContent=d.auto_enabled?'Автосинхронизация включена':'Синхронизация ещё не запускалась'}catch(e){}}
async function updateDashboardCounters(){const cards=document.querySelector('#cards');if(!cards)return;const inboxRows=[...document.querySelectorAll('#inbox tr textarea')].length;const complaints=await (await fetch('/api/complaints?status=ready_manual')).json();const extra=`<div class="card"><div class="muted">Новые обращения</div><div class="metric">${inboxRows}</div><div class="metric-note">отзывы и вопросы в работе</div></div><div class="card"><div class="muted">Ждут проверки</div><div class="metric">${complaints.length}</div><div class="metric-note">жалобы на 1–2★</div></div>`;if(!cards.querySelector('[data-extra-cards]')){const wrap=document.createElement('div');wrap.dataset.extraCards='1';wrap.style.display='contents';wrap.innerHTML=extra;cards.append(wrap)}}
const baseLoad=load;load=async function(){await baseLoad();enhanceInbox();updateSyncStatus();updateDashboardCounters();const audit=document.querySelector('#audit');if(audit) audit.innerHTML=audit.innerHTML.replace(/>([a-z_]+)</g,(m,e)=>'>'+readableEvent(e)+'<');};
const originalSync=sync;sync=async function(id){await originalSync(id);updateSyncStatus();};
const searchHost=document.querySelector('#productFilter')?.parentElement;if(searchHost&&!document.querySelector('#dashboardSearch')){const input=document.createElement('input');input.id='dashboardSearch';input.className='dashboard-search';input.placeholder='Быстрый поиск по тексту или ID';input.oninput=enhanceInbox;searchHost.append(input)}
const syncHost=document.querySelector('#interval')?.parentElement;if(syncHost&&!document.querySelector('#syncState')){const state=document.createElement('span');state.id='syncState';state.className='sync-state';state.textContent='Синхронизация: проверка…';syncHost.append(state)}
const filterHost=document.querySelector('#productFilter')?.parentElement;if(filterHost&&!document.querySelector('#statusFilter')){const state=document.createElement('select');state.id='statusFilter';state.className='btn secondary';state.innerHTML='<option value="">Все статусы</option><option value="draft">Активные черновики</option>';state.onchange=enhanceInbox;filterHost.insertBefore(state,document.querySelector('#productFilter'))}
const baseEnhance=enhanceInbox;enhanceInbox=function(){baseEnhance();const state=document.querySelector('#statusFilter')?.value||'';if(state){document.querySelectorAll('#inbox tr').forEach(row=>{if(row.querySelector('textarea'))row.style.display=state==='draft'?'':'none'})}};
const bulkHint=document.querySelector('#bulkButton')?.nextElementSibling;if(bulkHint)bulkHint.textContent='Пакетная отправка доступна только для отзывов. Перед публикацией сервис покажет количество, статусы покупки и полный текст каждого ответа.';
function relabelFbs(){document.querySelectorAll('h2').forEach(h=>{if(h.textContent.includes('Честного знака'))h.textContent='Отчёты поставок FBS'});const title=document.querySelector('.title');if(title)title.textContent='Отзывы, вопросы и поставки FBS';const download=document.querySelector('button[onclick="downloadReport()"]');if(download)download.textContent='⇩ Скачать сводку поставок FBS';const backup=document.querySelector('button[data-backup]');if(!backup&&download){const b=document.createElement('button');b.className='btn secondary';b.dataset.backup='1';b.textContent='⇩ Резервная копия';b.onclick=()=>window.location='/api/admin/backup';download.parentElement.appendChild(b)}const first=document.querySelector('#cards .card .muted');if(first)first.textContent='FBS-отчёты';const report=document.querySelector('#reports');if(report&&!report.children.length)report.innerHTML='<span class="muted">Загрузите отчёты поставок FBS в папку fbs_reports</span>'}
async function enhanceAdminPanels(){const reports=document.querySelector('#reports');if(!reports)return;const panel=reports.closest('.panel');if(panel&&!panel.querySelector('[data-fbs-upload]')){const bar=document.createElement('div');bar.className='toolbar';bar.innerHTML='<input data-fbs-upload type="file" accept=".pdf,.csv,.xlsx,.xls" hidden><button class="btn secondary" data-upload-btn>Загрузить FBS-файл</button><span class="muted" data-upload-state>PDF, CSV или Excel до 50 МБ</span>';panel.querySelector('h2')?.after(bar);const input=bar.querySelector('input');bar.querySelector('[data-upload-btn]').onclick=()=>input.click();input.onchange=async()=>{if(!input.files[0])return;const fd=new FormData();fd.append('file',input.files[0]);bar.querySelector('[data-upload-state]').textContent='Загрузка…';const r=await fetch('/api/fbs-reports/upload',{method:'POST',body:fd});const d=await r.json();bar.querySelector('[data-upload-state]').textContent=r.ok?'Файл загружен: '+d.filename:(d.detail||'Ошибка загрузки');if(r.ok)load()}}const existing=document.querySelector('#templatePanel');if(!existing){const p=document.createElement('section');p.id='templatePanel';p.className='panel';p.innerHTML='<h2>Шаблоны ответов</h2><p class="muted">Изменения применяются к новым и перегенерируемым черновикам.</p><div data-template-list>Загрузка…</div>';panel?.after(p);const templates=await (await fetch('/api/templates')).json();p.querySelector('[data-template-list]').innerHTML=Object.entries(templates).map(([key,text])=>`<div class="template-row"><label><b>${esc(key)}</b><textarea data-template="${esc(key)}">${esc(text)}</textarea><button class="btn secondary" data-save-template="${esc(key)}">Сохранить шаблон</button></label></div>`).join('');p.querySelectorAll('[data-save-template]').forEach(b=>b.onclick=async()=>{const key=b.dataset.saveTemplate;const text=p.querySelector(`[data-template="${CSS.escape(key)}"]`).value;const r=await fetch('/api/templates/'+encodeURIComponent(key),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});toast(r.ok?'Шаблон сохранён':'Не удалось сохранить шаблон')})}}
const dashboardLoadWithLabels=load;load=async function(){await dashboardLoadWithLabels();relabelFbs();enhanceAdminPanels()};
function enhanceProductKnowledge(){if(document.querySelector('#productKnowledgePanel'))return;const templates=document.querySelector('#templatePanel');if(!templates)return;const p=document.createElement('section');p.id='productKnowledgePanel';p.className='panel';p.innerHTML='<h2>База характеристик товаров</h2><p class="muted">Характеристики привязаны к WB ID и используются при подготовке ответов.</p><div class="toolbar"><input id="knowledgeProductId" class="dashboard-search" placeholder="WB ID товара"><button class="btn secondary" onclick="loadProductKnowledge()">Открыть</button></div><div id="knowledgeEditor" hidden><div class="knowledge-grid"><label>Состав<textarea data-fact="composition"></textarea></label><label>Замеры<textarea data-fact="measurements"></textarea></label><label>Детали модели<textarea data-fact="details"></textarea></label><label>Уход<textarea data-fact="care"></textarea></label><label>Сезонность<textarea data-fact="season"></textarea></label></div><button class="btn" onclick="saveProductKnowledge()">Сохранить характеристики</button></div>';templates.after(p)}
async function renderFbsRegistry(){const box=document.querySelector('#reports');if(!box)return;const r=await fetch('/api/fbs-reports'),d=await r.json();const labels={new:'Новый',checked:'Проверен',reported:'Отчитан',error:'Ошибка'};box.innerHTML=d.reports.length?d.reports.map(x=>`<div class="report"><b>${esc(x.warehouse||'Склад не указан')}</b><br><span class="muted">${esc(x.filename)} · ${Math.round(x.size_bytes/1024)} КБ</span><select class="btn secondary" onchange="setFbsStatus('${esc(x.filename)}',this.value)">${Object.entries(labels).map(([v,l])=>`<option value="${v}" ${x.status===v?'selected':''}>${l}</option>`).join('')}</select></div>`).join(''):'<span class="muted">FBS-файлы ещё не загружены</span>'}
async function setFbsStatus(filename,status){const r=await fetch('/api/fbs-reports/'+encodeURIComponent(filename)+'/status?status='+encodeURIComponent(status),{method:'PUT'});toast(r.ok?'Статус FBS сохранён':'Не удалось изменить статус')}
async function enhanceCabinets(){const box=document.querySelector('#cabinet');if(!box)return;const cabinets=await (await fetch('/api/cabinets')).json();[...box.querySelectorAll('button')].forEach((button,i)=>{const cabinet=cabinets[i];if(cabinet&&!button.querySelector('[data-cabinet-state]')){const state=document.createElement('span');state.dataset.cabinetState='1';state.className='dashboard-extra';state.textContent=cabinet.token_configured?' · токен подключён':' · нужен токен';button.append(state)}})}
async function loadProductKnowledge(){const id=document.querySelector('#knowledgeProductId').value.trim();if(!/^\\d+$/.test(id)){toast('Введите корректный WB ID');return}const r=await fetch('/api/products/knowledge/'+id),d=await r.json();const editor=document.querySelector('#knowledgeEditor');editor.hidden=false;editor.dataset.productId=id;editor.querySelectorAll('[data-fact]').forEach(x=>x.value=d.facts[x.dataset.fact]||'')}
async function saveProductKnowledge(){const editor=document.querySelector('#knowledgeEditor'),id=editor.dataset.productId;if(!id)return;const facts={};editor.querySelectorAll('[data-fact]').forEach(x=>facts[x.dataset.fact]=x.value.trim());const r=await fetch('/api/products/knowledge/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(facts)});toast(r.ok?'Характеристики сохранены':'Не удалось сохранить характеристики')}
const dashboardLoadWithKnowledge=load;load=async function(){await dashboardLoadWithKnowledge();enhanceProductKnowledge();await renderFbsRegistry();await enhanceCabinets()};
async function enhanceAutoReplies(){
  if(document.querySelector('#autoRepliesPanel'))return;
  const anchor=document.querySelector('#templatePanel')||document.querySelector('#inbox-panel');if(!anchor)return;
  const p=document.createElement('section');p.id='autoRepliesPanel';p.className='panel';
  p.innerHTML='<div class="auto-head"><div><h2>Автоматические ответы на отзывы</h2><p class="muted">Сервис анализирует новые отзывы, выбирает подходящий шаблон и создаёт персональный черновик. Публикация без подтверждения выключена.</p></div><span class="status-chip" data-auto-state>Загрузка…</span></div><div class="auto-grid"><label class="auto-toggle"><input data-auto-enabled type="checkbox"><span><b>Включить автоответы</b><small>Обрабатывать отзывы при синхронизации</small></span></label><label>Режим<select data-auto-mode class="btn secondary"><option value="draft">Черновик + модерация</option><option value="auto_generate">Автогенерация черновика</option></select></label><label>От рейтинга<select data-auto-min class="btn secondary"></select></label><label>До рейтинга<select data-auto-max class="btn secondary"></select></label></div><div class="auto-grid"><label class="auto-toggle"><input data-auto-moderation type="checkbox" checked><span><b>Требовать модерацию</b><small>Перед отправкой оператор проверяет текст</small></span></label><label class="auto-toggle"><input data-auto-publish type="checkbox"><span><b>Автопубликация</b><small>Опасный режим: доступен только без модерации</small></span></label></div><label class="auto-rules-label">Правила по ключевым словам <small class="muted">JSON-массив: name, min_rating, max_rating, keywords, action (generate/skip)</small><textarea data-auto-rules placeholder="[{&quot;name&quot;:&quot;Проблемы с размером&quot;,&quot;keywords&quot;:[&quot;размер&quot;,&quot;маломерит&quot;]}]"></textarea></label><div class="toolbar"><button class="btn" data-auto-save>Сохранить настройки</button><button class="btn secondary" data-auto-run>Обработать загруженные отзывы</button><span class="muted" data-auto-result></span></div>';
  anchor.after(p);
  const fill=(select)=>{select.innerHTML=[1,2,3,4,5].map(x=>`<option value="${x}">${x} ★</option>`).join('')};fill(p.querySelector('[data-auto-min]'));fill(p.querySelector('[data-auto-max]'));
  const setState=(x)=>{p.querySelector('[data-auto-enabled]').checked=!!x.enabled;p.querySelector('[data-auto-mode]').value=x.mode;p.querySelector('[data-auto-min]').value=x.min_rating;p.querySelector('[data-auto-max]').value=x.max_rating;p.querySelector('[data-auto-moderation]').checked=!!x.require_moderation;p.querySelector('[data-auto-publish]').checked=!!x.auto_publish;p.querySelector('[data-auto-rules]').value=JSON.stringify(x.rules||[],null,2);p.querySelector('[data-auto-state]').textContent=x.enabled?'Включено':'Выключено';p.querySelector('[data-auto-state]').classList.toggle('sent',!!x.enabled)};
  const r=await fetch('/api/settings/auto-replies');if(r.ok)setState(await r.json());
  p.querySelector('[data-auto-save]').onclick=async()=>{let rules=[];try{rules=JSON.parse(p.querySelector('[data-auto-rules]').value||'[]');if(!Array.isArray(rules))throw new Error()}catch(e){toast('Правила должны быть корректным JSON-массивом');return}const body={enabled:p.querySelector('[data-auto-enabled]').checked,mode:p.querySelector('[data-auto-mode]').value,min_rating:Number(p.querySelector('[data-auto-min]').value),max_rating:Number(p.querySelector('[data-auto-max]').value),require_moderation:p.querySelector('[data-auto-moderation]').checked,auto_publish:p.querySelector('[data-auto-publish]').checked,rules};const response=await fetch('/api/settings/auto-replies',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok){toast(data.detail||'Не удалось сохранить настройки');return}setState(data);toast('Настройки автоответов сохранены')};
  p.querySelector('[data-auto-run]').onclick=async()=>{const b=p.querySelector('[data-auto-run]');b.disabled=true;b.textContent='Обработка…';try{const response=await fetch('/api/auto-replies/run',{method:'POST'}),data=await response.json();p.querySelector('[data-auto-result]').textContent=response.ok?`Проверено: ${data.checked}, создано: ${data.generated}`:(data.detail||'Ошибка обработки');if(response.ok)await load()}finally{b.disabled=false;b.textContent='Обработать загруженные отзывы'}};
}
const autoRepliesLoad=load;load=async function(){await autoRepliesLoad();await enhanceAutoReplies()};
const autoRepliesStyle=document.createElement('style');autoRepliesStyle.textContent='.auto-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.auto-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0}.auto-grid label,.auto-rules-label{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:700}.auto-grid select{width:100%;margin:0}.auto-toggle{display:flex!important;flex-direction:row!important;align-items:flex-start;gap:9px;padding:12px;border:1px solid #eee;border-radius:11px;background:#fafafa}.auto-toggle input{margin-top:3px}.auto-toggle small{display:block;color:#6b7280;font-weight:400;margin-top:4px}.auto-rules-label textarea{min-width:0;min-height:100px;font-family:ui-monospace,Consolas,monospace;font-size:12px}@media(max-width:800px){.auto-grid{grid-template-columns:1fr 1fr}}@media(max-width:560px){.auto-head{flex-direction:column}.auto-grid{grid-template-columns:1fr}}';document.head.appendChild(autoRepliesStyle);
const reviewMarkupStyle=document.createElement('style');reviewMarkupStyle.textContent='.tag-row{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.review-tag,.sentiment,.moderation-status{display:inline-flex;align-items:center;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:800;white-space:nowrap}.review-tag{background:#eef2ff;color:#4338ca}.topic-quality{background:#fee2e2;color:#991b1b}.topic-size{background:#fef3c7;color:#92400e}.topic-delivery{background:#dbeafe;color:#1d4ed8}.topic-packaging{background:#f3e8ff;color:#6b21a8}.topic-material{background:#dcfce7;color:#166534}.topic-color{background:#fce7f3;color:#9d174d}.sentiment.positive{background:#dcfce7;color:#166534}.sentiment.negative{background:#fee2e2;color:#991b1b}.sentiment.neutral{background:#f3f4f6;color:#4b5563}.moderation-line{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:7px}.moderation-status{background:#fff7ed;color:#9a3412}.table-wrap th:first-child,.table-wrap td:first-child{width:38px;text-align:center}.table-wrap th:nth-child(2){width:100px}.table-wrap th:nth-child(3){width:29%}.table-wrap th:nth-child(4){width:150px}.table-wrap th:nth-child(5){width:35%}';document.head.appendChild(reviewMarkupStyle);
const settingsStyle=document.createElement('style');settingsStyle.textContent='.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:14px 0}.settings-field{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:700}.settings-field input,.settings-field select,.settings-grid>input,.settings-grid>select{width:100%;min-width:0;padding:10px;border:1px solid #d8d4e3;border-radius:9px;font:inherit;background:#fff}.settings-field small{font-size:11px;color:#6b7280;font-weight:400}.settings-checks{display:flex;flex-wrap:wrap;gap:16px;margin:14px 0;color:#4b5563;font-size:13px}.settings-checks label{display:flex;gap:7px;align-items:center}.panel>textarea[data-rules-json],.panel>textarea[data-knowledge-store],.panel>textarea[data-tpl-text]{width:100%;min-width:0;min-height:110px;margin:12px 0;padding:11px;border:1px solid #d8d4e3;border-radius:9px;font:inherit}.template-card{padding:14px;border:1px solid #eef0f5;border-radius:11px;margin-top:10px;background:#fff}.template-card>div{display:flex;gap:8px;align-items:center}.template-card p{white-space:pre-wrap;color:#4b5563;line-height:1.5}.template-card .btn{float:right}@media(max-width:760px){.settings-grid{grid-template-columns:1fr}}';document.head.appendChild(settingsStyle);
async function moduleSettings(name){const r=await fetch('/api/settings/modules/'+encodeURIComponent(name));return r.ok?await r.json():{values:{}}}
async function saveModuleSettings(name,values){const r=await fetch('/api/settings/modules/'+encodeURIComponent(name),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});toast(r.ok?'Настройки сохранены':'Не удалось сохранить настройки');return r.ok}
function field(label,html){return `<label class="settings-field"><span>${label}</span>${html}</label>`}
/* async function enhanceExtendedPanels(){
  const anchor=document.querySelector('#autoRepliesPanel')||document.querySelector('#templatePanel');if(!anchor)return;
  if(!document.querySelector('#aiSettingsPanel')){const p=document.createElement('section');p.id='aiSettingsPanel';p.className='panel';p.innerHTML='<h2>Настройки ИИ</h2><p class="muted">Стиль и ограничения генератора. Настройки применяются к новым черновикам.</p><div class="settings-grid">'+field('Фирменное приветствие','<input data-ai="greeting" placeholder="Здравствуйте!"><small>Если заполнено, добавляется в начало ответа.</small>')+field('Подпись','<input data-ai="signature" placeholder="С уважением, команда магазина"><small>Добавляется в конец ответа.</small>')+field('Обращение','<select data-ai="address"><option value="name">По имени покупателя</option><option value="neutral">Нейтрально</option></select>')+field('Длина ответа','<select data-ai="length"><option value="short">Короткий</option><option value="medium">Средний</option><option value="long">Подробный</option></select>')+'</div><div class="settings-checks"><label><input type="checkbox" data-ai="use_emojis"> Использовать смайлики</label><label><input type="checkbox" data-ai="favorite_prompt"> Предлагать добавить магазин в избранное</label><label><input type="checkbox" data-ai="photo_compliment"> Делать комплимент при фото или видео</label></div><button class="btn" data-ai-save>Сохранить настройки ИИ</button>';anchor.after(p);const data=await moduleSettings('ai');const values=data.values||{};p.querySelectorAll('[data-ai]').forEach(x=>{if(x.type==='checkbox')x.checked=!!values[x.dataset.ai];else x.value=values[x.dataset.ai]||''});p.querySelector('[data-ai-save]').onclick=async()=>{const values={};p.querySelectorAll('[data-ai]').forEach(x=>values[x.dataset.ai]=x.type==='checkbox'?x.checked:x.value);await saveModuleSettings('ai',values)}}
  if(!document.querySelector('#notificationsPanel')){const p=document.createElement('section');p.id='notificationsPanel';p.className='panel';p.innerHTML='<h2>Настройки оповещений Telegram</h2><p class="muted">Токен хранится только в локальных настройках. Уведомления отправляются после синхронизации.</p><div class="settings-grid">'+field('Токен Telegram-бота','<input type="password" data-tg="bot_token" placeholder="123456:...">')+field('Chat ID','<input data-tg="chat_id" placeholder="-100..."><small>Узнать можно через @userinfobot.</small>')+field('Кабинеты','<input data-tg="cabinet_ids" placeholder="default, второй кабинет">')+field('Рейтинг для важных отзывов','<select data-tg="min_rating"><option value="1">1–5 звёзд</option><option value="2">1–2 звезды</option><option value="3">1–3 звезды</option></select>')+'</div><div class="settings-checks"><label><input type="checkbox" data-tg="enabled"> Включить Telegram</label><label><input type="checkbox" data-tg="notify_new_reviews"> Новые отзывы</label><label><input type="checkbox" data-tg="notify_published"> Ответы опубликованы</label><label><input type="checkbox" data-tg="notify_low_balance"> Низкий баланс</label></div><button class="btn" data-tg-save>Сохранить Telegram</button>';p.after(document.querySelector('#aiSettingsPanel'));const data=await moduleSettings('telegram');const values=data.values||{};p.querySelectorAll('[data-tg]').forEach(x=>{if(x.type==='checkbox')x.checked=!!values[x.dataset.tg];else x.value=values[x.dataset.tg]||''});p.querySelector('[data-tg-save]').onclick=async()=>{const values={};p.querySelectorAll('[data-tg]').forEach(x=>values[x.dataset.tg]=x.type==='checkbox'?x.checked:x.value);await saveModuleSettings('telegram',values)}}
  if(!document.querySelector('#importPanel')){const p=document.createElement('section');p.id='importPanel';p.className='panel';p.innerHTML='<h2>Подгрузка отзывов</h2><p class="muted">Загрузка истории по выбранному периоду. Ранее сохранённые отзывы не дублируются.</p><div class="settings-grid">'+field('Дата от','<input type="date" data-import="date_from">')+field('Дата до','<input type="date" data-import="date_to">')+field('Кабинеты','<input data-import="cabinet_ids" placeholder="default, cabinet-2">')+field('Разметка','<select data-import="with_markup"><option value="true">Загружать с темами и тональностью</option><option value="false">Только тексты</option></select>')+'</div><button class="btn" data-import-run>Загрузить отзывы</button><span class="muted" data-import-result></span>';p.after(document.querySelector('#notificationsPanel'));p.querySelector('[data-import-run]').onclick=async()=>{const b=p.querySelector('[data-import-run']);b.disabled=true;const cabinetText=p.querySelector('[data-import="cabinet_ids"]').value.trim();const body={cabinet_ids:cabinetText?cabinetText.split(',').map(x=>x.trim()).filter(Boolean):undefined,date_from:p.querySelector('[data-import="date_from"]').value||null,date_to:p.querySelector('[data-import="date_to"]').value||null,with_markup:p.querySelector('[data-import="with_markup"]').value==='true'};const r=await fetch('/api/import/reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();p.querySelector('[data-import-result]').textContent=r.ok?'Загружено: '+d.imported:(d.detail||'Ошибка импорта');b.disabled=false;if(r.ok)await load()}}}
  if(!document.querySelector('#rulesPanel')){const p=document.createElement('section');p.id='rulesPanel';p.className='panel';p.innerHTML='<h2>Правила обработки отзывов</h2><p class="muted">Правила проверяются сверху вниз. Укажите темы, тональность, рейтинг, товар, медиа, стоп-слова и тип ответа.</p><textarea data-rules-json placeholder="[{&quot;name&quot;:&quot;Негатив о размере&quot;,&quot;topics&quot;:[&quot;size&quot;],&quot;sentiments&quot;:[&quot;negative&quot;],&quot;response_type&quot;:&quot;ai&quot;,&quot;moderation_required&quot;:true,&quot;priority&quot;:10}]"></textarea><div class="toolbar"><button class="btn" data-rules-save>Сохранить правила</button><span class="muted">Подробные правила также доступны в блоке автоответов.</span></div>';p.after(document.querySelector('#importPanel'));const data=await moduleSettings('rules');p.querySelector('[data-rules-json]').value=JSON.stringify(data.values?.rules||[],null,2);p.querySelector('[data-rules-save]').onclick=async()=>{try{const rules=JSON.parse(p.querySelector('[data-rules-json]').value||'[]');if(!Array.isArray(rules))throw new Error();await saveModuleSettings('rules',{rules})}catch(e){toast('Правила должны быть корректным JSON-массивом')}}}}
  if(!document.querySelector('#recommendationsPanel')){const p=document.createElement('section');p.id='recommendationsPanel';p.className='panel';p.innerHTML='<h2>Рекомендации товаров</h2><p class="muted">Сохраняйте товары, категории или ссылки, которые можно использовать в ответах по правилам.</p><div class="settings-grid"><input data-rec-name placeholder="Название рекомендации"><input data-rec-products placeholder="WB ID товаров через запятую"><input data-rec-category placeholder="Категория"><input data-rec-link placeholder="Ссылка на подборку (Ozon)"></div><button class="btn" data-rec-save>Добавить рекомендацию</button><div data-rec-list class="report-list"></div>';p.after(document.querySelector('#rulesPanel'));const render=async()=>{const r=await fetch('/api/recommendations'),items=await r.json();p.querySelector('[data-rec-list]').innerHTML=items.map(x=>`<div class="report"><b>${esc(x.name)}</b><br><span class="muted">${esc(x.product_ids||x.category||x.link||'Без условий')}</span></div>`).join('')||'<span class="muted">Рекомендаций пока нет</span>'};p.querySelector('[data-rec-save]').onclick=async()=>{const body={name:p.querySelector('[data-rec-name]').value.trim(),product_ids:p.querySelector('[data-rec-products]').value.split(',').map(x=>x.trim()).filter(Boolean),category:p.querySelector('[data-rec-category]').value.trim(),link:p.querySelector('[data-rec-link]').value.trim(),enabled:true};if(!body.name){toast('Укажите название рекомендации');return}const r=await fetch('/api/recommendations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){toast('Рекомендация добавлена');await render()}};await render()}
  if(!document.querySelector('#knowledgeHubPanel')){const p=document.createElement('section');p.id='knowledgeHubPanel';p.className='panel';p.innerHTML='<h2>База знаний магазина</h2><p class="muted">Общие правила магазина используются как контекст для ответов: доставка, оплата, гарантия, обмен, запреты и группы товаров.</p><textarea data-knowledge-store placeholder="Например: не предлагаем возврат в ответе на отзыв; по размеру направляем к таблице замеров; вопросы по заказу — в поддержку WB."></textarea><button class="btn" data-knowledge-save>Сохранить правила магазина</button>';p.after(document.querySelector('#recommendationsPanel'));const data=await moduleSettings('knowledge');p.querySelector('[data-knowledge-store]').value=data.values?.text||'';p.querySelector('[data-knowledge-save]').onclick=async()=>{await saveModuleSettings('knowledge',{text:p.querySelector('[data-knowledge-store]').value})}}
  const navTargets={knowledge:'knowledgeHubPanel',recommendations:'recommendationsPanel',settings:'templatePanel',ai:'aiSettingsPanel',import:'importPanel'};Object.entries(navTargets).forEach(([nav,target])=>{const link=document.querySelector(`[data-nav="${nav}"]`);if(link)link.setAttribute('href','#'+target)});
  if(!document.querySelector('#customTemplatesPanel')){const p=document.createElement('section');p.id='customTemplatesPanel';p.className='panel';p.innerHTML='<h2>Личные шаблоны ответов</h2><p class="muted">Можно создавать неограниченное количество шаблонов. Переменные: <code>{{name}}</code>, <code>{{rating}}</code>, <code>{{product_id}}</code>, <code>{{product_name}}</code>, <code>{{text}}</code>.</p><div class="settings-grid"><input data-tpl-name placeholder="Название шаблона"><select data-tpl-topic><option value="general">Общее</option><option value="quality">Качество</option><option value="size">Размер</option><option value="delivery">Доставка</option><option value="material">Материал</option><option value="color">Цвет</option></select><select data-tpl-sentiment><option value="any">Любая тональность</option><option value="positive">Позитив</option><option value="negative">Негатив</option><option value="neutral">Нейтральный</option></select></div><textarea data-tpl-text placeholder="Здравствуйте, {{name}}! Спасибо за обратную связь..."></textarea><input data-tpl-signature placeholder="Подпись под ответом"><button class="btn" data-tpl-create>Создать шаблон</button><div data-tpl-list></div>';p.after(document.querySelector('#templatePanel'));const render=async()=>{const r=await fetch('/api/templates/custom'),items=await r.json();p.querySelector('[data-tpl-list]').innerHTML=items.map(x=>`<article class="template-card"><div><b>${esc(x.name)}</b><span class="review-tag">${esc(x.topic||'general')} · ${esc(x.sentiment||'any')}</span></div><p>${esc(x.text)}</p><button class="btn secondary" onclick="deleteCustomTemplate('${esc(x.id)}')">Удалить</button></article>`).join('')||'<p class="muted">Личных шаблонов пока нет.</p>'};window.deleteCustomTemplate=async(id)=>{const r=await fetch('/api/templates/custom/'+encodeURIComponent(id),{method:'DELETE'});if(r.ok){toast('Шаблон удалён');await render()}};p.querySelector('[data-tpl-create]').onclick=async()=>{const body={name:p.querySelector('[data-tpl-name]').value.trim(),topic:p.querySelector('[data-tpl-topic]').value,sentiment:p.querySelector('[data-tpl-sentiment]').value,text:p.querySelector('[data-tpl-text]').value,signature:p.querySelector('[data-tpl-signature]').value,enabled:true};if(!body.name||body.text.length<2){toast('Укажите название и текст шаблона');return}const r=await fetch('/api/templates/custom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){toast('Шаблон создан');p.querySelector('[data-tpl-name]').value='';p.querySelector('[data-tpl-text]').value='';await render()}};await render()}
}
} */
const extendedLoad=load;load=async function(){await extendedLoad()};
async function loadModulePanel(id,title,module,fields){
  if(document.querySelector('#'+id))return;
  const anchor=document.querySelector('#autoRepliesPanel')||document.querySelector('#templatePanel');if(!anchor)return;
  const p=document.createElement('section');p.id=id;p.className='panel';
  p.innerHTML='<h2>'+title+'</h2><p class="muted">Настройки сохраняются локально и применяются к новым операциям.</p><div class="module-fields">'+fields.map(f=>f.type==='checkbox'?'<label class="module-check"><input data-module-key="'+f.key+'" type="checkbox"> <span>'+f.label+'</span></label>':'<label><span>'+f.label+'</span><input data-module-key="'+f.key+'" type="'+(f.secret?'password':'text')+'" placeholder="'+(f.placeholder||'')+'"></label>').join('')+'</div><button class="btn" data-module-save>Сохранить</button>';
  anchor.after(p);
  const response=await fetch('/api/settings/modules/'+encodeURIComponent(module));const data=response.ok?await response.json():{values:{}};const values=data.values||{};
  p.querySelectorAll('[data-module-key]').forEach(x=>x.type==='checkbox'?x.checked===true||values[x.dataset.moduleKey]===true||values[x.dataset.moduleKey]==='true':x.value=values[x.dataset.moduleKey]||'');
  p.querySelector('[data-module-save]').onclick=async()=>{const out={};p.querySelectorAll('[data-module-key]').forEach(x=>out[x.dataset.moduleKey]=x.type==='checkbox'?x.checked:x.value);const r=await fetch('/api/settings/modules/'+encodeURIComponent(module),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:out})});toast(r.ok?'Настройки сохранены':'Не удалось сохранить настройки')};
}
async function enhanceSimplePanels(){
  await loadModulePanel('aiSettingsPanel','Настройки ИИ','ai',[{key:'signature',label:'Фирменная подпись',placeholder:'С уважением, команда магазина'},{key:'tone',label:'Тон ответа',placeholder:'дружелюбный, спокойный'},{key:'length',label:'Длина ответа',placeholder:'короткий / средний / подробный'}]);
  await loadModulePanel('notificationsPanel','Настройки оповещений Telegram','telegram',[{key:'bot_token',label:'Токен бота',secret:true},{key:'chat_id',label:'Chat ID'},{key:'min_rating',label:'Минимальный рейтинг для уведомлений',placeholder:'1'},{key:'enabled',label:'Включить Telegram',type:'checkbox'},{key:'notify_new_reviews',label:'Новые отзывы',type:'checkbox'},{key:'notify_new_questions',label:'Новые вопросы',type:'checkbox'},{key:'notify_published',label:'Опубликованные ответы',type:'checkbox'},{key:'notify_question_answers',label:'Ответы на вопросы',type:'checkbox'}]);
  await loadModulePanel('knowledgeHubPanel','База знаний магазина','knowledge',[{key:'text',label:'Общие правила магазина',placeholder:'Правила доставки, возврата, оплаты и общения'}]);
  await loadModulePanel('recommendationsPanel','Рекомендации товаров','recommendations',[{key:'default_product_ids',label:'WB ID товаров через запятую'},{key:'categories',label:'Категории'}]);
}
async function enhanceCustomTemplatePanel(){if(document.querySelector('#customTemplatesPanel'))return;const anchor=document.querySelector('#templatePanel');if(!anchor)return;const p=document.createElement('section');p.id='customTemplatesPanel';p.className='panel';p.innerHTML='<h2>Личные шаблоны ответов</h2><p class="muted">Переменные: {{name}}, {{rating}}, {{product_id}}, {{product_name}}, {{text}}.</p><input data-tpl-name placeholder="Название шаблона"><textarea data-tpl-text placeholder="Здравствуйте, {{name}}! Спасибо за обратную связь..."></textarea><input data-tpl-signature placeholder="Подпись"><button class="btn" data-tpl-create>Создать шаблон</button><div data-tpl-list></div>';anchor.after(p);const render=async()=>{const r=await fetch('/api/templates/custom'),items=await r.json();p.querySelector('[data-tpl-list]').innerHTML=items.map(x=>'<div class="template-card"><b>'+esc(x.name)+'</b><p>'+esc(x.text)+'</p></div>').join('')||'<span class="muted">Личных шаблонов пока нет.</span>'};p.querySelector('[data-tpl-create]').onclick=async()=>{const body={name:p.querySelector('[data-tpl-name]').value.trim(),text:p.querySelector('[data-tpl-text]').value,signature:p.querySelector('[data-tpl-signature]').value,topic:'general',sentiment:'any',enabled:true};if(!body.name||body.text.length<2){toast('Укажите название и текст шаблона');return}const r=await fetch('/api/templates/custom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){toast('Шаблон создан');await render()}};await render()}
const simplePanelsLoad=load;load=async function(){await simplePanelsLoad();await enhanceSimplePanels();await enhanceCustomTemplatePanel()};
async function enhanceQuestionsPanel(){if(document.querySelector('#questionsPanel'))return;const anchor=document.querySelector('#inbox-panel')||document.querySelector('#autoRepliesPanel');if(!anchor)return;const p=document.createElement('section');p.id='questionsPanel';p.className='panel';p.innerHTML='<div class="question-head"><div><h2>Все вопросы покупателей</h2><p class="muted">Автоответы формируются по базе знаний и шаблонам. Перед отправкой ответ проходит модерацию.</p></div><button class="btn secondary" data-question-refresh>Обновить</button></div><div class="toolbar"><input data-question-search placeholder="Поиск по вопросу, имени или WB ID"><select data-question-status class="btn secondary"><option value="">Все статусы</option><option value="draft">Черновики</option><option value="ready">Готовы к отправке</option><option value="published">Отправлены</option></select><input data-question-from type="date"><input data-question-to type="date"><button class="btn" data-question-run>Сформировать автоответы</button></div><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Товар</th><th>Вопрос</th><th>Ответ</th></tr></thead><tbody data-question-list><tr><td colspan="4">Загрузка…</td></tr></tbody></table></div><hr><h3>Настройки ответов на вопросы</h3><div class="question-settings"><label><input type="checkbox" data-question-enabled> Включить автоответы</label><label><input type="checkbox" data-question-keywords checked> Сначала искать по ключевым словам</label><label><input type="checkbox" data-question-ai checked> Использовать генерацию ИИ</label><label><input type="checkbox" data-question-moderation checked> Модерировать ответы</label><label><input type="checkbox" data-question-name checked> Обращаться по имени</label><input data-question-signature placeholder="Подпись под ответом"><select data-question-empty><option value="manual">Если ответа нет — ручной ответ</option><option value="ai">Если ответа нет — ИИ</option><option value="ignore">Если ответа нет — игнорировать</option></select></div><button class="btn" data-question-save>Сохранить настройки вопросов</button><h3>Шаблон вопроса с ключевыми словами</h3><div class="question-settings"><input data-question-tpl-name placeholder="Название шаблона"><input data-question-tpl-keywords placeholder="Ключевые слова через запятую"><textarea data-question-tpl-text placeholder="Здравствуйте, {{name}}! Ответ на вопрос..."></textarea><input data-question-tpl-signature placeholder="Подпись"></div><button class="btn secondary" data-question-tpl-save>Добавить шаблон</button></section>';anchor.after(p);const loadQuestions=async()=>{const q=new URLSearchParams({item_type:'question',limit:'100'});const search=p.querySelector('[data-question-search]').value.trim(),status=p.querySelector('[data-question-status]').value,from=p.querySelector('[data-question-from]').value,to=p.querySelector('[data-question-to]').value;if(search)q.set('search',search);if(status)q.set('status',status);if(from)q.set('date_from',from);if(to)q.set('date_to',to);const r=await fetch('/api/inbox/overview?'+q),items=r.ok?await r.json():[];p.querySelector('[data-question-list]').innerHTML=items.map(x=>`<tr><td>${esc(workflowDate(x.created_at))}</td><td>WB ID: ${esc(x.product_id||'—')}<br><span class="muted">${esc(x.product_name||'')}</span></td><td><b>${esc(x.author_name||'Аноним')}</b><br>${esc(x.text)}</td><td><span class="status-chip">${esc({pending:'На модерации',approved:'Подтверждён',rework:'На доработке',rejected:'Отклонён'}[x.moderation_status]||'Черновик')}</span><textarea data-question-draft="${esc(x.id)}">${esc(x.draft_text)}</textarea><div class="draft-actions"><button class="btn secondary" onclick="moderateDraft('${esc(x.id)}','approve')">Подтвердить</button><button class="btn secondary" onclick="moderateDraft('${esc(x.id)}','rework')">На доработку</button><button class="btn secondary" onclick="sendDraft('${esc(x.id)}',this)">Отправить</button></div></td></tr>`).join('')||'<tr><td colspan="4" class="muted">Вопросов по фильтрам нет</td></tr>'};p.querySelectorAll('[data-question-search],[data-question-status],[data-question-from],[data-question-to]').forEach(x=>x.onchange=loadQuestions);p.querySelector('[data-question-search]').oninput=loadQuestions;p.querySelector('[data-question-refresh]').onclick=loadQuestions;p.querySelector('[data-question-run]').onclick=async()=>{const r=await fetch('/api/auto-questions/run',{method:'POST'});toast(r.ok?'Черновики вопросов обновлены':'Включите автоответы на вопросы');if(r.ok)await loadQuestions()};const settings=await (await fetch('/api/settings/questions')).json();p.querySelector('[data-question-enabled]').checked=!!settings.enabled;p.querySelector('[data-question-keywords]').checked=!!settings.use_keywords;p.querySelector('[data-question-ai]').checked=!!settings.use_ai;p.querySelector('[data-question-moderation]').checked=!!settings.require_moderation;p.querySelector('[data-question-name]').checked=!!settings.address_by_name;p.querySelector('[data-question-signature]').value=settings.signature||'';p.querySelector('[data-question-empty]').value=settings.no_answer_action||'manual';p.querySelector('[data-question-save]').onclick=async()=>{const body={enabled:p.querySelector('[data-question-enabled]').checked,use_keywords:p.querySelector('[data-question-keywords]').checked,use_ai:p.querySelector('[data-question-ai]').checked,require_moderation:p.querySelector('[data-question-moderation]').checked,address_by_name:p.querySelector('[data-question-name]').checked,signature:p.querySelector('[data-question-signature]').value,no_answer_action:p.querySelector('[data-question-empty]').value};const r=await fetch('/api/settings/questions',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast(r.ok?'Настройки вопросов сохранены':'Не удалось сохранить настройки')};p.querySelector('[data-question-tpl-save]').onclick=async()=>{const body={name:p.querySelector('[data-question-tpl-name]').value.trim(),keywords:p.querySelector('[data-question-tpl-keywords]').value.split(',').map(x=>x.trim()).filter(Boolean),text:p.querySelector('[data-question-tpl-text]').value,signature:p.querySelector('[data-question-tpl-signature]').value,kind:'question',topic:'general',sentiment:'any',group:'questions',enabled:true};if(!body.name||body.text.length<2){toast('Заполните название и текст шаблона');return}const r=await fetch('/api/templates/custom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast(r.ok?'Шаблон вопроса добавлен':'Не удалось сохранить шаблон')};await loadQuestions()}
const questionNav=[...document.querySelectorAll('.nav-group-title')].find(x=>x.textContent.includes('вопросами'));if(questionNav&&!document.querySelector('[data-nav="questions"]')){const link=document.createElement('a');link.href='#questionsPanel';link.className='nav-link sub';link.dataset.nav='questions';link.textContent='Все вопросы';link.onclick=()=>document.querySelector('#questionsPanel')?.scrollIntoView({behavior:'smooth'});questionNav.after(link)}
const questionsLoad=load;load=async function(){await questionsLoad();await enhanceQuestionsPanel()};
const simpleNavTargets={knowledge:'knowledgeHubPanel',recommendations:'recommendationsPanel',ai:'aiSettingsPanel'};Object.entries(simpleNavTargets).forEach(([nav,target])=>{const link=document.querySelector(`[data-nav="${nav}"]`);if(link)link.setAttribute('href','#'+target)});
const adminStyle=document.createElement('style');adminStyle.textContent='.template-row{padding:14px 0;border-bottom:1px solid #eef0f5}.template-row label{display:grid;grid-template-columns:190px 1fr auto;gap:12px;align-items:center}.template-row textarea{min-width:0;min-height:72px}.knowledge-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:14px 0}.knowledge-grid label{font-size:13px;font-weight:700}.knowledge-grid textarea{display:block;min-width:0;margin-top:6px}.report select{display:block;width:100%;margin-top:10px;height:36px}.module-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:14px 0}.module-fields label{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:700}.module-fields input{padding:10px;border:1px solid #d8d4e3;border-radius:9px;font:inherit}@media(max-width:760px){.template-row label,.knowledge-grid,.module-fields{grid-template-columns:1fr}.template-row .btn{width:100%}}';document.head.appendChild(adminStyle);
const layoutStyle=document.createElement('style');layoutStyle.textContent='.shell{max-width:none;padding:28px 38px 70px 315px}.sidebar{position:fixed;z-index:20;left:0;top:0;bottom:0;width:270px;padding:26px 18px 18px;background:#fff;border-right:1px solid #edf0f5;box-shadow:8px 0 28px #11182708;overflow:auto}.sidebar-brand{display:flex;align-items:center;gap:10px;padding:0 10px 27px}.sidebar-brand .logo{width:36px;height:36px;border-radius:11px}.sidebar-brand strong{display:block;letter-spacing:.04em;font-size:17px}.sidebar-brand span{display:block;color:#98a0ad;font-size:11px;margin-top:2px}.side-nav{display:flex;flex-direction:column;gap:3px}.nav-label{color:#a1a8b4;font-size:11px;font-weight:800;letter-spacing:.08em;margin:19px 10px 7px}.nav-link,.nav-group-title{display:flex;align-items:center;gap:10px;min-height:38px;padding:8px 11px;border-radius:9px;color:#4b5563;text-decoration:none;font-size:13px;font-weight:650}.nav-link:hover,.nav-link.active{background:#f0f1f5;color:#111827}.nav-link.active{box-shadow:inset 3px 0 #2563eb}.nav-link.sub{padding-left:34px;font-size:12px;font-weight:600}.nav-link em{font-style:normal;font-size:10px;margin-left:auto;padding:2px 5px;border-radius:5px;background:#f0f0f0;color:#8b93a1}.nav-plus{margin-left:auto;color:#9ca3af}.nav-group-title{justify-content:flex-start;color:#4b5563}.nav-group-title span:last-child{margin-left:auto;color:#9ca3af}.sidebar-footer{margin:24px 10px 0;padding-top:14px;border-top:1px solid #eef0f5;color:#a1a8b4;font-size:11px}.sidebar-footer span{float:right}.placeholder-panel{min-height:180px}.placeholder-panel h2{margin-bottom:8px}@media(max-width:900px){.shell{padding-left:92px}.sidebar{width:72px;padding:18px 9px}.sidebar-brand{padding:0 0 20px;justify-content:center}.sidebar-brand>div:last-child,.nav-link span,.nav-group-title span,.nav-label,.sidebar-footer{display:none}.nav-link,.nav-group-title{justify-content:center;padding:9px}.nav-link.sub{display:none}.nav-link em,.nav-plus{display:none}}';document.head.appendChild(layoutStyle);
const topStyle=document.createElement('style');topStyle.textContent='.top .brand>.logo{display:none}.top{padding-top:8px}';document.head.appendChild(topStyle);
function setupNavigation(){const main=document.querySelector('main.shell');if(!main)return;const missing={products:['Товары','Здесь будет каталог товаров, характеристики и привязка WB ID.'],cabinets:['Кабинеты маркетплейсов','Управление кабинетами и токенами будет доступно здесь.'],analytics:['Аналитика','Сводка по обращениям, публикациям, ошибкам и FBS.'],chats:['Управление чатами','Раздел для рабочих диалогов с покупателями.'],messengers:['Агенты в мессенджерах','Подключение уведомлений и агентов мессенджеров.'],profile:['Личный кабинет','Профиль пользователя и права доступа.'],finances:['Финансы','Финансовая сводка и расходы по кабинетам.']};Object.entries(missing).forEach(([id,[title,text]])=>{if(!document.getElementById(id)){const s=document.createElement('section');s.id=id;s.className='panel placeholder-panel';s.innerHTML=`<h2>${title}</h2><p class="muted">${text}</p><span class="status-chip">Раздел готовится</span>`;main.append(s)}});document.querySelectorAll('[data-nav]').forEach(link=>{link.onclick=e=>{const target=document.getElementById(link.getAttribute('href').slice(1));if(!target){e.preventDefault();toast('Раздел появится в следующем обновлении');return}document.querySelectorAll('[data-nav]').forEach(x=>x.classList.remove('active'));link.classList.add('active');setTimeout(()=>target.scrollIntoView({behavior:'smooth',block:'start'}),0)}})}setupNavigation();
function bindPanelIds(){document.querySelectorAll('.panel h2').forEach(h=>{const t=h.textContent.toLowerCase(),p=h.closest('.panel');if(!p.id&&t.includes('входящие'))p.id='inbox-panel';if(!p.id&&t.includes('отчёт'))p.id='fbs-panel';if(!p.id&&t.includes('очередь жалоб'))p.id='complaints-panel';if(!p.id&&t.includes('журнал действий'))p.id='audit-panel'});setupNavigation()}bindPanelIds();
const workflowLabels={new:'Новое',draft:'Черновик',ready:'Готово к отправке',sending:'Отправляется',error:'Нужна проверка',published:'Опубликовано'};let workflowPage=0;const workflowSize=20;
function workflowFilters(){const p=new URLSearchParams();const type=document.querySelector('#typeFilter')?.value,rating=document.querySelector('#ratingFilter')?.value,order=document.querySelector('#orderFilter')?.value,product=document.querySelector('#productFilter')?.value.trim(),search=document.querySelector('#dashboardSearch')?.value.trim(),status=document.querySelector('#statusFilter')?.value,topic=document.querySelector('#topicFilter')?.value,sentiment=document.querySelector('#sentimentFilter')?.value,dateFrom=document.querySelector('#dateFromFilter')?.value,dateTo=document.querySelector('#dateToFilter')?.value,sort=document.querySelector('#sortFilter')?.value;if(type)p.set('item_type',type);if(rating)p.set('rating',rating);if(order)p.set('order_status',order);if(product)p.set('product_id',product);if(search)p.set('search',search);if(status)p.set('status',status);if(topic)p.set('topic',topic);if(sentiment)p.set('sentiment',sentiment);if(dateFrom)p.set('date_from',dateFrom);if(dateTo)p.set('date_to',dateTo);if(sort)p.set('sort',sort);p.set('limit',workflowSize);p.set('offset',workflowPage*workflowSize);return p}
function workflowDate(value){const d=new Date(value);return Number.isNaN(d.valueOf())?'':d.toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'})}
function statusLabel(value){return workflowLabels[value]||value}
async function checkDraft(id,field){const r=await fetch('/api/replies/check/'+encodeURIComponent(id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:field.value})}),d=await r.json();if(d.ok)toast('Проверка пройдена: ответ можно отправлять');else toast('Проверьте: '+d.warnings.join('; '))}
async function regenerateDraft(id){const button=document.querySelector(`button[data-regenerate="${CSS.escape(id)}"]`);if(button){button.disabled=true;button.textContent='Обновление…'}try{const r=await fetch('/api/replies/drafts/'+encodeURIComponent(id)+'/regenerate',{method:'POST'});const data=await r.json().catch(()=>({}));if(!r.ok){toast(data.detail||'Не удалось перегенерировать ответ');return}toast('Черновик обновлён по шаблону');await load()}catch(e){toast('Не удалось обновить черновик')}finally{if(button){button.disabled=false;button.textContent='Обновить'}}}
function renderWorkflowRows(items){const body=document.querySelector('#inbox');if(!body)return;const labels={positive:'Позитив',negative:'Негатив',neutral:'Нейтральный'};body.innerHTML=items.length?items.map(x=>`<tr><td><input class="row-select" type="checkbox" value="${esc(x.id)}" onchange="updateSelectedCount()"></td><td><small class="badge">${x.item_type==='review'?'Отзыв':'Вопрос'}</small><br><span class="muted">${esc(workflowDate(x.created_at))}</span></td><td>${esc(x.text)||'<span class="muted">Без текста</span>'}<br><span class="muted">WB ID: ${esc(x.product_id||'—')}</span><div class="tag-row">${(x.topics||[]).map(t=>`<span class="review-tag topic-${esc(t)}">${esc({quality:'Качество',size:'Размер',delivery:'Доставка',packaging:'Упаковка',material:'Материал',color:'Цвет',price:'Цена',texture:'Текстура',general:'Общее'}[t]||t)}</span>`).join('')}</div></td><td><b>${x.rating??'—'} ${x.rating?'★':''}</b><br><span class="sentiment ${esc(x.sentiment||'neutral')}">${labels[x.sentiment]||'Нейтральный'}</span><br>${x.item_type==='review'?`<small class="purchase ${esc(x.order_status)}">${esc(orderLabels[x.order_status]||orderLabels.notSpecified)}</small>`:'<span class="muted">Не применимо</span>'}</td><td><div class="moderation-line"><span class="status-chip ${x.published?'sent':''}">${esc(statusLabel(x.status))}</span><span class="moderation-status">${esc({pending:'На модерации',approved:'Подтверждён',rejected:'Отклонён',rework:'На доработке'}[x.moderation_status]||'На модерации')}</span></div><textarea data-draft="${esc(x.id)}" data-original="${esc(x.draft_text)}" ${x.published||x.status==='sending'?'disabled':''}>${esc(x.draft_text)}</textarea><div class="draft-actions">${x.published?'':`<button class="btn secondary" onclick="saveDraft('${esc(x.id)}',this.closest('td').querySelector('textarea'),this)">Сохранить</button><button class="btn secondary" onclick="checkDraft('${esc(x.id)}',this.closest('td').querySelector('textarea'))">Проверить</button><button class="btn secondary" onclick="moderateDraft('${esc(x.id)}','approve')">Подтвердить</button><button class="btn secondary" onclick="moderateDraft('${esc(x.id)}','rework')">На доработку</button><button class="btn secondary" data-regenerate="${esc(x.id)}" onclick="regenerateDraft('${esc(x.id)}')">Обновить</button>`}</div></td><td>${x.published?'<span class="muted">Ответ опубликован</span>':`<button class="btn secondary" onclick="sendDraft('${esc(x.id)}',this)">Отправить</button>`}</td></tr>`).join(''):'<tr><td colspan="6" class="muted">По текущим фильтрам ничего не найдено.</td></tr>';const pager=document.querySelector('#workflowPager')||document.createElement('div');pager.id='workflowPager';pager.className='toolbar';pager.innerHTML=`<button class="btn secondary" ${workflowPage===0?'disabled':''} onclick="workflowPage--;load()">← Предыдущие</button><span class="dashboard-extra">Страница ${workflowPage+1} · показано ${items.length}</span><button class="btn secondary" ${items.length<workflowSize?'disabled':''} onclick="workflowPage++;load()">Следующие →</button>`;if(!pager.parentElement)body.closest('.table-wrap').after(pager)}
function selectedRows(){return [...document.querySelectorAll('.row-select:checked')].map(x=>x.value)}function updateSelectedCount(){const values=selectedRows();const node=document.querySelector('[data-selected-count]');if(node)node.textContent='Выбрано: '+values.length}function selectAllRows(checked){document.querySelectorAll('.row-select').forEach(x=>x.checked=checked);updateSelectedCount()}function toggleGroupBar(){const bar=document.querySelector('#groupBar');bar.hidden=!bar.hidden}async function groupAction(action){const item_ids=selectedRows();if(!item_ids.length){toast('Выберите хотя бы один отзыв');return}const r=await fetch('/api/replies/group-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids,action})}),d=await r.json();toast(r.ok?'Групповое действие выполнено':'Не удалось выполнить действие');if(r.ok)await load()}async function moderateDraft(id,action){const r=await fetch('/api/replies/drafts/'+encodeURIComponent(id)+'/moderate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});toast(r.ok?(action==='approve'?'Ответ подтверждён':'Ответ отправлен на доработку'):'Не удалось изменить модерацию');if(r.ok)await load()}
async function renderWorkflow(){try{const r=await fetch('/api/inbox/overview?'+workflowFilters());if(!r.ok)return;const items=await r.json();renderWorkflowRows(items);const count=document.querySelector('#inboxCount');if(count)count.textContent=`Показано ${items.length} · страница ${workflowPage+1}`;updateDashboardCounters()}catch(e){toast('Не удалось загрузить рабочую очередь')}}
const workflowLoad=load;load=async function(){await workflowLoad();await renderWorkflow()};
const workflowStatus=document.querySelector('#statusFilter');if(workflowStatus)workflowStatus.innerHTML='<option value="">Все статусы</option><option value="new">Новые</option><option value="draft">Черновики</option><option value="ready">Готовы к отправке</option><option value="error">Нужна проверка</option><option value="published">Отправленные</option>';
setInterval(updateSyncStatus,30000);load();
</script><script>
(()=>{const ready=()=>{const root=document.querySelector('#chats');if(!root||root.dataset.chatReady)return;root.dataset.chatReady='1';root.className='panel chats-workspace';root.innerHTML='<div class="chat-layout"><section><div class="chat-head"><div><h2>Чаты с покупателями</h2><p class="muted">Единая история сообщений из подключённых маркетплейсов.</p></div><button class="btn secondary" data-chat-refresh>Обновить</button></div><div class="toolbar"><input data-chat-search placeholder="Поиск по товару или истории переписки"><select data-chat-market><option value="">Все площадки</option><option value="wildberries">Wildberries</option><option value="ozon">Ozon</option><option value="yandex">Яндекс Маркет</option></select><select data-chat-status><option value="">Все статусы</option><option value="waiting">Ждут ответа</option><option value="answered">Отвеченные</option><option value="closed">Закрытые</option></select></div><div data-chat-list>Загрузка…</div></section><section data-chat-thread class="chat-thread"><div class="empty-state">Выберите диалог слева</div></section></div><hr><h3>Настройки чатов</h3><div class="settings-checks"><label><input type="checkbox" data-chat-setting="auto_generate"> Генерировать ответы</label><label><input type="checkbox" data-chat-setting="auto_send"> Отправлять автоматически</label><label><input type="checkbox" data-chat-setting="manager_redirect"> Перенаправлять менеджеру</label><label><input type="checkbox" data-chat-setting="off_hours_enabled"> Ответ в нерабочие часы</label></div><div class="settings-grid"><input data-chat-setting="manager_contact" placeholder="Контакт менеджера"><textarea data-chat-setting="off_hours_template" placeholder="Шаблон для нерабочих часов"></textarea></div><button class="btn" data-chat-save>Сохранить настройки</button><h3>Шаблоны рассылок</h3><div class="settings-grid"><textarea data-broadcast="new_order" placeholder="Шаблон нового заказа"></textarea><textarea data-broadcast="delivery" placeholder="Шаблон уведомления по доставке"></textarea></div><button class="btn secondary" data-broadcast-save>Сохранить шаблоны</button>';const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const load=async()=>{const q=new URLSearchParams(),s=root.querySelector('[data-chat-search]').value.trim(),m=root.querySelector('[data-chat-market]').value,st=root.querySelector('[data-chat-status]').value;if(s)q.set('search',s);if(m)q.set('marketplace',m);if(st)q.set('status',st);const r=await fetch('/api/chats?'+q),items=r.ok?await r.json():[];root.querySelector('[data-chat-list]').innerHTML=items.map(x=>{const last=(x.messages||[]).slice(-1)[0]||{};return '<button class="chat-card" data-chat-id="'+esc(x.id)+'"><b>'+esc(x.product_name||'Диалог')+'</b><small>'+esc(x.marketplace||'')+' · '+esc(x.product_id||'')+'</small><p>'+esc(last.text||'Нет сообщений')+'</p></button>'}).join('')||'<p class="muted">Чаты не найдены. Подключите адаптер маркетплейса или выполните импорт.</p>';root.querySelectorAll('[data-chat-id]').forEach(b=>b.onclick=async()=>{const c=await (await fetch('/api/chats/'+encodeURIComponent(b.dataset.chatId))).json();root.querySelector('[data-chat-thread]').innerHTML='<h3>'+esc(c.product_name||'Чат')+'</h3><div class="messages">'+(c.messages||[]).map(x=>'<div class="message '+(x.sender==='seller'?'seller':'buyer')+'"><b>'+esc(x.sender==='seller'?'Магазин':'Покупатель')+'</b><p>'+esc(x.text)+'</p></div>').join('')+'</div><textarea data-chat-text placeholder="Введите сообщение…"></textarea><button class="btn" data-chat-send>Отправить</button>';root.querySelector('[data-chat-send]').onclick=async()=>{const text=root.querySelector('[data-chat-text]').value;if(!text.trim())return;const z=await fetch('/api/chats/'+encodeURIComponent(c.id)+'/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});toast(z.ok?'Сообщение сохранено':'Ошибка сохранения');if(z.ok){await b.click();await load()}}})};root.querySelectorAll('[data-chat-search],[data-chat-market],[data-chat-status]').forEach(x=>x.oninput=load);root.querySelector('[data-chat-refresh]').onclick=load;const ms=async(name)=>{const r=await fetch('/api/settings/modules/'+name);return (await r.json()).values||{}};const save=async(name,v)=>{await fetch('/api/settings/modules/'+name,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:v})});toast('Настройки сохранены')};ms('chats').then(v=>root.querySelectorAll('[data-chat-setting]').forEach(x=>x.type==='checkbox'?x.checked=!!v[x.dataset.chatSetting]:x.value=v[x.dataset.chatSetting]||''));ms('broadcasts').then(v=>root.querySelectorAll('[data-broadcast]').forEach(x=>x.value=v[x.dataset.broadcast]||''));root.querySelector('[data-chat-save]').onclick=()=>{const v={};root.querySelectorAll('[data-chat-setting]').forEach(x=>v[x.dataset.chatSetting]=x.type==='checkbox'?x.checked:x.value);return save('chats',v)};root.querySelector('[data-broadcast-save]').onclick=()=>{const v={};root.querySelectorAll('[data-broadcast]').forEach(x=>v[x.dataset.broadcast]=x.value);return save('broadcasts',v)};load()};document.readyState==='loading'?document.addEventListener('DOMContentLoaded',ready):ready()})();
</script><script>(()=>{const clean=()=>{document.querySelectorAll('#inbox tr').forEach(row=>{const saves=[...row.querySelectorAll('button')].filter(x=>x.textContent.trim()==='Сохранить');saves.slice(1).forEach(x=>x.closest('.draft-actions')?.remove());row.querySelectorAll('.draft-actions').forEach(x=>{x.style.display='flex';x.style.flexWrap='wrap';x.style.gap='6px';x.style.alignItems='center'})})};new MutationObserver(clean).observe(document.querySelector('#inbox')||document.body,{childList:true,subtree:true});clean()})();</script></body></html>"""


for _page in ("products", "cabinets", "knowledge", "analytics", "reviews", "templates", "recommendations", "settings", "ai", "import", "questions", "chats", "messengers", "profile", "finances"):
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


@app.get("/api/cabinets")
def list_cabinets() -> list[dict[str, object]]:
    return [{"id": cabinet.id, "name": cabinet.name, "token_configured": bool(cabinet.token)} for cabinet in load_cabinets(settings.cabinets_json)]


@app.post("/api/sync/{cabinet_id}")
async def sync_cabinet(cabinet_id: str) -> dict[str, object]:
    try:
        return await perform_sync(cabinet_id)
    except WBApiError as exc:
        draft_store.set_setting("sync_last_error", str(exc)[:500])
        status = 404 if "не найден" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/api/import/reviews")
async def import_reviews(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект настроек импорта")
    cabinet_ids = payload.get("cabinet_ids") or [cabinet.id for cabinet in load_cabinets(settings.cabinets_json)]
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    with_markup = bool(payload.get("with_markup", True))
    imported = 0
    results = []
    for cabinet_id in cabinet_ids:
        cabinets = {cabinet.id: cabinet for cabinet in load_cabinets(settings.cabinets_json)}
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
def inbox_overview(item_type: str | None = Query(default=None, pattern="^(review|question)$"), rating: int | None = Query(default=None, ge=1, le=5), product_id: int | None = Query(default=None), order_status: OrderStatus | None = Query(default=None), search: str | None = Query(default=None, max_length=100), status: str | None = Query(default=None, pattern="^(new|draft|ready|sending|error|published)$"), topic: str | None = Query(default=None, max_length=40), sentiment: str | None = Query(default=None, pattern="^(positive|negative|neutral)$"), date_from: str | None = Query(default=None, max_length=10), date_to: str | None = Query(default=None, max_length=10), sort: str = Query(default="desc", pattern="^(asc|desc)$"), limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> list[dict[str, object]]:
    return draft_store.list_inbox_overview(item_type=item_type, rating=rating, product_id=product_id, order_status=order_status, search=search, status=status, topic=topic, sentiment=sentiment, date_from=date_from, date_to=date_to, sort=sort, limit=limit, offset=offset)


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
async def send_review_batch(token: str) -> dict:
    return bulk_sender.start(token)


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
