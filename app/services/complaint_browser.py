"""Dedicated visible WB browser. No existing cookies or passwords are read.

Playwright lives on one worker thread (also works with Windows uvicorn reload).
Preparation never submits a complaint. Submission requires an expiring preview,
explicit confirmation, exact visible feedback ID and unchanged form/account.
"""
import asyncio
import importlib.util
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import json
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field
from app.models import ComplaintStatus


class ComplaintPrepareRequest(BaseModel):
    text: str = Field(min_length=50, max_length=1000)
    use_selected: bool = False


class ComplaintSubmitRequest(BaseModel):
    token: str
    confirmed: bool = False


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_wb_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return urlsplit(url).scheme == "https" and (host == "wildberries.ru" or host.endswith(".wildberries.ru"))


def redact(text: str) -> str:
    text = re.sub(r"(?:\+7|8)[\s()-]*\d{3}[\s()-]*\d{3}[\s()-]*\d{2}[\s()-]*\d{2}", "[телефон скрыт]", text)
    return re.sub(r"\b[A-Za-z0-9_-]{45,}\b", "[длинный идентификатор скрыт]", text)

REVIEWS_URL = "https://seller.wildberries.ru/feedbacks/feedbacks-tab/not-answered"

def normalized(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def review_fragments(text: str) -> list[str]:
    # API uses Достоинства/Недостатки; the seller UI shows Плюсы/Минусы.
    return [normalized(re.sub(r"^(?:Достоинства|Недостатки|Плюсы|Минусы|Комментарий):\s*", "", line, flags=re.I))
            for line in text.splitlines() if line.strip()]


class ComplaintBrowser:
    def __init__(self, project_root=None, store=None):
        self.root = Path(project_root or Path.cwd())
        self.profile = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "WBOperations" / "complaints-browser"
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-complaints")
        self.context = None
        self.driver = None
        self.page = None
        self.task = None
        self.state = "disconnected"
        self.message = "Для подачи жалоб нужен вход в отдельный браузер WB."
        self.installed_version = None
        self.automation_ready = False
        self.store = store
        self.selected_item = None
        self.error_type = None
        self.error_detail = None
        self.pending = None
        self.lock = asyncio.Lock()
        self.submitted_count = 0
        try:
            self.installed_version = version("playwright")
        except PackageNotFoundError:
            pass

    def snapshot(self):
        return {"status": self.state, "message": self.message,
                "playwright_version": self.installed_version,
                "browser_open": self.context is not None,
                "automation_ready": self.automation_ready,
                "error_type": self.error_type, "error_detail": self.error_detail,
                "complaints_submitted_by_browser": self.submitted_count}

    def start(self):
        if self.task and not self.task.done():
            return self.snapshot()
        if self.context is not None:
            return self.snapshot()
        self.state = "starting"
        self.message = "Подготовка отдельного браузера WB…"
        self.task = asyncio.create_task(self._connect())
        return self.snapshot()

    async def _connect(self):
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self._setup)
        except Exception as exc:
            self.state = "error"
            self.message = "Не удалось открыть браузер WB. Проверьте установку Playwright и доступность Chrome."
            self.error_type = type(exc).__name__
            self.error_detail = redact(str(exc))[:600]

    def _install_staged(self):
        # Installing *.py files directly into a watched venv can restart uvicorn
        # before pip writes dist-info. Prepare outside the watched project first.
        staging = Path(tempfile.mkdtemp(prefix="wb-playwright-setup-"))
        try:
            process = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "--target", str(staging), "playwright"],
                capture_output=True, timeout=240,
            )
            if process.returncode:
                raise RuntimeError("Установка Playwright во временную папку не завершилась")
            metadata = list(staging.glob("playwright-*.dist-info"))
            if not metadata or not (staging / "playwright" / "sync_api").exists():
                raise RuntimeError("Пакет Playwright установлен не полностью")
            target = next(Path(p) for p in site.getsitepackages() if Path(p).name.lower() == "site-packages")
            if not target.resolve().is_relative_to((self.root / ".venv").resolve()):
                raise RuntimeError("Сервис должен быть запущен из виртуального окружения проекта .venv")
            previous = staging / "_previous"
            previous.mkdir()
            for entry in list(staging.iterdir()):
                if entry.name in ("_previous", "bin"):
                    continue
                destination = target / entry.name
                if destination.exists():
                    if not entry.name.startswith("playwright"):
                        continue
                    # Preserve the incomplete old package until its replacement
                    # has been moved into place, without deleting user files.
                    destination.rename(previous / entry.name)
                try:
                    entry.rename(destination)
                except Exception:
                    saved = previous / entry.name
                    if saved.exists() and not destination.exists():
                        saved.rename(destination)
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _setup(self):
        self.error_type = self.error_detail = None
        try:
            installed = version("playwright")
        except PackageNotFoundError:
            installed = None
        if not installed or importlib.util.find_spec("playwright") is None:
            self.state = "installing"
            self.message = "Устанавливается разрешённый Playwright в окружение проекта…"
            self._install_staged()
            self.installed_version = version("playwright")
            self.state = "installed"
            self.message = "Playwright установлен. Нажмите «Подключить браузер WB», чтобы открыть Chrome."
            return
        self.installed_version = version("playwright")
        self.state = "opening_browser"
        self.message = "Открывается отдельный Chrome для Wildberries…"
        from playwright.sync_api import sync_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        self.driver = sync_playwright().start()
        self.context = self.driver.chromium.launch_persistent_context(
            str(self.profile), channel="chrome", headless=False,
            viewport={"width": 1360, "height": 900},
            locale="ru-RU", accept_downloads=False,
        )
        self.context.set_default_timeout(15000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(REVIEWS_URL, wait_until="domcontentloaded", timeout=60000)
        self.page.bring_to_front()
        self.state = "waiting_login"
        self.message = "Войдите в WB в открытом Chrome, выберите нужный кабинет и откройте раздел отзывов. Пароли и SMS-коды вводите только в браузере."

    async def inspect(self):
        if self.state in ("starting", "installing", "opening_browser"):
            raise HTTPException(409, "Браузер ещё подготавливается")
        if self.context is None:
            raise HTTPException(409, "Сначала подключите браузер WB")
        try:
            return await asyncio.get_running_loop().run_in_executor(self.executor, self._inspect)
        except HTTPException:
            raise
        except Exception as exc:
            self.state = "error"
            self.message = "Браузер закрыт или недоступен. Нажмите подключение заново."
            self.context = None
            raise HTTPException(503, self.message) from exc

    async def reviews(self):
        if self.context is None:
            if self.task and not self.task.done():
                await self.task
            if self.context is None:
                await self._connect()
        if self.context is None:
            raise HTTPException(503, self.message)
        return await asyncio.get_running_loop().run_in_executor(self.executor, self._navigate_reviews)

    def _navigate_reviews(self):
        self._inspect()
        if not self.page or self.page.is_closed() or not is_wb_url(self.page.url):
            raise HTTPException(409, "В браузере должна быть открыта страница WB")
        entry = self.page.get_by_role("button", name=re.compile(r"^Ответить на отзывы"))
        if urlsplit(self.page.url).path in ("", "/"):
            try:
                entry.first.wait_for(state="visible", timeout=20000)
            except Exception:
                return self._inspect()
        if entry.count() == 1:
            entry.click()
            try:
                self.page.get_by_text("Ждут ответа", exact=True).first.wait_for(timeout=15000)
            except Exception:
                pass
        return self._inspect()

    async def open_review(self, item):
        self.pending = None
        if self.context is None:
            await self.reviews()
        try:
            return await asyncio.get_running_loop().run_in_executor(self.executor, self._open_review, item)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, "Не удалось подготовить форму WB. Проверьте вход, закройте подсказки или откройте нужный отзыв вручную в отдельном Chrome.") from exc

    def _open_review(self, item):
        self._inspect()
        self.page.goto(REVIEWS_URL, wait_until="domcontentloaded", timeout=60000)
        search = self.page.get_by_placeholder("Поиск по Артикулам WB")
        search.wait_for(timeout=20000)
        tutorial = self.page.get_by_role("button", name="Понятно", exact=True)
        if tutorial.count() == 1 and tutorial.is_visible():
            tutorial.click()
        search.fill(str(item.product_id))
        search.press("Enter")
        prefix = (review_fragments(item.text) or [""])[0][:100]
        candidates = []
        for answered in (False, True):
            if answered:
                self.page.get_by_text("Есть ответ", exact=True).click()
                search.fill(str(item.product_id))
                search.press("Enter")
            try:
                self.page.wait_for_function(
                    """product => Array.from(document.querySelectorAll('tr.Table-item')).some(x=>x.innerText.includes(product))""",
                    arg=str(item.product_id), timeout=10000,
                )
            except Exception:
                continue
            for row in self.page.locator("tr.Table-item").all():
                text = row.inner_text()
                if str(item.product_id) in text and len(prefix) >= 10 and prefix in normalized(text):
                    candidates.append(row)
            if candidates:
                break
        self.selected_item = item
        if len(candidates) != 1:
            return {**self._inspect(), "selection_status": "needs_manual_selection",
                    "item_id": item.id, "candidate_count": len(candidates)}
        candidates[0].click()
        self.page.get_by_role("button", name="Ещё", exact=True).first.wait_for(timeout=15000)
        self.page.get_by_role("button", name="Ещё", exact=True).click()
        self._verify_identity(item)
        self._open_form()
        return {**self._inspect(), "selection_status": "review_open", "item_id": item.id}

    def _verify_identity(self, item):
        if not self.page or self.page.is_closed() or not is_wb_url(self.page.url):
            raise HTTPException(409, "Откройте отзыв в отдельном браузере WB")
        identity = self.page.get_by_role("button", name=f"ID: {item.id}", exact=True)
        if identity.count() != 1 or not identity.is_visible():
            raise HTTPException(409, "ID открытого отзыва не совпадает. Откройте нужный отзыв и нажмите «Ещё», чтобы показать ID.")
        text = normalized(self.page.locator("body").inner_text())
        if any(fragment not in text for fragment in review_fragments(item.text)) or str(item.product_id) not in text:
            raise HTTPException(409, "Текст или товар открытого отзыва изменились. Синхронизируйте данные и проверьте отзыв.")

    def _open_form(self):
        other = self.page.locator('input[type=radio][name="Другое"]')
        if other.count() and other.is_visible():
            other.check()
            return
        actions = self.page.locator('[class*="Feedback-actions-buttons-view"] button')
        if actions.count() != 2:
            raise HTTPException(409, "WB не показывает кнопку жалобы. Возможно, она уже подана. Проверьте «Мои жалобы».")
        actions.first.click()
        other.wait_for(timeout=15000)
        other.check()
        self.page.locator('textarea[name="explanation"]').wait_for(timeout=10000)

    def _check_eligible(self, item):
        if item.item_type.value != "review" or item.rating not in (1, 2) or not item.product_id:
            raise HTTPException(400, "Нужен отзыв 1–2 звезды с WB ID")
        state = self.store.get_setting("complaint_delivery:" + item.id, "") if self.store else ""
        if state:
            raise HTTPException(409, "Жалоба уже подана либо предыдущая отправка требует проверки в «Мои жалобы». Повтор заблокирован.")
        if self.store:
            case = next((c for c in self.store.list_complaints() if c.item_id == item.id), None)
            if case is None or case.status in (ComplaintStatus.SUBMITTED_MANUAL, ComplaintStatus.SUBMITTED_BROWSER, ComplaintStatus.DISMISSED):
                raise HTTPException(409, "Карточка закрыта или жалоба уже подана")

    async def prepare(self, item, request):
        async with self.lock:
            self.pending = None
            self._check_eligible(item)
            if not 50 <= len(request.text.strip()) <= 1000:
                raise HTTPException(400, "Текст жалобы должен содержать 50–1000 символов")
            if not request.use_selected:
                opened = await self.open_review(item)
                if opened.get("selection_status") != "review_open":
                    raise HTTPException(409, "Автопоиск не нашёл точный отзыв на текущей странице. Откройте его вручную в отдельном Chrome, нажмите «Ещё» и используйте проверку открытого отзыва.")
            elif self.context is None:
                raise HTTPException(409, "Сначала подключите отдельный браузер WB")
            try:
                return await asyncio.get_running_loop().run_in_executor(self.executor, self._prepare, item, request.text.strip())
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(409, "Форма WB недоступна. Проверьте её в Chrome; жалоба не отправлена.") from exc

    def _cabinet(self):
        account = self.page.get_by_test_id("desktop-profile-select-button-chips-component")
        if account.count() != 1 or not account.is_visible():
            raise HTTPException(409, "Не удалось проверить выбранный кабинет WB")
        return account.inner_text().strip()

    def _prepare(self, item, text):
        self._inspect()
        self._verify_identity(item)
        self._open_form()
        self.page.locator('textarea[name="explanation"]').fill(text)
        button = self.page.locator('[class*="Complaint-form__buttons"] button').filter(has_text=re.compile("^Отправить$"))
        if button.count() != 1 or not button.is_enabled():
            raise HTTPException(409, "WB не разрешил отправку. Проверьте поля и ограничения в браузере.")
        if self.page.locator('input[type=file][name="photos"]').evaluate_all("(xs)=>xs.some(x=>x.files?.length>0)"):
            raise HTTPException(409, "В форме есть вложение. Удалите его или подайте жалобу вручную: вложения не включены в подтверждение.")
        data = {"token": uuid4().hex, "item_id": item.id, "product_id": item.product_id,
                "cabinet_id": item.cabinet_id, "cabinet": self._cabinet(),
                "destination": "Модерация отзывов Wildberries",
                "reason": "Другое", "text": text, "review_text": item.text,
                "created_at": time.time(), "expires_in": 300}
        self.pending = data
        self.automation_ready = True
        self.message = "Форма WB проверена. Отправка только после подтверждения конкретной жалобы."
        return data

    def _validate_pending(self, request):
        data = self.pending
        if not request.confirmed:
            raise HTTPException(400, "Необходимо явное подтверждение отправки")
        if not data or request.token != data["token"] or time.time() - data["created_at"] > 300:
            raise HTTPException(409, "Предпросмотр устарел. Подготовьте жалобу заново.")
        item = self.store.get_inbox(data["item_id"])
        if item is None or item.text != data["review_text"] or item.product_id != data["product_id"] or item.cabinet_id != data["cabinet_id"]:
            raise HTTPException(409, "Отзыв изменился после проверки")
        self._check_eligible(item)
        return data, item

    async def submit(self, request):
        async with self.lock:
            data, item = self._validate_pending(request)
            return await asyncio.get_running_loop().run_in_executor(self.executor, self._submit, data, item)

    def _claim(self, item_id):
        with self.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            key = "complaint_delivery:" + item_id
            if c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone():
                raise HTTPException(409, "Повторная отправка заблокирована")
            # Set before clicking: a crash/restart must never cause an auto-retry.
            c.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, "uncertain"))

    def _submit(self, data, item):
        self._verify_identity(item)
        if self._cabinet() != data["cabinet"]:
            raise HTTPException(409, "Кабинет WB изменился. Подготовьте жалобу заново.")
        other = self.page.locator('input[type=radio][name="Другое"]')
        field = self.page.locator('textarea[name="explanation"]')
        if not other.is_checked() or field.input_value() != data["text"]:
            raise HTTPException(409, "Причина или текст в форме WB изменены. Повторите предпросмотр.")
        if self.page.locator('input[type=file][name="photos"]').evaluate_all("(xs)=>xs.some(x=>x.files?.length>0)"):
            raise HTTPException(409, "Вложения изменились. Отправка отменена.")
        button = self.page.locator('[class*="Complaint-form__buttons"] button').filter(has_text=re.compile("^Отправить$"))
        if button.count() != 1 or not button.is_enabled():
            raise HTTPException(409, "Кнопка отправки WB недоступна")
        before = self.page.locator("body").inner_text().lower()
        success = ("жалоба отправлена", "жалоба успешно отправлена", "жалоба принята")
        if any(s in before for s in success):
            raise HTTPException(409, "В WB уже есть сообщение об отправке. Проверьте «Мои жалобы».")
        self._claim(item.id)
        self.pending = None
        self.store.audit("complaint_browser_attempt", item.id, json.dumps(data, ensure_ascii=False))
        try:
            button.click()
            self.page.wait_for_function("""() => {
                const t=document.body.innerText.toLowerCase();
                return ['жалоба отправлена','жалоба успешно отправлена','жалоба принята'].some(s=>t.includes(s));
            }""", timeout=20000)
            # A click or form closing alone is NOT proof of delivery.
            self.store.set_setting("complaint_delivery:" + item.id, "sent")
            self.store.update_complaint_status(item.id, ComplaintStatus.SUBMITTED_BROWSER)
            self.store.audit("complaint_browser_sent", item.id, "WB показал подтверждение подачи")
            self.submitted_count += 1
            return {"status": "sent", "item_id": item.id, "message": "WB подтвердил подачу жалобы."}
        except Exception:
            self.store.audit("complaint_browser_uncertain", item.id, "Нет однозначного подтверждения WB; повтор заблокирован")
            return {"status": "uncertain", "item_id": item.id,
                    "message": "Не удалось однозначно подтвердить результат. Проверьте «Мои жалобы» в WB. Повторная отправка заблокирована."}

    def _inspect(self):
        pages = [p for p in self.context.pages if not p.is_closed() and is_wb_url(p.url)]
        if not pages:
            raise HTTPException(409, "В отдельном браузере не открыта страница WB")
        self.page = pages[-1]
        # Visible DOM only. No cookies, storage, request headers or input values.
        text = redact(self.page.locator("body").inner_text(timeout=10000))[:10000]
        controls = self.page.evaluate("""() => ({
          buttons:Array.from(document.querySelectorAll('button,[role=button],[role=menuitem]')).filter(x=>x.getClientRects().length).map(x=>({text:x.innerText||x.getAttribute('aria-label')||'',role:x.getAttribute('role')||'button',label:x.getAttribute('aria-label')||'',testid:x.getAttribute('data-testid')||'',class:x.className||'',parentClass:x.parentElement.className||'',icon:x.querySelector('svg')?.outerHTML.slice(0,1500)||''})).slice(0,200),
          fields:Array.from(document.querySelectorAll('input,textarea,select')).filter(x=>x.getClientRects().length).map(x=>({tag:x.tagName.toLowerCase(),type:x.type||'',placeholder:x.getAttribute('placeholder')||'',label:x.getAttribute('aria-label')||'',name:x.name||'',id:x.id||''})).filter(x=>x.type!=='password'&&x.type!=='hidden').slice(0,40),
          links:Array.from(document.querySelectorAll('a[href]')).filter(x=>x.getClientRects().length).map(x=>({text:x.innerText||'',href:(x.href||'').split('?')[0].split('#')[0]})).filter(x=>x.text).slice(0,50),
          rows:Array.from(document.querySelectorAll('tr,[role=row]')).filter(x=>x.getClientRects().length).map(x=>({text:x.innerText.slice(0,2500),id:x.id||'',class:x.className||'',attrs:Object.fromEntries(Array.from(x.attributes).filter(a=>a.name.startsWith('data-')).map(a=>[a.name,a.value])),checkboxes:Array.from(x.querySelectorAll('input[type=checkbox]')).map(y=>({name:y.name,value:y.value})),buttons:Array.from(x.querySelectorAll('button')).map(y=>({text:y.innerText,label:y.getAttribute('aria-label')||'',title:y.title||'',class:y.className}))})).slice(0,20),
          dialogs:Array.from(document.querySelectorAll('[role=dialog]')).filter(x=>x.getClientRects().length).map(x=>({text:x.innerText.slice(0,6000),class:x.className})).slice(0,3)
        })""")
        login = any(word in text.lower() for word in ("введите код", "получить код", "войти по номеру", "введите номер телефона"))
        self.state = "waiting_login" if login else "browser_connected"
        self.message = ("Завершите вход в WB в открытом Chrome." if login else
                        "Браузер WB подключён. Для отправки подготовьте конкретную жалобу и подтвердите текст.")
        if urlsplit(self.page.url).path in ("", "/") and not login:
            # Avoid exporting unrelated account finance widgets on the homepage.
            text = ("Главная страница WB. Для работы откройте раздел отзывов." if "Баланс" in text
                    else text[:3000])
            controls["buttons"] = [b for b in controls["buttons"] if any(w in b["text"].lower() for w in ("отзыв", "войти", "загруз", "обнов", "провер"))]
            controls["links"] = [l for l in controls["links"] if "отзыв" in l["text"].lower()]
            controls["rows"] = []
        return {**self.snapshot(), "page_url": safe_url(self.page.url),
                "page_title": redact(self.page.title()), "visible_text": text, "controls": controls}

    async def close(self):
        if self.task and not self.task.done():
            # Do not cancel a running package installation or thread mid-operation.
            await self.task
        await asyncio.get_running_loop().run_in_executor(self.executor, self._close)
        self.executor.shutdown(wait=False)

    def _close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.driver:
            try:
                self.driver.stop()
            except Exception:
                pass
        self.context = self.driver = self.page = None
