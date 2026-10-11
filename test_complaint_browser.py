import unittest
from app.services.complaint_browser import ComplaintBrowser,is_wb_url,safe_url,redact

class BrowserTests(unittest.TestCase):
    def test_only_wb_domains(self):
        self.assertTrue(is_wb_url("https://seller.wildberries.ru/"))
        self.assertTrue(is_wb_url("https://auth.wildberries.ru/login"))
        self.assertFalse(is_wb_url("https://wildberries.ru.evil.test/"))
        self.assertFalse(is_wb_url("https://example.test/"))
        self.assertFalse(is_wb_url("http://seller.wildberries.ru/"))

    def test_no_url_auth_params(self):
        self.assertEqual(safe_url("https://seller.wildberries.ru/path?token=secret#auth"),"https://seller.wildberries.ru/path")

    def test_phone_and_token_redacted(self):
        self.assertNotIn("999",redact("Телефон +7 (999) 123-45-67"))
        self.assertNotIn("A"*50,redact("Bearer "+"A"*50))

    def test_submission_disabled_before_actual_form_verification(self):
        browser=ComplaintBrowser()
        self.assertFalse(browser.snapshot()["automation_ready"])
        self.assertEqual(browser.snapshot()["complaints_submitted_by_browser"],0)
        self.assertFalse(browser.snapshot()["browser_open"])
        browser.executor.shutdown()

if __name__=="__main__":
    unittest.main()
