"""WB merge ownership, validation and one-shot request contract; no real API calls."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.services.nomenclature import Nomenclature, NomenclatureError, CARDS_URL, MOVE_CARDS_URL, pet_kind


MERGE_HEADERS = ["Номер склейки", "nmID", "Артикул", "Название", "Бренд", "Предмет",
                 "subjectID", "Кабинет", "cabinet_id", "imtID", "Кошки/Собаки"]


class WBMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cards.json"
        self.cabinets = [SimpleNamespace(id="a", name="Shop", token="test-token", marketplace="wildberries")]
        self.n = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        self.remote = [dict(nmID=101, imtID=11, subjectID=9, subjectName="Джинсы", vendorCode="A", brand="Brand"),
                       dict(nmID=102, imtID=12, subjectID=9, subjectName="Джинсы", vendorCode="A", brand="Brand")]
        self.moves = []
        self.list_calls = 0
        self.error = None
        self.post_refresh_error = False

        def handler(req):
            self.assertEqual(req.headers["Authorization"], "test-token")
            if str(req.url) == CARDS_URL:
                self.list_calls += 1
                if self.post_refresh_error and self.moves:
                    return httpx.Response(503)
                return httpx.Response(200, json={"cards": self.remote, "cursor": {"total": len(self.remote)}})
            self.assertEqual(str(req.url), MOVE_CARDS_URL)
            body = json.loads(req.content)
            self.moves.append(body)
            operation_key = body.get("targetIMT", "detach:" + str(body["nmIDs"][0]))
            error = self.error.get(operation_key) if isinstance(self.error, dict) else self.error
            if error == "timeout":
                raise httpx.ReadTimeout("timeout", request=req)
            if error:
                return httpx.Response(error, json={"error": True})
            for c in self.remote:
                if c["nmID"] in body["nmIDs"]:
                    c["imtID"] = body.get("targetIMT", 100000 + c["nmID"])
            return httpx.Response(200, json={"error": False})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        self.real_client = real_client
        self.client_patch = patch("app.services.nomenclature.httpx.Client", side_effect=lambda **kw: real_client(transport=transport, **kw))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.sleep_patch = patch("app.services.nomenclature.time.sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.n.refresh_cards()

    def merge(self, ids=None, target=11, cabinet="a"):
        return self.n.merge_cards(cabinet, [101, 102] if ids is None else ids, target)

    def test_refresh_preserves_individual_cards_and_identity(self):
        cards = self.n.cards()["cards"]
        self.assertEqual(len(cards), 2)  # same vendor must not collapse nmIDs
        self.assertEqual([(c["nm"], c["imt_id"], c["cabinet_id"], c["subject_id"]) for c in cards],
                         [(101, 11, "a", 9), (102, 12, "a", 9)])
        self.assertNotIn("test-token", self.path.read_text(encoding="utf-8"))

    def test_get_api_route_returns_cards_over_http(self):
        from app import main

        with patch.object(main, "nomenclature", self.n), patch("app.services.nomenclature.httpx.Client", self.real_client):
            response = TestClient(main.app).get("/api/nomenclature/wb-merges")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual([card["nm"] for card in response.json()["cards"]], [101, 102])

    def test_success_payload_and_refresh_and_replay(self):
        result = self.merge()
        self.assertTrue(result["accepted"])
        self.assertEqual(self.moves, [{"targetIMT": 11, "nmIDs": [102]}])
        self.assertEqual({c["imt_id"] for c in self.n.cards()["cards"]}, {11})
        with self.assertRaisesRegex(NomenclatureError, "уже отправлен"):
            self.merge()
        self.assertEqual(len(self.moves), 1)

    def test_first_selection_is_target(self):
        self.n.merge_cards("a", [102, 101], 12)
        self.assertEqual(self.moves, [{"targetIMT": 12, "nmIDs": [101]}])

    def test_validate_count_types_duplicates_and_target(self):
        for ids in ([], [101], list(range(31)), [101, 101], [True, 102], ["101", 102], [101, -1], None):
            with self.subTest(ids=ids), self.assertRaises(NomenclatureError):
                self.n.merge_cards("a", ids, 11)
        for target in (None, 0, True, "11", 999):
            with self.subTest(target=target), self.assertRaises(NomenclatureError):
                self.merge(target=target)
        self.assertFalse(self.moves)

    def test_rejects_cross_cabinet_and_unknown_account(self):
        data = self.n.cards()
        data["cards"][1]["cabinet_id"] = "b"
        self.n._save_cards(data)
        with self.assertRaisesRegex(NomenclatureError, "одному кабинету"):
            self.merge()
        with self.assertRaisesRegex(NomenclatureError, "Кабинет не найден"):
            self.merge(cabinet="missing")
        self.assertFalse(self.moves)

    def test_fresh_ownership_and_subject_required(self):
        self.remote[1]["subjectID"] = 10
        with self.assertRaisesRegex(NomenclatureError, "одного предмета"):
            self.merge()
        self.remote.pop()
        with self.assertRaisesRegex(NomenclatureError, "не соответствует"):
            self.merge()
        self.assertFalse(self.moves)

    def test_missing_legacy_identity_requires_refresh(self):
        data = self.n.cards()
        data["cards"][0].pop("cabinet_id")
        self.n._save_cards(data)
        with self.assertRaisesRegex(NomenclatureError, "обновите список"):
            self.merge()
        self.assertFalse(self.moves)

    def test_api_errors_are_safe_and_lock_released(self):
        for status in (401, 403, 429, 400, 500):
            self.error = status
            with self.subTest(status=status), self.assertRaises(NomenclatureError) as caught:
                self.merge()
            self.assertNotIn("test-token", str(caught.exception))
            self.assertFalse(self.n._move_lock.locked())
        self.error = None
        self.merge()

    def test_timeout_does_not_allow_double_send(self):
        self.error = "timeout"
        with self.assertRaisesRegex(NomenclatureError, "неизвестен"):
            self.merge()
        with self.assertRaisesRegex(NomenclatureError, "уже отправлен"):
            self.merge()
        self.assertEqual(len(self.moves), 1)

    def test_concurrent_merge_rejected(self):
        self.n._move_lock.acquire()
        try:
            with self.assertRaisesRegex(NomenclatureError, "уже выполняется"):
                self.merge()
        finally:
            self.n._move_lock.release()
        self.assertFalse(self.moves)

    def test_success_with_failed_refresh_is_not_reported_as_failed_move(self):
        self.post_refresh_error = True
        result = self.merge()
        self.assertTrue(result["accepted"])
        self.assertIn("не удалось", result["warning"])

    @staticmethod
    def workbook(rows, headers=None):
        book = Workbook()
        sheet = book.active
        sheet.append(headers or MERGE_HEADERS)
        for row in rows:
            sheet.append(row)
        output = io.BytesIO()
        book.save(output)
        book.close()
        return output.getvalue()

    def preview(self, rows):
        return self.n.preview_merge_excel("groups.xlsx", self.workbook(rows))

    @staticmethod
    def saved_book(book):
        output = io.BytesIO()
        book.save(output)
        book.close()
        return output.getvalue()

    def test_header_can_follow_title_and_blank_rows_on_nonactive_sheet(self):
        book = Workbook()
        book.active.title = "Инструкция"
        book.active.append(["Заполните номера на листе Товары"])
        book.create_sheet("Пустой")
        template = book.create_sheet("Служебный шаблон")
        template.append(MERGE_HEADERS)
        sheet = book.create_sheet("Товары")
        sheet.append(["Склейки на сегодня"])
        sheet.append([])
        sheet.append(MERGE_HEADERS)
        sheet.append(["group", 102])
        sheet.append(["group", 101])
        book.active = 0
        preview = self.n.preview_merge_excel("saved.xlsx", self.saved_book(book))
        self.assertEqual(preview["valid_count"], 1)
        self.assertEqual(preview["groups"][0]["nm_ids"], [102, 101])
        self.assertEqual(preview["groups"][0]["target_nm"], 102)
        self.assertFalse(self.moves)

    def test_header_whitespace_nbsp_linebreaks_case_and_explicit_nmid_aliases(self):
        for header in ("nmID", " NM\u00a0 ID\n", "Nm_Id", "nm-id", "NM ID (WB)",
                       "Артикул WB", "Артикул Wildberries (nmID)"):
            with self.subTest(header=header):
                preview = self.n.preview_merge_excel("alias.xlsx", self.workbook(
                    [["7", 101], ["7", "102"]], ["  НоМеР\u00a0 \n СкЛеЙкИ  ", header]))
                self.assertEqual(preview["valid_count"], 1)
                self.assertEqual(preview["groups"][0]["nm_ids"], [101, 102])
        # Generic article/id columns must not silently become WB identifiers.
        for header in ("ID", "Артикул", "vendorCode"):
            with self.subTest(header=header), self.assertRaisesRegex(NomenclatureError, "Не найдена"):
                self.n.preview_merge_excel("bad.xlsx", self.workbook([["7", 101], ["7", 102]],
                                                                         ["Номер склейки", header]))

    def test_duplicate_headers_allow_fully_empty_or_identical_columns(self):
        rows_cases = [
            [["7", 101, None, None], ["7", 102, None, None]],
            [[None, None, "7", 101], [None, None, "7", 102]],
            [["7", 101, " 7 ", "101"], ["7", 102, "7", 102.0]],
            [["7", 101, "7", None], ["7", 102, "7", None]],
        ]
        for rows in rows_cases:
            with self.subTest(rows=rows):
                preview = self.n.preview_merge_excel("duplicates.xlsx", self.workbook(
                    rows, ["Номер склейки", "nmID", "Номер\nсклейки", "nm ID"]))
                self.assertEqual(preview["valid_count"], 1)
                self.assertEqual(preview["groups"][0]["nm_ids"], [101, 102])
                self.assertEqual(preview["groups"][0]["number"], "7")
        self.assertFalse(self.moves)

    def test_conflicting_duplicates_report_sheet_columns_and_first_conflicting_row(self):
        cases = [
            (["Номер склейки", "nmID", "NM ID"], [["7", 101, 101], ["7", 102, 999]], "nmID", "B и C", "3"),
            (["Номер склейки", "nmID", "Номер склейки"], [["7", 101, "8"], ["7", 102, "7"]], "Номер склейки", "A и C", "2"),
            # Complementary values are not a safe duplicate: never guess how to combine them.
            (["Номер склейки", "nmID", "nmID"], [["7", 101, None], ["7", None, 102]], "nmID", "B и C", "2"),
        ]
        for headers, rows, field, columns, line in cases:
            with self.subTest(field=field, rows=rows):
                book = Workbook()
                book.active.title = "Мои товары"
                book.active.append(headers)
                for row in rows:
                    book.active.append(row)
                with self.assertRaises(NomenclatureError) as caught:
                    self.n.preview_merge_excel("conflict.xlsx", self.saved_book(book))
                message = str(caught.exception)
                for expected in ("Мои товары", field, columns, "строка " + line):
                    self.assertIn(expected, message)
        self.assertFalse(self.moves)

    def test_identical_duplicate_invalid_nmid_still_fails_validation(self):
        for invalid in (True, "=100+1", -1, 101.5):
            with self.subTest(invalid=invalid):
                preview = self.n.preview_merge_excel("invalid.xlsx", self.workbook(
                    [["7", invalid, invalid], ["7", 102, 102]], ["Номер склейки", "nmID", "nm ID"]))
                self.assertEqual(preview["valid_count"], 0)
                self.assertEqual(preview["invalid_count"], 1)
        self.assertFalse(self.moves)

    def test_multiple_conflicting_data_tables_are_ambiguous(self):
        book = Workbook()
        book.active.title = "Первый"
        for sheet in (book.active, book.create_sheet("Второй")):
            sheet.append(["Номер склейки", "nmID"])
            sheet.append(["one" if sheet.title == "Первый" else "two", 101])
            sheet.append(["one" if sheet.title == "Первый" else "two", 102])
        with self.assertRaisesRegex(NomenclatureError, "несколько разных таблиц.*Первый.*Второй"):
            self.n.preview_merge_excel("ambiguous.xlsx", self.saved_book(book))

    def test_edited_saved_export_on_another_sheet_imports_and_http_batch_applies_exact_membership(self):
        from app import main

        self.existing_group(3)
        book = load_workbook(io.BytesIO(self.n.export_merge_excel()))
        sheet = book.active
        sheet.insert_rows(1, 2)
        sheet.cell(1, 1, "Заполненный экспорт")
        sheet.cell(3, 1, "  НОМЕР\u00a0\nСКЛЕЙКИ ")
        sheet.cell(3, 2, "NM ID (WB)")
        sheet.cell(4, 1, "номер 009")
        sheet.cell(5, 1, "номер 009")
        # A user may leave a copied empty nmID column beside the edited table.
        sheet.cell(3, 12, "nmID")
        service = book.create_sheet("Инструкция", 0)
        service.append(["Импортируйте таблицу с другого листа"])
        book.active = 0
        content = self.saved_book(book)
        with patch.object(main, "nomenclature", self.n), patch.object(main.draft_store, "audit"), \
                patch.object(main, "_opiu_wb_tokens", return_value=[]):
            client = TestClient(main.app)
            preview = client.post("/api/nomenclature/wb-merges/preview", files={
                "file": ("edited-export.xlsx", content)})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["valid_count"], 1)
            self.assertEqual(preview.json()["groups"][0]["desired_members"], [101, 102])
            self.assertEqual(preview.json()["groups"][0]["exits"], [103])
            result = client.post("/api/nomenclature/wb-merges/batch", json={
                "confirmed": True, "preview_id": preview.json()["preview_id"]})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["summary"]["success"], 1)
            self.assertEqual(self.moves, [{"nmIDs": [103]}])
            cards = {c["nm"]: c for c in client.get("/api/nomenclature/wb-merges").json()["cards"]}
            self.assertEqual(cards[101]["merge_label"], "номер 009")
            self.assertEqual(cards[102]["merge_label"], "номер 009")
            self.assertTrue(cards[103]["is_single"])

    def second_pair(self):
        self.remote.extend([
            dict(nmID=103, imtID=13, subjectID=9, subjectName="Джинсы", vendorCode="B", brand="Brand"),
            dict(nmID=104, imtID=14, subjectID=9, subjectName="Джинсы", vendorCode="B", brand="Brand")])
        self.n.refresh_cards()

    def test_excel_export_preserves_every_card_and_exact_columns(self):
        data = self.n.cards()
        data["cards"][0]["title"] = "=HYPERLINK(\"https://example.invalid\")"
        self.n._save_cards(data)
        book = load_workbook(io.BytesIO(self.n.export_merge_excel()), read_only=True, data_only=False)
        self.addCleanup(book.close)
        sheet = book.active
        cells = list(sheet.iter_rows())
        rows = [[cell.value for cell in row] for row in cells]
        self.assertEqual(rows[0], MERGE_HEADERS)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[1] for r in rows[1:]], [101, 102])
        self.assertEqual([r[2] for r in rows[1:]], ["A", "A"])
        self.assertTrue(all(r[0] in (None, "") for r in rows[1:]))
        self.assertEqual(rows[1][6:11], [9, "Shop", "a", 11, "Не определено"])
        self.assertNotEqual(cells[1][3].data_type, "f")

    def test_excel_preview_first_row_is_target_and_ignores_untrusted_metadata(self):
        preview = self.preview([
            ["любое значение", 102, "FAKE", "FAKE", "FAKE", "FAKE", 999, "FAKE", "evil", 999],
            ["любое значение", 101, "FAKE", "FAKE", "FAKE", "FAKE", 888, "FAKE", "evil", 999]])
        self.assertEqual((preview["valid_count"], preview["invalid_count"]), (1, 0))
        group = preview["groups"][0]
        self.assertTrue(group["valid"])
        self.assertEqual(group["nm_ids"], [102, 101])
        self.assertEqual((group["target_nm"], group["target_imt"]), (102, 12))
        self.assertEqual((group["cabinet_id"], group["subject_id"]), ("a", 9))
        self.assertTrue(preview["preview_id"])
        self.assertFalse(self.moves)

    def test_excel_preview_uses_cache_to_reject_forged_same_cabinet(self):
        data = self.n.cards()
        data["cards"][1]["cabinet_id"] = "b"
        self.n._save_cards(data)
        preview = self.preview([[1, 101, "", "", "", "", 9, "Shop", "a", 11],
                                [1, 102, "", "", "", "", 9, "Shop", "a", 11]])
        self.assertEqual((preview["valid_count"], preview["invalid_count"]), (0, 1))
        self.assertFalse(preview["groups"][0]["valid"])
        self.assertFalse(self.moves)

    def test_excel_preview_invalid_counts_unknown_ids_and_duplicate_members(self):
        cases = (
            [[1, 101]],
            [[1, 101], [1, 101]],
            [[1, 101], [1, 999]],
            [[1, 101], [1, "=101+1"]],
            [[1, 101], [1, True]],
            [[1, nm] for nm in range(101, 132)],
            [["first", 101], ["first", 102], ["second", 101], ["second", 102]],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                preview = self.preview(rows)
                self.assertEqual(preview["valid_count"], 0)
                self.assertGreater(preview["invalid_count"], 0)
                self.assertTrue(all(not g["valid"] and g["error"] for g in preview["groups"]))
        self.assertFalse(self.moves)

    def test_excel_preview_subject_and_target_are_required_in_cache(self):
        for field, value in (("subject_id", 10), ("imt_id", None)):
            with self.subTest(field=field):
                data = self.n.cards()
                data["cards"][0][field] = value
                self.n._save_cards(data)
                preview = self.preview([[1, 101], [1, 102]])
                self.assertEqual(preview["valid_count"], 0)
                self.n.refresh_cards()

    def test_excel_preview_zero_label_and_blank_ungrouped_rows(self):
        preview = self.preview([[None, 999], [0, 101], [0, 102], [None, None]])
        self.assertEqual((preview["valid_count"], preview["invalid_count"]), (1, 0))
        self.assertEqual(preview["groups"][0]["nm_ids"], [101, 102])

    def test_excel_preview_rejects_bad_file_and_missing_required_header(self):
        for filename, content in (("groups.csv", b"1,101\n1,102"), ("groups.xlsx", b"not a zip"),
                                  ("groups.xlsx", self.workbook([[1, 101]], ["group", "id"]))):
            with self.subTest(filename=filename), self.assertRaises(NomenclatureError):
                self.n.preview_merge_excel(filename, content)
        self.assertFalse(self.moves)

    def test_excel_export_and_preview_http_routes_are_read_only(self):
        from app import main

        with patch.object(main, "nomenclature", self.n), patch.object(main.draft_store, "audit"), \
                patch("app.services.nomenclature.httpx.Client", self.real_client):
            client = TestClient(main.app)
            export = client.get("/api/nomenclature/wb-merges/export")
            preview = client.post("/api/nomenclature/wb-merges/preview", files={
                "file": ("groups.xlsx", self.workbook([[1, 101], [1, 102]]),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml.sheet", export.headers["content-type"])
        self.assertTrue(export.content.startswith(b"PK"))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["valid_count"], 1)
        self.assertFalse(self.moves)

    def test_batch_happy_path_fetches_once_before_and_after_and_posts_groups_in_order(self):
        self.second_pair()
        preview = self.preview([["first", 102], ["first", 101], ["second", 103], ["second", 104]])
        before = self.list_calls
        audit = Mock()
        result = self.n.merge_batch(preview["preview_id"], audit=audit)
        self.assertEqual(self.list_calls - before, 2)  # one shared preflight, one post-move refresh
        self.assertEqual(self.moves, [{"targetIMT": 12, "nmIDs": [101]},
                                      {"targetIMT": 13, "nmIDs": [104]}])
        self.assertEqual([g["status"] for g in result["groups"]], ["success", "success"])
        self.assertEqual(result["summary"], {"success": 2, "error": 0, "unknown": 0, "total": 2})
        self.assertGreaterEqual(audit.call_count, 2)
        with self.assertRaises(NomenclatureError):
            self.n.merge_batch(preview["preview_id"])
        self.assertEqual(len(self.moves), 2)

    def test_preview_can_be_confirmed_by_another_worker_once(self):
        preview = self.preview([[1, 101], [1, 102]])
        other = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        result = other.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"], {"success": 1, "error": 0, "unknown": 0, "total": 1})
        self.assertEqual(self.moves, [{"targetIMT": 11, "nmIDs": [102]}])
        with self.assertRaises(NomenclatureError):
            self.n.merge_batch(preview["preview_id"])
        self.assertEqual(len(self.moves), 1)

    def test_process_lock_blocks_other_worker_single_and_batch(self):
        preview = self.preview([[1, 101], [1, 102]])
        other = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        lock = self.n._acquire_move_file_lock()
        before = self.list_calls
        try:
            with self.assertRaisesRegex(NomenclatureError, "уже выполняется"):
                other.merge_cards("a", [101, 102], 11)
            with self.assertRaisesRegex(NomenclatureError, "уже выполняется"):
                other.merge_batch(preview["preview_id"])
            self.assertEqual(self.list_calls, before)
            self.assertFalse(self.moves)
            self.assertFalse(other._move_lock.locked())
        finally:
            lock.close()
        result = other.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["success"], 1)

    def test_audit_failure_after_success_preserves_accepted_result(self):
        preview = self.preview([[1, 101], [1, 102]])

        def failing_audit(event, cabinet_id, details):
            if event == "wb_cards_merge_success":
                raise OSError("audit unavailable")

        result = self.n.merge_batch(preview["preview_id"], audit=failing_audit)
        self.assertEqual(result["summary"], {"success": 1, "error": 0, "unknown": 0, "total": 1})
        self.assertEqual(result["groups"][0]["status"], "success")
        self.assertIn("аудит", result["groups"][0]["warning"])
        self.assertEqual(self.moves, [{"targetIMT": 11, "nmIDs": [102]}])
        self.assertFalse(self.n._move_lock.locked())

    def test_batch_partial_error_keeps_success_and_audits_every_group(self):
        self.second_pair()
        preview = self.preview([["bad", 101], ["bad", 102], ["good", 103], ["good", 104], ["invalid", 999]])
        self.error = {11: 400}
        audit = Mock()
        before = self.list_calls
        result = self.n.merge_batch(preview["preview_id"], audit=audit)
        self.assertEqual(self.list_calls - before, 2)
        self.assertEqual([g["status"] for g in result["groups"]], ["error", "success", "error"])
        self.assertEqual(result["summary"], {"success": 1, "error": 2, "unknown": 0, "total": 3})
        self.assertEqual(len(self.moves), 2)
        self.assertGreaterEqual(audit.call_count, 3)
        self.assertNotIn("test-token", json.dumps(result, ensure_ascii=False))

    def test_batch_timeout_unknown_does_not_resend_after_new_preview_or_restart(self):
        self.second_pair()
        rows = [["timeout", 101], ["timeout", 102], ["good", 103], ["good", 104]]
        preview = self.preview(rows)
        self.error = {11: "timeout"}
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual([g["status"] for g in result["groups"]], ["unknown", "success"])
        self.assertEqual(result["summary"], {"success": 1, "error": 0, "unknown": 1, "total": 2})
        self.assertFalse(self.n._move_lock.locked())
        self.error = None
        self.n = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        again = self.preview(rows[:2])
        result = self.n.merge_batch(again["preview_id"])
        self.assertNotEqual(result["groups"][0]["status"], "success")
        self.assertEqual(len(self.moves), 2)

    def test_batch_timeout_cannot_be_replayed_through_single_merge(self):
        preview = self.preview([[1, 101], [1, 102]])
        self.error = "timeout"
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual(result["groups"][0]["status"], "unknown")
        self.error = None
        with self.assertRaises(NomenclatureError):
            self.merge()
        self.assertEqual(len(self.moves), 1)

    def test_single_timeout_cannot_be_replayed_through_batch(self):
        self.error = "timeout"
        with self.assertRaises(NomenclatureError):
            self.merge()
        self.error = None
        preview = self.preview([[1, 101], [1, 102]])
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual(result["groups"][0]["status"], "unknown")
        self.assertEqual(len(self.moves), 1)

    def test_batch_rechecks_fresh_ownership_subject_and_target_before_post(self):
        for mutation in ("subject", "target", "ownership"):
            with self.subTest(mutation=mutation):
                original = json.loads(json.dumps(self.remote))
                preview = self.preview([[1, 101], [1, 102]])
                if mutation == "subject":
                    self.remote[1]["subjectID"] = 10
                elif mutation == "target":
                    self.remote[0]["imtID"] = 999
                else:
                    self.remote.pop()
                result = self.n.merge_batch(preview["preview_id"])
                self.assertEqual(result["summary"]["error"], 1)
                self.assertFalse(self.moves)
                self.remote[:] = original

    def test_batch_revalidates_changed_cache_and_requires_confirmation(self):
        from app import main

        preview = self.preview([[1, 101], [1, 102]])
        data = self.n.cards()
        data["cards"][0]["subject_id"] = 100
        self.n._save_cards(data)
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["error"], 1)
        with patch.object(main, "nomenclature", self.n), patch.object(main.draft_store, "audit"), \
                patch("app.services.nomenclature.httpx.Client", self.real_client):
            response = TestClient(main.app).post("/api/nomenclature/wb-merges/batch", json={
                "preview_id": "anything", "confirmed": False})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.moves)

    def existing_group(self, count=17):
        self.remote[:] = [dict(nmID=nm, imtID=11, subjectID=9, subjectName="Джинсы",
                               vendorCode=str(nm), brand="Brand") for nm in range(101, 101 + count)]
        self.n.refresh_cards()

    def test_exact_eleven_of_seventeen_detaches_six_separately_without_join(self):
        self.existing_group()
        preview = self.preview([["0042", nm] for nm in range(101, 112)])
        group = preview["groups"][0]
        self.assertEqual(group["desired_members"], list(range(101, 112)))
        self.assertEqual(group["exits"], list(range(112, 118)))
        self.assertFalse(group["no_api"])
        self.assertFalse(group["join_needed"])
        audit = Mock()
        result = self.n.merge_batch(preview["preview_id"], audit=audit)
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(self.moves, [{"nmIDs": [nm]} for nm in range(112, 118)])
        self.assertEqual(len({c["imtID"] for c in self.remote if c["nmID"] >= 112}), 6)
        self.assertEqual(len(result["groups"][0]["operations"]), 6)
        self.assertEqual(sum(call.args[0] == "wb_cards_detach_success" for call in audit.call_args_list), 6)
        by_nm = {c["nm"]: c for c in self.n.cards()["cards"]}
        self.assertEqual(by_nm[101]["merge_label"], "0042")
        self.assertEqual(by_nm[101]["merge_size"], 11)
        self.assertFalse(by_nm[101]["is_single"])
        self.assertEqual((by_nm[112]["merge_label"], by_nm[112]["merge_size"], by_nm[112]["is_single"]), ("", 1, True))

    def test_exact_exits_are_completed_before_join(self):
        self.existing_group()
        self.remote[10]["imtID"] = 55  # desired member is outside the target group
        self.n.refresh_cards()
        preview = self.preview([["group", nm] for nm in range(101, 112)])
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(self.moves[:6], [{"nmIDs": [nm]} for nm in range(112, 118)])
        self.assertEqual(self.moves[6], {"targetIMT": 11, "nmIDs": list(range(102, 112))})
        self.assertEqual(sum(c["imtID"] == 11 for c in self.remote), 11)

    def test_blank_unrelated_groups_are_untouched_and_unlabelled_status_remains_visible(self):
        self.second_pair()
        self.remote[2]["imtID"] = self.remote[3]["imtID"] = 33
        self.n.refresh_cards()
        preview = self.preview([["wanted", 101], ["wanted", 102], [None, 103], [None, 104]])
        self.assertEqual(len(preview["groups"]), 1)
        self.n.merge_batch(preview["preview_id"])
        self.assertEqual(self.moves, [{"targetIMT": 11, "nmIDs": [102]}])
        other = [c for c in self.n.cards()["cards"] if c["nm"] in (103, 104)]
        self.assertTrue(all(c["imt_id"] == 33 and c["merge_size"] == 2 and c["is_single"] is False
                            and c["merge_label"] == "" for c in other))

    def test_clearing_persisted_label_detaches_to_single_and_clears_label(self):
        initial = self.preview([["custom", 101], ["custom", 102]])
        self.n.merge_batch(initial["preview_id"])
        other = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        preview = other.preview_merge_excel("clear.xlsx", self.workbook([[None, 102]]))
        self.assertEqual(preview["groups"][0]["kind"], "single")
        self.assertEqual(preview["groups"][0]["exits"], [102])
        result = other.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(self.moves[-1], {"nmIDs": [102]})
        card = next(c for c in self.n.cards()["cards"] if c["nm"] == 102)
        self.assertEqual((card["merge_label"], card["merge_size"], card["is_single"]), ("", 1, True))
        self.assertEqual(self.n._merge_labels()["a"], {"101": "custom"})

    def test_no_api_group_saves_exact_user_number_across_workers_restart_and_export(self):
        self.remote[1]["imtID"] = 11
        self.n.refresh_cards()
        preview = self.preview([["0007/А", 101], ["0007/А", 102]])
        self.assertTrue(preview["groups"][0]["no_api"])
        self.assertEqual(preview["valid_count"], 1)
        before = self.list_calls
        worker = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        result = worker.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(self.list_calls - before, 1)
        self.assertEqual(self.moves, [])
        restarted = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        self.assertEqual([c["merge_label"] for c in restarted.cards()["cards"]], ["0007/А", "0007/А"])
        wb = load_workbook(io.BytesIO(restarted.export_merge_excel()), read_only=True)
        self.addCleanup(wb.close)
        rows = list(wb.active.iter_rows())
        self.assertEqual([row[0].value for row in rows[1:]], ["0007/А", "0007/А"])
        self.assertTrue(all(row[i].number_format == "0" for row in rows[1:] for i in (1, 9)))
        self.assertNotIn("test-token", restarted.cards_path.with_name("wb_merge_labels.json").read_text(encoding="utf-8"))

    def test_clearing_label_from_already_single_needs_no_api(self):
        lock = self.n._acquire_move_file_lock()
        try:
            self.n._update_merge_labels("a", [101], "old")
        finally:
            lock.close()
        preview = self.preview([[None, 101]])
        self.assertTrue(preview["groups"][0]["no_api"])
        self.n.merge_batch(preview["preview_id"])
        self.assertFalse(self.moves)
        self.assertEqual(self.n.cards()["cards"][0]["merge_label"], "")

    def test_different_workers_preserve_each_others_saved_labels(self):
        self.second_pair()
        self.remote[1]["imtID"] = 11
        self.remote[3]["imtID"] = 13
        self.n.refresh_cards()
        other = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        other.cards()  # Reading before another worker writes must not cache the mapping.
        self.n.merge_batch(self.preview([["first", 101], ["first", 102]])["preview_id"])
        preview = other.preview_merge_excel("second.xlsx", self.workbook([["second", 103], ["second", 104]]))
        other.merge_batch(preview["preview_id"])
        self.assertEqual(self.n._merge_labels()["a"], {"101": "first", "102": "first", "103": "second", "104": "second"})
        self.assertFalse(self.moves)

    def test_unknown_join_cannot_be_reported_as_no_api_success(self):
        self.error = "timeout"
        result = self.n.merge_batch(self.preview([["first", 101], ["first", 102]])["preview_id"])
        self.assertEqual(result["summary"]["unknown"], 1)
        self.remote[1]["imtID"] = 11  # WB may have applied the timed-out operation.
        self.error = None
        self.n.refresh_cards()
        preview = self.preview([["second", 101], ["second", 102]])
        self.assertTrue(preview["groups"][0]["no_api"])
        result = self.n.merge_batch(preview["preview_id"])
        self.assertEqual(result["summary"]["unknown"], 1)
        self.assertFalse(self.n._merge_labels())
        self.assertEqual(len(self.moves), 1)

    def test_detach_with_unpersisted_outcome_blocks_dependent_join(self):
        self.existing_group(3)
        self.remote[1]["imtID"] = 22
        self.n.refresh_cards()
        save = self.n._save_move_ledger

        def fail_completion(ledger):
            if any(entry["status"] == "success" for entry in ledger.values()):
                raise NomenclatureError("disk error")
            save(ledger)

        with patch.object(self.n, "_save_move_ledger", side_effect=fail_completion):
            result = self.n.merge_batch(self.preview([["new", 101], ["new", 102]])["preview_id"])
        self.assertEqual(result["summary"]["unknown"], 1)
        self.assertEqual(self.moves, [{"nmIDs": [103]}])
        self.assertEqual(next(iter(self.n._move_ledger().values()))["status"], "unknown")
        self.assertFalse(self.n._merge_labels())

    def test_detach_error_and_timeout_keep_partial_result_and_prevent_join(self):
        for error, status in ((400, "error"), ("timeout", "unknown")):
            with self.subTest(error=error):
                self.existing_group(4)
                self.remote[1]["imtID"] = 22
                self.n.refresh_cards()
                self.moves.clear()
                self.error = {"detach:104": error}
                preview = self.preview([["new", 101], ["new", 102]])
                result = self.n.merge_batch(preview["preview_id"])
                group = result["groups"][0]
                self.assertEqual(group["status"], status)
                self.assertEqual([op["status"] for op in group["operations"]], ["success", status])
                self.assertEqual(self.moves, [{"nmIDs": [103]}, {"nmIDs": [104]}])
                self.assertFalse(self.n._merge_labels()["a"])
                # Independent runs need a new ledger (a success is intentionally not replayable).
                self.path.with_name("wb_merge_requests.json").unlink()

    def test_detach_unknown_blocks_new_preview_and_overlapping_direct_join_after_restart(self):
        self.existing_group(4)
        self.remote[1]["imtID"] = 22
        self.n.refresh_cards()
        self.error = {"detach:103": "timeout"}
        result = self.n.merge_batch(self.preview([["new", 101], ["new", 102]])["preview_id"])
        self.assertEqual(result["summary"]["unknown"], 1)
        self.error = None
        worker = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
        retry = worker.preview_merge_excel("again.xlsx", self.workbook([["renamed", 101], ["renamed", 102]]))
        self.assertEqual(worker.merge_batch(retry["preview_id"])["summary"]["unknown"], 1)
        with self.assertRaisesRegex(NomenclatureError, "неизвестен"):
            worker.merge_cards("a", [103, 102], 11)
        self.assertEqual(self.moves, [{"nmIDs": [103]}])
        ledger = worker._move_ledger()
        self.assertEqual(next(iter(ledger.values()))["operation"], "detach")

    def test_detach_extras_and_non_target_identity_are_revalidated(self):
        self.existing_group(3)
        for nm, field, value in ((103, "subjectID", 10), (103, "imtID", 55), (102, "imtID", 44)):
            with self.subTest(nm=nm, field=field):
                preview = self.preview([["new", 101], ["new", 102]])
                card = next(c for c in self.remote if c["nmID"] == nm)
                old = card[field]
                card[field] = value
                self.assertEqual(self.n.merge_batch(preview["preview_id"])["summary"]["error"], 1)
                self.assertFalse(self.moves)
                card[field] = old

    def test_post_move_refresh_failure_does_not_publish_stale_group(self):
        self.existing_group(3)
        self.post_refresh_error = True
        result = self.n.merge_batch(self.preview([["new", 101], ["new", 102]])["preview_id"])
        self.assertEqual(result["summary"]["success"], 1)
        self.assertIn("не удалось", result["groups"][0]["warning"])
        card = next(c for c in self.n.cards()["cards"] if c["nm"] == 103)
        self.assertIsNone(card["imt_id"])
        self.assertIsNone(card["is_single"])

    def test_group_target_that_needs_detach_is_rejected_before_sending(self):
        self.existing_group(4)
        preview = self.preview([["one", 101], ["one", 102], ["two", 103], ["two", 104]])
        invalid = [g for g in preview["groups"] if not g["valid"]]
        self.assertTrue(invalid)
        self.assertIn("сначала отделите", invalid[0]["error"])
        self.assertFalse(self.moves)

    def test_pet_kind_uses_title_and_subject_with_word_boundaries_and_case(self):
        cases = [
            ("Корм для КОШЕК", "Корм", "Кошки"),
            ("Корм для котят", "Корм", "Кошки"),
            ("Кошачий туалет", "Туалеты", "Кошки"),
            ("Корм", "Для взрослых собак", "Собаки"),
            ("Щенячий корм", "Товары для щенков", "Собаки"),
            ("Лакомство для кошек и собак", "Корм", "Кошки и собаки"),
            ("Корм для кота", "Для собак", "Кошки и собаки"),
            ("CAT & DOG", "Pet food", "Кошки и собаки"),
            ("Kitten / puppies", "", "Кошки и собаки"),
            ("Котлеты, скотч, кошелёк, догма", "Прочее", "Не определено"),
            (None, None, "Не определено"),
        ]
        for title, subject, expected in cases:
            with self.subTest(title=title, subject=subject):
                self.assertEqual(pet_kind(title, subject), expected)

    def test_pet_kind_api_and_export_are_derived_from_cached_title_and_subject(self):
        data = self.n.cards()
        data["cards"][0].update(title="Для кошек", subject="Лакомство для собак", pet_kind="Собаки")
        data["cards"][1].update(title="Корм для кошек", subject="Корм", pet_kind="Собаки")
        self.n._save_cards(data)
        cards = self.n.cards()["cards"]
        self.assertEqual([c["pet_kind"] for c in cards], ["Кошки и собаки", "Кошки"])
        book = load_workbook(io.BytesIO(self.n.export_merge_excel()), read_only=True)
        self.addCleanup(book.close)
        rows = list(book.active.values)
        self.assertEqual(rows[0][-1], "Кошки/Собаки")
        self.assertEqual([row[-1] for row in rows[1:]], ["Кошки и собаки", "Кошки"])

    def test_http_exact_eleven_of_seventeen_uses_actual_baseline_and_persists_label(self):
        from app import main

        self.existing_group()
        for card in self.remote:
            card["imtID"] = 736918694
            card["title"] = "Корм для кошек"
        self.n.refresh_cards()
        with patch.object(main, "nomenclature", self.n), patch.object(main.draft_store, "audit"), \
                patch.object(main, "_opiu_wb_tokens", return_value=[]):
            client = TestClient(main.app)
            preview = client.post("/api/nomenclature/wb-merges/preview", files={
                "file": ("exact.xlsx", self.workbook([[1, nm] for nm in range(101, 112)] +
                                                    [[None, nm] for nm in range(112, 118)]))})
            self.assertEqual(preview.status_code, 200)
            group = preview.json()["groups"][0]
            self.assertEqual(group["desired_members"], list(range(101, 112)))
            self.assertEqual(group["exits"], list(range(112, 118)))
            self.assertEqual(len(group["baseline"]["actual_members"]), 17)
            # Presentation rows may include extras or be reordered without changing WB.
            # Validation must compare the primitive actual snapshot, not the UI lists.
            path = self.path.parent / "wb_merge_previews" / (preview.json()["preview_id"] + ".json")
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["groups"][0]["cards"].extend(stored["groups"][0]["exit_cards"])
            path.write_text(json.dumps(stored), encoding="utf-8")
            data = self.n.cards()
            data["cards"].reverse()
            data["cards"][0]["title"] = "Обновлённое название"
            self.n._save_cards(data)
            self.remote.reverse()
            result = client.post("/api/nomenclature/wb-merges/batch", json={
                "confirmed": True, "preview_id": preview.json()["preview_id"]})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["summary"], {"success": 1, "error": 0, "unknown": 0, "total": 1})
            self.assertEqual(self.moves, [{"nmIDs": [nm]} for nm in range(112, 118)])
            cards = client.get("/api/nomenclature/wb-merges").json()["cards"]
            desired = [c for c in cards if c["nm"] < 112]
            self.assertTrue(all(c["merge_label"] == "1" and c["merge_size"] == 11 for c in desired))
            restarted = Nomenclature(lambda: Path(self.temp.name), cabinets=lambda: self.cabinets, cards_path=self.path)
            with patch.object(main, "nomenclature", restarted):
                reloaded = client.get("/api/nomenclature/wb-merges").json()["cards"]
                self.assertTrue(all(c["merge_label"] == "1" for c in reloaded if c["nm"] < 112))

    def test_actual_baseline_rejects_changed_target_membership_and_single_peers(self):
        self.existing_group(4)
        preview = self.preview([["new", 101], ["new", 102]])
        self.remote.append(dict(nmID=105, imtID=11, subjectID=9))
        self.assertEqual(self.n.merge_batch(preview["preview_id"])["summary"]["error"], 1)
        self.assertFalse(self.moves)
        self.remote.pop()
        lock = self.n._acquire_move_file_lock()
        try:
            self.n._update_merge_labels("a", [104], "clear")
        finally:
            lock.close()
        preview = self.preview([[None, 104]])
        self.remote[1]["imtID"] = 22
        self.assertEqual(self.n.merge_batch(preview["preview_id"])["summary"]["error"], 1)
        self.assertFalse(self.moves)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the browser-state regression")
    def test_ui_shows_saved_label_immediately_and_filters_pet_kind_and_number_after_reload(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('app/static/products.js', 'utf8');
const initial = [
  {nm:101,cabinet_id:'a',cabinet:'Shop',subject_id:9,subject:'Корм',title:'Для кошек',pet_kind:'Кошки',imt_id:11,merge_size:1,is_single:true,merge_label:''},
  {nm:102,cabinet_id:'a',cabinet:'Shop',subject_id:9,subject:'Корм',title:'Для кошек',pet_kind:'Кошки',imt_id:12,merge_size:1,is_single:true,merge_label:''},
  {nm:103,cabinet_id:'a',cabinet:'Shop',subject_id:9,subject:'Корм',title:'Для собак',pet_kind:'Собаки',imt_id:13,merge_size:1,is_single:true,merge_label:'other'}
];
function element() { return {style:{},innerHTML:'',textContent:'',classList:{toggle(){}},setAttribute(){},querySelectorAll(){return[];},querySelector(){if(!this.child)this.child={};return this.child;}}; }
function rootNode() {
  const nodes = new Map();
  return {...element(),querySelector(s){if(!nodes.has(s))nodes.set(s,element());return nodes.get(s);}};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
let persisted = JSON.parse(JSON.stringify(initial)), applied = false, failList = false;
async function fetchMock(url, options) {
  assert(options);
  if(url.endsWith('/preview')) return {ok:true,status:200,json:async()=>({preview_id:'preview',valid_count:1,invalid_count:0,groups:[{number:'0042',valid:true,nm_ids:[101,102],desired_members:[101,102],exits:[]}]})};
  if(url.endsWith('/batch')) {
    applied = true; failList = true;
    persisted = persisted.map(c => c.nm === 103 ? c : {...c,merge_label:'0042',imt_id:11,merge_size:2,is_single:false});
    return {ok:true,status:200,json:async()=>({summary:{success:1,error:0,unknown:0,total:1},groups:[{number:'0042',cabinet_id:'a',nm_ids:[101,102],status:'success',operations:[]}]})};
  }
  assert.strictEqual(options.cache,'no-store');
  if(failList) throw new Error('list unavailable');
  return {ok:true,status:200,json:async()=>({cards:JSON.parse(JSON.stringify(persisted))})};
}
function boot() {
  const context = {window:{},localStorage:{getItem(){return null;}},document:{createElement:element,head:{append(){},appendChild(){}}},fetch:fetchMock,FormData:class {append(){}},setTimeout,clearTimeout};
  vm.createContext(context); vm.runInContext(source,context);
  const root = rootNode(); context.window.WBOPSMerges.render(root); return root;
}
function filter(root,name,value) {
  root.onchange({target:{name,value,dataset:{},matches(){return false;},closest(){return null;}}});
}
(async()=>{
  let root = boot(); await tick();
  root.onchange({target:{matches(){return true;},files:[{name:'groups.xlsx'}]}}); await tick();
  await root.querySelector('[data-merge-preview]').querySelector('[data-merge-batch]').onclick();
  assert(applied);
  assert(root.querySelector('tbody').innerHTML.includes('<td>0042</td>'), 'successful labels must show despite failed list reload');
  assert(root.querySelector('[name=merge_label]').innerHTML.includes('0042'));
  failList=false; root=boot(); await tick();
  filter(root,'pet_kind','Собаки');
  assert(root.querySelector('tbody').innerHTML.includes('<td>103</td>'));
  assert(!root.querySelector('tbody').innerHTML.includes('<td>101</td>'));
  filter(root,'pet_kind',''); filter(root,'merge_label','0042');
  assert(root.querySelector('tbody').innerHTML.includes('<td>101</td>'));
  assert(root.querySelector('tbody').innerHTML.includes('<td>102</td>'));
  assert(!root.querySelector('tbody').innerHTML.includes('<td>103</td>'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-"], input=script, text=True,
                                capture_output=True, cwd=Path(__file__).parent, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
