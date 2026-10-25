"""ОПиУ (отчёт о прибылях и убытках) маркетплейсов.

Расчёт — скрипты в папке opiu (перенесены из mp_opiu_excel). Отчёт собирает opiu_all.py, здесь
читается готовый opiu_all_*.xlsx (листы «Свод», «Контроль», «Товары») и запускается пересборка.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

SUMMARY_SHEET = "Свод"
CONTROL_SHEET = "Контроль"
GOODS_SHEET = "Товары"
MARKETPLACE_NAMES = {"Wildberries", "Ozon", "Яндекс Маркет"}
TOTAL_PREFIXES = ("ИТОГО", "ВАЛОВАЯ", "ПРИБЫЛЬ", "Выручка за вычетом", "Итого расходы")
KPI_LABELS = {
    "revenue": "Выручка за вычетом возвратов",
    "mp_expenses": "Итого расходы маркетплейсов",
    "mp_result": "ИТОГО: РЕЗУЛЬТАТ ПО ДАННЫМ МАРКЕТПЛЕЙСОВ",
    "cogs": "Себестоимость проданных товаров",
    "gross": "ВАЛОВАЯ ПРИБЫЛЬ (после себестоимости)",
    "prep": "Подготовка товара, ₽",
    "tax": "Налог с выручки, ₽",
    "profit": "ПРИБЫЛЬ (после себестоимости, подготовки и налога)",
}
OK_STATUSES = {"", "OK", "ОК"}
# Движок ОПиУ живёт внутри WB OPS: папка opiu рядом с app (свои скрипты, настройки кабинетов, cost.csv, кэш).
ENGINE_DIR = Path(__file__).resolve().parents[2] / "opiu"
DEFAULT_DIR_CANDIDATES = (ENGINE_DIR,)


def default_python() -> str:
    """Python для opiu_all.py: тот же, что у WB OPS, если в нём есть openpyxl, иначе системный."""
    import importlib.util
    return sys.executable if importlib.util.find_spec("openpyxl") else "python"


class FinanceError(Exception):
    pass


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return "" if value is None else str(value).strip()


def find_project_dir(configured: str | None) -> Path | None:
    candidates = [Path(configured)] if configured else list(DEFAULT_DIR_CANDIDATES)
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if (path / "opiu_all.py").is_file():
            return path
    return None


def latest_report(project: Path) -> Path | None:
    """Самый свежий сводный ОПиУ по месяцам (без недельных, дневных и юнит-экономики)."""
    files = [p for p in project.glob("opiu_all_*.xlsx") if not p.name.startswith("~$")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


class _XlsxSheet:
    def __init__(self, rows):
        self._rows = rows

    def iter_rows(self, values_only=True, **_):
        return iter(self._rows)


class _XlsxBook:
    """Минимальное чтение .xlsx стандартной библиотекой (значения ячеек), если openpyxl не установлен."""
    NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "p": "http://schemas.openxmlformats.org/package/2006/relationships"}

    def __init__(self, path: Path):
        import zipfile
        import xml.etree.ElementTree as ET
        self._zip = zipfile.ZipFile(path)
        self._ET = ET
        names = set(self._zip.namelist())
        self._shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", self.NS):
                self._shared.append("".join(t.text or "" for t in si.iter(f"{{{self.NS['m']}}}t")))
        book = ET.fromstring(self._zip.read("xl/workbook.xml"))
        rels = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("p:Relationship", self.NS)}
        self._sheets = {}
        for sheet in book.findall("m:sheets/m:sheet", self.NS):
            target = targets.get(sheet.get(f"{{{self.NS['r']}}}id"), "")
            target = target.lstrip("/")
            self._sheets[sheet.get("name")] = target if target.startswith("xl/") else "xl/" + target
        self.sheetnames = list(self._sheets)

    @staticmethod
    def _col(ref):
        n = 0
        for ch in ref:
            if not ch.isalpha():
                break
            n = n * 26 + ord(ch.upper()) - 64
        return n - 1

    def __getitem__(self, name):
        root = self._ET.fromstring(self._zip.read(self._sheets[name]))
        rows, m = [], "{" + self.NS["m"] + "}"
        for row in root.iter(m + "row"):
            idx = int(row.get("r", len(rows) + 1)) - 1
            while len(rows) < idx:
                rows.append(())
            cells = {}
            for c in row.findall(m + "c"):
                kind, v = c.get("t"), c.find(m + "v")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(m + "t"))
                elif v is None or v.text is None:
                    value = None
                elif kind == "s":
                    value = self._shared[int(v.text)]
                elif kind in ("str", "e"):
                    value = v.text
                elif kind == "b":
                    value = v.text == "1"
                else:
                    number = float(v.text)
                    value = int(number) if number.is_integer() and "." not in v.text and "E" not in v.text.upper() else number
                cells[self._col(c.get("r", "A"))] = None if value == "" else value
            width = max(cells) + 1 if cells else 0
            rows.append(tuple(cells.get(i) for i in range(width)))
        return _XlsxSheet(rows)

    def close(self):
        self._zip.close()


def _load_workbook(path: Path):
    try:
        from openpyxl import load_workbook
    except ImportError:
        load_workbook = None
    try:
        if load_workbook is None:
            return _XlsxBook(path)
        return load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise FinanceError(f"Не удалось открыть {path.name}: {type(exc).__name__}") from exc


def parse_summary(ws) -> dict:
    rows = list(ws.iter_rows(values_only=True))
    header_index = next((i for i, r in enumerate(rows) if r and _text(r[0]) == "Статья"), None)
    if header_index is None:
        raise FinanceError("На листе «Свод» не найдена строка заголовка «Статья»")
    header = rows[header_index]
    title = _text(rows[0][0]) if rows and rows[0] else ""
    subtitle = _text(rows[1][0]) if len(rows) > 1 and rows[1] else ""
    period_cols, forecast = [], None
    for col, value in enumerate(header):
        name = _text(value)
        if col < 3 or not name:
            continue
        if name.lower().startswith("прогноз"):
            forecast = {"col": col, "title": name}
        else:
            period_cols.append((col, name))
    periods = [name for _, name in period_cols]

    out, section, parent = [], "", None
    for r in rows[header_index + 1:]:
        if not r or r[0] is None:
            continue
        label = _text(r[0])
        values = [_num(r[c]) if c < len(r) else None for c, _ in period_cols]
        total = _num(r[1]) if len(r) > 1 else None
        pct = _num(r[2]) if len(r) > 2 else None
        fc = _num(r[forecast["col"]]) if forecast and forecast["col"] < len(r) else None
        has_numbers = total is not None or any(v is not None for v in values)
        if not has_numbers:
            if label.isupper() and len(label) > 3:   # заголовок раздела: ВЫРУЧКА, РАСХОДЫ…, ДОЛЯ ОТ ВЫРУЧКИ
                section = label
                out.append({"kind": "section", "label": label})
            continue                                  # пояснения и пустые строки
        is_share = section.startswith("ДОЛЯ") or label.endswith("% от выручки")
        if label in MARKETPLACE_NAMES and parent is not None and not is_share:
            kind = "detail"
        elif is_share:
            kind = "share"
        elif label.startswith(TOTAL_PREFIXES):
            kind = "total"
        else:
            kind = "article"
        if kind == "article":
            parent = label
        out.append({"kind": kind, "label": label, "section": section, "total": total, "pct": pct,
                    "values": values, "forecast": fc, "parent": parent if kind == "detail" else None})
    kpi = {}
    for key, label in KPI_LABELS.items():
        row = next((x for x in out if x["kind"] != "detail" and x.get("label") == label), None)
        if row:
            kpi[key] = {"total": row["total"], "last": row["values"][-1] if row["values"] else None,
                        "forecast": row["forecast"]}
    revenue = (kpi.get("revenue") or {}).get("total")
    profit = (kpi.get("profit") or {}).get("total")
    kpi["margin"] = {"total": profit / revenue if revenue and profit is not None else None}
    return {"title": title, "subtitle": subtitle, "periods": periods,
            "forecast_title": forecast["title"] if forecast else None, "rows": out, "kpi": kpi}


def parse_control(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    out = []
    for r in rows[1:]:
        if not r or not any(v is not None for v in r[:5]):
            continue
        cells = list(r[:5]) + [None] * (5 - len(r[:5]))
        mp, cabinet, metric, value, status = (_text(cells[0]), _text(cells[1]), _text(cells[2]), cells[3], _text(cells[4]))
        if not metric and not mp:
            continue
        out.append({"marketplace": mp, "cabinet": cabinet, "metric": metric,
                    "value": value if isinstance(value, (int, float, str)) or value is None else str(value),
                    "status": status, "problem": status.upper() not in OK_STATUSES})
    return out


def parse_goods(ws, limit: int = 200) -> dict:
    rows = list(ws.iter_rows(values_only=True))
    header_index = next((i for i, r in enumerate(rows[:10]) if r and any(_text(v) == "Артикул" for v in r)), None)
    if header_index is None:
        return {"header": [], "rows": []}
    header = [_text(v) for v in rows[header_index]]
    while header and not header[-1]:
        header.pop()
    width = len(header)
    items = []
    for r in rows[header_index + 1:]:
        if not r or all(v is None for v in r[:width]):
            continue
        items.append([v if isinstance(v, (int, float, str)) or v is None else str(v) for v in r[:width]])
        if len(items) >= limit:
            break
    return {"header": header, "rows": items}


_cache: dict = {}


def read_report(path: Path) -> dict:
    stamp = path.stat().st_mtime
    cached = _cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    wb = _load_workbook(path)
    try:
        if SUMMARY_SHEET not in wb.sheetnames:
            raise FinanceError(f"В файле {path.name} нет листа «{SUMMARY_SHEET}»")
        data = {"summary": parse_summary(wb[SUMMARY_SHEET]),
                "control": parse_control(wb[CONTROL_SHEET]) if CONTROL_SHEET in wb.sheetnames else [],
                "goods": parse_goods(wb[GOODS_SHEET]) if GOODS_SHEET in wb.sheetnames else {"header": [], "rows": []},
                "file": path.name, "updated_at": datetime.fromtimestamp(stamp).isoformat(timespec="minutes")}
    finally:
        wb.close()
    _cache.clear()
    _cache[str(path)] = (stamp, data)
    return data


class ReportRunner:
    """Пересборка ОПиУ в фоне: один запуск за раз, вывод скрипта сохраняется для панели."""

    def __init__(self):
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.log: deque[str] = deque(maxlen=400)
        self.state = {"status": "idle", "mode": None, "started_at": None, "finished_at": None, "code": None}

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "log": list(self.log)[-60:]}

    def start(self, project: Path, python: str, mode: str) -> dict:
        with self.lock:
            if self.state["status"] == "running":
                raise FinanceError("Отчёт уже собирается. Дождитесь окончания.")
            cmd = [python or sys.executable, "opiu_all.py", "--no-unit"]
            if mode == "cache":
                cmd.append("--from-cache")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            try:
                self.process = subprocess.Popen(cmd, cwd=project, env=env, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                                errors="replace")
            except OSError as exc:
                raise FinanceError(f"Не удалось запустить Python ({cmd[0]}): {exc}") from exc
            self.log.clear()
            self.state = {"status": "running", "mode": mode, "started_at": time.time(), "finished_at": None, "code": None}
        threading.Thread(target=self._pump, daemon=True).start()
        return self.status()

    def _pump(self):
        process = self.process
        for line in process.stdout:
            line = line.rstrip()
            if line:
                with self.lock:
                    self.log.append(line)
        code = process.wait()
        with self.lock:
            self.state.update(status="done" if code == 0 else "error", code=code, finished_at=time.time())

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
