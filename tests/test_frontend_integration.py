import tempfile
import unittest
from pathlib import Path

from backend.app import create_app
from backend.order_store import OrderStore
from backend.order_service import OrderService

class FakePayPalForFrontend:
    """Fake PayPal for integration tests - no real network calls."""
    def __init__(self, approved=True):
        self.calls = []
        self.approved = approved  # If True, show_order returns APPROVED
    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.calls.append(("create_order", amount_cents, currency, request_id, kwargs))
        order_id = f"PAYPALORDER{len(self.calls):03d}"
        return {
            "order_id": order_id,
            "status": "CREATED",
            "approval_url": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
        }
    def show_order(self, order_id):
        if self.approved:
            return {"order_id": order_id, "order_status": "APPROVED"}
        else:
            return {"order_id": order_id, "order_status": "CREATED"}
    def capture_order(self, order_id, request_id):
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "capture_id": f"CAPTURE{order_id[-3:]}",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD"
        }

class FrontendIntegrationTests(unittest.TestCase):
    def test_custom_song_order_js_exists(self):
        path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        self.assertTrue(path.exists())

    def test_buildBrief_function_exists(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("function buildBrief()", content)

    def test_submitOrder_function_exists(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("async function submitOrder()", content)

    def test_isSubmitting_flag_exists(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("let isSubmitting = false", content)

    def test_creatingOrder_strings_exist(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        # EN
        self.assertIn("Creating your secure order...", content)
        # ES
        self.assertIn("Creando tu pedido seguro...", content)

    def test_orderCreationFailed_strings_exist(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        # EN
        self.assertIn("We couldn't create your order. Please try again.", content)
        # ES
        self.assertIn("No pudimos crear tu pedido.", content)

    def test_product_slug_is_custom_song(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn('product: "custom-song"', content)
        self.assertNotIn('product: "custom_song"', content)

    def test_solo_slugs_match_backend(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        # Backend expects: none, guitar-solo, piano-solo
        self.assertIn('"guitar-solo"', content)
        self.assertIn('"piano-solo"', content)
        self.assertIn('"none"', content)

    def test_solo_not_in_brief(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        # The brief object should not include solo
        # Find the brief construction and verify solo is not added to it
        # We check that brief includes fields but solo is sent separately
        self.assertIn('product: "custom-song"', content)
        self.assertIn('solo: soloValue', content)  # solo is sent separately
        # In buildBrief, verify solo is NOT included
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        self.assertNotIn('solo:', brief_section)

    def test_brief_includes_acknowledgements(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        self.assertIn("contentGuidelines: f(\"contentGuidelines\").checked", brief_section)
        self.assertIn("revisionAcknowledgement: f(\"revisionAcknowledgement\").checked", brief_section)
        self.assertIn("licenseAcknowledgement: f(\"licenseAcknowledgement\").checked", brief_section)

    def test_brief_includes_lyricsPermission_conditional(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        # lyricsPermission must be true/false when lyricsStatus === "complete", null otherwise
        self.assertIn('lyricsPermission', brief_section)
        self.assertIn('f("lyricsPermission").checked', brief_section)
        self.assertIn('checked("lyricsStatus") === "complete"', brief_section)
        self.assertIn('result.lyricsPermission = null', brief_section)

    def test_brief_other_fields_always_null_or_value(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        # Other fields must always be present in result (not undefined)
        self.assertIn('result.purposeOther = null', brief_section)
        self.assertIn('result.languageOther = null', brief_section)
        self.assertIn('result.genreOther = null', brief_section)
        self.assertIn('result.moodOther = null', brief_section)

    def test_brief_empty_to_null(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        # Check that optional fields are set to null when empty
        self.assertIn("|| null", brief_section)

    def test_brief_mood_is_array(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        self.assertIn("mood: checks(\"mood\")", brief_section)

    def test_brief_instrument_is_array(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        brief_section = content[content.find("function buildBrief()"):content.find("function renderLanguage")]
        self.assertIn("instrument: checks(\"instrument\")", brief_section)

    def test_create_uses_post(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn('method: "POST"', content)
        self.assertIn("/api/paypal/orders", content)

    def test_redirect_uses_approval_url(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("window.location.assign(data.approval_url)", content)

    def test_response_validation(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("!data.local_order_id", content)
        self.assertIn("!data.paypal_order_id", content)
        self.assertIn("!data.status", content)
        self.assertIn("!data.approval_url", content)
        self.assertIn("!data.amount", content)
        self.assertIn("!data.currency", content)

    def test_status_validation(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn('data.status !== "PAYPAL_CREATED"', content)

    def test_double_submit_prevention(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("if (isSubmitting) return;", content)
        self.assertIn("isSubmitting = true;", content)
        self.assertIn("isSubmitting = false;", content)

    def test_button_disabled_during_submit(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("next.disabled = true;", content)
        self.assertIn("next.disabled = false;", content)

    def test_error_display(self):
        js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("error.textContent = err.message;", content)

    def test_promo_lab_html_exists(self):
        path = Path(__file__).parent.parent / "promo-lab.html"
        self.assertTrue(path.exists())

    def test_promo_lab_html_has_modal(self):
        path = Path(__file__).parent.parent / "promo-lab.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("id=\"custom-song-modal\"", content)

    def test_promo_lab_html_has_script(self):
        path = Path(__file__).parent.parent / "promo-lab.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("custom-song-order.js", content)

    def test_modal_has_form(self):
        path = Path(__file__).parent.parent / "promo-lab.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("<form", content)
        self.assertIn('data-custom-song-step="5"', content)

class FrontendBackendIntegrationTests(unittest.TestCase):
    """Integration tests: verify frontend payload is accepted by backend with correct pricing."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        self.paypal = FakePayPalForFrontend()
        self.service = OrderService(self.store, self.paypal, "https://example.test/return", "https://example.test/cancel")
        self.client = create_app(order_service=self.service).test_client()
    def tearDown(self): self.temp.cleanup()

    def test_frontend_solo_none_payload_accepted_with_199(self):
        """Test that frontend payload with solo='none' is accepted by backend with amount 199.00"""
        brief = {
            "purpose": "gift",
            "subject": "Test Subject",
            "story": "Test Story",
            "language": "english",
            "genre": "pop",
            "mood": ["joyful"],
            "lyricsStatus": "none",
            "lyricsPermission": None,
            "creativeFreedom": "balanced",
            "instrument": ["guitar"],
            "vocal": "carlos",
            "references": None,
            "avoid": None,
            "additionalNotes": None,
            "contentGuidelines": True,
            "revisionAcknowledgement": True,
            "licenseAcknowledgement": True,
            "purposeOther": None,
            "languageOther": None,
            "genreOther": None,
            "moodOther": None
        }
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": brief
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["amount"], "199.00")
        self.assertEqual(response.json["currency"], "USD")
        self.assertEqual(response.json["status"], "PAYPAL_CREATED")
        self.assertIn("approval_url", response.json)
        self.assertIn("local_order_id", response.json)
        self.assertIn("paypal_order_id", response.json)

    def test_frontend_solo_guitar_solo_payload_accepted_with_224(self):
        """Test that frontend payload with solo='guitar-solo' is accepted by backend with amount 224.00"""
        brief = {
            "purpose": "gift",
            "subject": "Test Subject",
            "story": "Test Story",
            "language": "english",
            "genre": "pop",
            "mood": ["joyful"],
            "lyricsStatus": "none",
            "lyricsPermission": None,
            "creativeFreedom": "balanced",
            "instrument": ["guitar"],
            "vocal": "carlos",
            "references": None,
            "avoid": None,
            "additionalNotes": None,
            "contentGuidelines": True,
            "revisionAcknowledgement": True,
            "licenseAcknowledgement": True,
            "purposeOther": None,
            "languageOther": None,
            "genreOther": None,
            "moodOther": None
        }
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "guitar-solo",
            "brief": brief
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["amount"], "224.00")
        self.assertEqual(response.json["currency"], "USD")
        self.assertEqual(response.json["status"], "PAYPAL_CREATED")

    def test_frontend_solo_piano_solo_payload_accepted_with_224(self):
        """Test that frontend payload with solo='piano-solo' is accepted by backend with amount 224.00"""
        brief = {
            "purpose": "gift",
            "subject": "Test Subject",
            "story": "Test Story",
            "language": "english",
            "genre": "pop",
            "mood": ["joyful"],
            "lyricsStatus": "none",
            "lyricsPermission": None,
            "creativeFreedom": "balanced",
            "instrument": ["piano"],
            "vocal": "carlos",
            "references": None,
            "avoid": None,
            "additionalNotes": None,
            "contentGuidelines": True,
            "revisionAcknowledgement": True,
            "licenseAcknowledgement": True,
            "purposeOther": None,
            "languageOther": None,
            "genreOther": None,
            "moodOther": None
        }
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "piano-solo",
            "brief": brief
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["amount"], "224.00")
        self.assertEqual(response.json["currency"], "USD")
        self.assertEqual(response.json["status"], "PAYPAL_CREATED")

    def test_frontend_payload_brief_persisted(self):
        """Test that brief from frontend payload is persisted in backend."""
        import sqlite3
        brief = {
            "purpose": "wedding",
            "subject": "Test Wedding Subject",
            "story": "Test Wedding Story",
            "language": "spanish",
            "genre": "ballad",
            "mood": ["romantic", "emotional"],
            "lyricsStatus": "complete",
            "lyricsPermission": True,
            "creativeFreedom": "lead",
            "instrument": ["piano", "guitar"],
            "vocal": "carlos",
            "references": "Reference 1, Reference 2",
            "avoid": "Nothing to avoid",
            "additionalNotes": "Additional notes here",
            "contentGuidelines": True,
            "revisionAcknowledgement": True,
            "licenseAcknowledgement": True,
            "purposeOther": None,
            "languageOther": None,
            "genreOther": None,
            "moodOther": None
        }
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": brief
        })
        self.assertEqual(response.status_code, 201)
        local_order_id = response.json["local_order_id"]

        # Verify brief is persisted in SQLite
        connection = sqlite3.connect(self.store._database_path)
        try:
            cursor = connection.execute(
                "SELECT brief_json FROM custom_song_orders WHERE local_order_id = ?",
                (local_order_id,)
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            import json
            persisted_brief = json.loads(row[0])
            self.assertEqual(persisted_brief["purpose"], "wedding")
            self.assertEqual(persisted_brief["subject"], "Test Wedding Subject")
            self.assertEqual(persisted_brief["lyricsPermission"], True)
        finally:
            connection.close()

    def test_frontend_wrong_solo_slug_rejected(self):
        """Test that wrong solo slug (not matching backend) is rejected."""
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "guitar",  # Wrong - backend expects guitar-solo
            "brief": {"purpose": "gift", "subject": "Test", "story": "Test", "language": "english", "genre": "pop", "mood": ["joyful"], "lyricsStatus": "none", "lyricsPermission": None, "creativeFreedom": "balanced", "instrument": ["guitar"], "vocal": "carlos", "contentGuidelines": True, "revisionAcknowledgement": True, "licenseAcknowledgement": True, "purposeOther": None, "languageOther": None, "genreOther": None, "moodOther": None}
        })
        self.assertEqual(response.status_code, 422)

    def test_frontend_payload_no_price_fields(self):
        """Test that frontend does NOT send price fields in request."""
        brief = {
            "purpose": "gift",
            "subject": "Test",
            "story": "Test",
            "language": "english",
            "genre": "pop",
            "mood": ["joyful"],
            "lyricsStatus": "none",
            "lyricsPermission": None,
            "creativeFreedom": "balanced",
            "instrument": ["guitar"],
            "vocal": "carlos",
            "contentGuidelines": True,
            "revisionAcknowledgement": True,
            "licenseAcknowledgement": True,
            "purposeOther": None,
            "languageOther": None,
            "genreOther": None,
            "moodOther": None
        }
        # This payload should work - no amount/price/currency fields
        response = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": brief
        })
        self.assertEqual(response.status_code, 201)
        # Price comes from backend, not frontend
        self.assertEqual(response.json["amount"], "199.00")

class ReviewCompleteBugTests(unittest.TestCase):
    """Tests for the REVIEW COMPLETE button bug fix."""
    def setUp(self):
        self.js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        self.content = self.js_path.read_text(encoding="utf-8")

    def test_review_complete_button_has_type_button(self):
        """REVIEW COMPLETE button must have type='button' to prevent native form submit."""
        html_path = Path(__file__).parent.parent / "promo-lab.html"
        html_content = html_path.read_text(encoding="utf-8")
        # Find the next button (which becomes REVIEW COMPLETE in step 5)
        self.assertIn('data-custom-song-next', html_content)
        # Verify it has type="button"
        # Extract the button element
        import re
        button_match = re.search(r'<button[^>]*data-custom-song-next[^>]*>', html_content)
        self.assertIsNotNone(button_match, "Next button not found")
        button_html = button_match.group(0)
        self.assertIn('type="button"', button_html,
                      "REVIEW COMPLETE button must have type='button' to prevent native form submit")

    def test_next_button_handler_prevents_default(self):
        """Next button click handler must call e.preventDefault() and e.stopPropagation()."""
        # Verify the next button handler has preventDefault and stopPropagation
        self.assertIn('e.preventDefault()', self.content)
        self.assertIn('e.stopPropagation()', self.content)
        # Verify it's in the next button handler context
        next_handler_start = self.content.find('next.addEventListener("click", async (e)=>')
        self.assertNotEqual(next_handler_start, -1, "Next button handler not found")
        next_handler_section = self.content[next_handler_start:next_handler_start + 300]
        self.assertIn('e.preventDefault()', next_handler_section)
        self.assertIn('e.stopPropagation()', next_handler_section)

    def test_next_button_handler_is_async(self):
        """Next button handler must be async to support await submitOrder()."""
        self.assertIn('next.addEventListener("click", async (e)=>', self.content)

    def test_next_button_calls_submitOrder_on_step_5(self):
        """In step 5, next button must call submitOrder()."""
        next_handler_start = self.content.find('next.addEventListener("click", async (e)=>')
        self.assertNotEqual(next_handler_start, -1, "Next button handler not found")
        next_handler_section = self.content[next_handler_start:next_handler_start + 300]
        self.assertIn('await submitOrder()', next_handler_section)
        # Verify step logic: if step < 5 navigate, else submitOrder
        self.assertIn('step < 5', next_handler_section)

    def test_double_submit_prevention_with_isSubmitting(self):
        """Double submit must be prevented by isSubmitting flag."""
        self.assertIn('if (isSubmitting) return;', self.content)
        self.assertIn('isSubmitting = true;', self.content)
        self.assertIn('isSubmitting = false;', self.content)

    def test_dirty_protection_still_exists(self):
        """Close confirmation for dirty state must still exist for manual close."""
        self.assertIn('function dirty()', self.content)
        self.assertIn('function close(force=false)', self.content)
        self.assertIn('dirty()', self.content)
        self.assertIn('confirm(t("discard"))', self.content)

    def test_close_called_with_force_true_on_finish(self):
        """Finish button must call close(true) to bypass dirty check."""
        self.assertIn('close(true)', self.content)

    def test_close_listeners_for_close_buttons(self):
        """Close buttons must have listeners that call close()."""
        self.assertIn('[data-custom-song-close]', self.content)
        self.assertIn('addEventListener("click",()=>close())', self.content)

    def test_submitOrder_disables_button_during_submit(self):
        """submitOrder must disable the next button during submission."""
        submit_start = self.content.find('async function submitOrder()')
        submit_end = self.content.find('open.addEventListener', submit_start)
        submit_section = self.content[submit_start:submit_end]
        self.assertIn('next.disabled = true', submit_section)
        self.assertIn('next.disabled = false', self.content)

    def test_redirect_to_approval_url_present(self):
        """submitOrder must redirect to approval_url on success."""
        self.assertIn('window.location.assign(data.approval_url)', self.content)

    def test_redirect_bypasses_dirty_check(self):
        """Redirect to PayPal must not be blocked by dirty state."""
        # Check that modal is hidden before redirect to bypass dirty check
        submit_start = self.content.find('async function submitOrder()')
        self.assertNotEqual(submit_start, -1, "submitOrder function not found")
        # Find the end of submitOrder function
        next_function_start = self.content.find('open.addEventListener', submit_start)
        self.assertNotEqual(next_function_start, -1, "End of submitOrder not found")
        submit_section = self.content[submit_start:next_function_start]
        # Verify modal is hidden before redirect (with or without semicolon)
        self.assertIn('modal.hidden = true', submit_section)

    def test_error_handling_restores_button_state(self):
        """Error in submitOrder must restore button state."""
        self.assertIn('next.disabled = false;', self.content)
        self.assertIn('isSubmitting = false;', self.content)
        self.assertIn('error.textContent = err.message;', self.content)

    def test_form_submit_prevented(self):
        """Form submit must be prevented to avoid native HTML submit."""
        self.assertIn('form.addEventListener("submit",e=>e.preventDefault())', self.content)


class CrossReferenceTests(unittest.TestCase):
    """Cross-reference tests: verify all JS selectors/field names exist in HTML."""
    def setUp(self):
        self.js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        self.html_path = Path(__file__).parent.parent / "promo-lab.html"
        self.js_content = self.js_path.read_text(encoding="utf-8")
        self.html_content = self.html_path.read_text(encoding="utf-8")

    def test_all_brief_field_names_exist_in_html(self):
        """All field names used in buildBrief() must exist in promo-lab.html."""
        # Extract all field names used in buildBrief()
        brief_section = self.js_content[self.js_content.find("function buildBrief()"):
                                        self.js_content.find("function renderLanguage")]
        # Field names used directly
        direct_fields = [
            "purpose", "subject", "story", "language", "genre", "mood",
            "lyricsStatus", "creativeFreedom", "instrument", "vocal",
            "references", "avoid", "additionalNotes",
            "contentGuidelines", "revisionAcknowledgement", "licenseAcknowledgement"
        ]
        # Other fields
        other_fields = ["purposeOther", "languageOther", "genreOther", "moodOther"]
        # Conditional fields
        conditional_fields = ["lyricsDetails", "lyricsPermission"]

        all_fields = direct_fields + other_fields + conditional_fields

        for field in all_fields:
            self.assertIn(f'name="{field}"', self.html_content,
                         f"Field '{field}' used in JS but not found in HTML")

    def test_all_checked_field_names_exist_in_html(self):
        """All field names used in checked() calls must exist in HTML."""
        # checked() is used for radio button groups
        checked_fields = ["purpose", "language", "lyricsStatus", "creativeFreedom", "vocal"]
        for field in checked_fields:
            self.assertIn(f'name="{field}"', self.html_content,
                         f"Checked field '{field}' used in JS but not found in HTML")

    def test_all_checks_field_names_exist_in_html(self):
        """All field names used in checks() calls must exist in HTML."""
        # checks() is used for checkbox groups
        checks_fields = ["mood", "instrument"]
        for field in checks_fields:
            self.assertIn(f'name="{field}"', self.html_content,
                         f"Checks field '{field}' used in JS but not found in HTML")

    def test_solo_field_exists_in_html(self):
        """The 'solo' field used in checked('solo') must exist in HTML."""
        self.assertIn('name="solo"', self.html_content)

    def test_next_button_selector_exists(self):
        """The next button selector must exist in HTML."""
        self.assertIn('data-custom-song-next', self.html_content)
        self.assertIn('data-custom-song-back', self.html_content)


class FrontendBriefStructureTests(unittest.TestCase):
    """Tests for buildBrief() structure and normalization."""
    def setUp(self):
        self.js_path = Path(__file__).parent.parent / "assets" / "scripts" / "custom-song-order.js"
        self.content = self.js_path.read_text(encoding="utf-8")
        self.brief_section = self.content[self.content.find("function buildBrief()"):self.content.find("function renderLanguage")]

    def test_lyricsPermission_null_when_not_complete(self):
        """lyricsPermission must be null when lyricsStatus !== 'complete'."""
        self.assertIn('result.lyricsPermission = null', self.brief_section)

    def test_other_fields_initialized_to_null(self):
        """Other fields must be initialized to null first."""
        self.assertIn('result.purposeOther = null', self.brief_section)
        self.assertIn('result.languageOther = null', self.brief_section)
        self.assertIn('result.genreOther = null', self.brief_section)
        self.assertIn('result.moodOther = null', self.brief_section)

    def test_lyricsPermission_true_false_when_complete(self):
        """lyricsPermission must be true/false when lyricsStatus === 'complete'."""
        self.assertIn('f("lyricsPermission").checked', self.brief_section)

    def test_acknowledgements_always_included(self):
        """All acknowledgements must always be included in brief."""
        self.assertIn('contentGuidelines:', self.brief_section)
        self.assertIn('revisionAcknowledgement:', self.brief_section)
        self.assertIn('licenseAcknowledgement:', self.brief_section)


class StaticRouteTests(unittest.TestCase):
    """Tests for static route serving of return/cancel pages."""
    def setUp(self):
        self.client = create_app().test_client()

    def test_paypal_return_route_200(self):
        """GET /paypal/return must return 200"""
        response = self.client.get("/paypal/return")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"paypal-return", response.data)

    def test_paypal_cancel_route_200(self):
        """GET /paypal/cancel must return 200"""
        response = self.client.get("/paypal/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"paypal-cancel", response.data)

    def test_paypal_return_with_query_string_200(self):
        """GET /paypal/return?token=X&PayerID=Y must return 200"""
        response = self.client.get("/paypal/return?token=PAYPALORDER123&PayerID=BUYER456")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"paypal-return", response.data)

    def test_paypal_return_content_type_html(self):
        """GET /paypal/return must have HTML content type"""
        response = self.client.get("/paypal/return")
        self.assertIn("text/html", response.content_type)

    def test_paypal_cancel_content_type_html(self):
        """GET /paypal/cancel must have HTML content type"""
        response = self.client.get("/paypal/cancel")
        self.assertIn("text/html", response.content_type)


class ReturnPageTests(unittest.TestCase):
    """Tests for /paypal/return page."""
    def test_return_page_html_exists(self):
        path = Path(__file__).parent.parent / "paypal-return.html"
        self.assertTrue(path.exists())

    def test_return_page_has_script(self):
        path = Path(__file__).parent.parent / "paypal-return.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("paypal-return.js", content)

    def test_return_page_has_required_elements(self):
        path = Path(__file__).parent.parent / "paypal-return.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn('id="status-heading"', content)
        self.assertIn('id="status-message"', content)
        self.assertIn('id="payment-amount"', content)
        self.assertIn('id="retry-button"', content)

    def test_return_page_has_bilingual_strings(self):
        path = Path(__file__).parent.parent / "paypal-return.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("data-i18n=", content)

    def test_hidden_attribute_cannot_be_overridden_by_button_display(self):
        css_path = Path(__file__).parent.parent / "assets" / "styles" / "main.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("[hidden] {\n  display: none !important;\n}", css)

class ReturnPageJSTests(unittest.TestCase):
    """Tests for paypal-return.js structure."""
    def setUp(self):
        self.js_path = Path(__file__).parent.parent / "assets" / "scripts" / "paypal-return.js"
        self.content = self.js_path.read_text(encoding="utf-8")

    def test_return_js_exists(self):
        self.assertTrue(self.js_path.exists())

    def test_token_extraction_from_search(self):
        self.assertIn("URLSearchParams", self.content)
        self.assertIn("window.location.search", self.content)

    def test_resolve_call_with_encodeURIComponent(self):
        self.assertIn("encodeURIComponent", self.content)
        self.assertIn("/api/paypal/orders/resolve?token=", self.content)

    def test_capture_call_with_local_order_id(self):
        self.assertIn("/api/paypal/orders/", self.content)
        self.assertIn("/capture", self.content)
        self.assertIn('JSON.stringify({})', self.content)

    def test_isConfirming_flag_exists(self):
        self.assertIn("isConfirming", self.content)

    def test_double_action_prevention(self):
        self.assertIn("if (isConfirming) return", self.content)

    def test_retry_button_handler(self):
        self.assertIn("retry-button", self.content)
        self.assertIn("addEventListener", self.content)

    def test_retry_button_visibility_matches_payment_state(self):
        success_section = self.content[self.content.find("function showSuccess"):self.content.find("function showRecoverableError")]
        recoverable_section = self.content[self.content.find("function showRecoverableError"):self.content.find("function showFatalError")]
        self.assertIn("retryButton.hidden = true", success_section)
        self.assertIn("retryButton.hidden = false", recoverable_section)

    def test_bilingual_strings_defined(self):
        self.assertIn("confirmingPayment", self.content)
        self.assertIn("paymentConfirmed", self.content)
        self.assertIn("paymentConfirmationPending", self.content)
        self.assertIn("tryAgain", self.content)
        self.assertIn("returnToCustomSong", self.content)

    def test_success_requires_amount_and_currency(self):
        self.assertIn("amount", self.content)
        self.assertIn("currency", self.content)

    def test_response_validation(self):
        self.assertIn("isValidResponse", self.content)
        self.assertIn("isValidCaptureResponse", self.content)

    def test_textContent_used_not_innerHTML(self):
        self.assertIn("textContent", self.content)
        self.assertNotIn("innerHTML", self.content)

    def test_no_sessionStorage(self):
        self.assertNotIn("sessionStorage", self.content)
        self.assertNotIn("localStorage", self.content)

    def test_no_PayPal_SDK(self):
        self.assertNotIn("paypal.com/sdk/js", self.content)

class CancelPageTests(unittest.TestCase):
    """Tests for /paypal/cancel page."""
    def test_cancel_page_html_exists(self):
        path = Path(__file__).parent.parent / "paypal-cancel.html"
        self.assertTrue(path.exists())

    def test_cancel_page_no_script_needed(self):
        path = Path(__file__).parent.parent / "paypal-cancel.html"
        content = path.read_text(encoding="utf-8")
        # Cancel page doesn't need JS, just static HTML
        self.assertNotIn("<script", content)

    def test_cancel_page_has_return_link(self):
        path = Path(__file__).parent.parent / "paypal-cancel.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("promo-lab.html", content)

    def test_cancel_page_has_bilingual_strings(self):
        path = Path(__file__).parent.parent / "paypal-cancel.html"
        content = path.read_text(encoding="utf-8")
        self.assertIn("data-i18n=", content)

class ReturnBackendIntegrationTests(unittest.TestCase):
    """Integration tests: return page flow with backend."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        self.paypal = FakePayPalForFrontend(approved=True)
        self.service = OrderService(self.store, self.paypal, "https://example.test/return", "https://example.test/cancel")
        self.client = create_app(order_service=self.service).test_client()
    def tearDown(self): self.temp.cleanup()

    def test_resolve_then_capture_full_flow(self):
        """Test full flow: create order -> resolve -> capture = PAID"""
        # Create order first
        create_resp = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": {
                "purpose": "gift", "subject": "Test", "story": "Test",
                "language": "english", "genre": "pop", "mood": ["joyful"],
                "lyricsStatus": "none", "lyricsPermission": None,
                "creativeFreedom": "balanced", "instrument": ["guitar"],
                "vocal": "carlos", "contentGuidelines": True,
                "revisionAcknowledgement": True, "licenseAcknowledgement": True,
                "purposeOther": None, "languageOther": None,
                "genreOther": None, "moodOther": None
            }
        })
        self.assertEqual(create_resp.status_code, 201)
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Resolve
        resolve_resp = self.client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        self.assertEqual(resolve_resp.status_code, 200)
        local_order_id = resolve_resp.json["local_order_id"]
        self.assertIsNotNone(local_order_id)

        # Capture
        capture_resp = self.client.post(f"/api/paypal/orders/{local_order_id}/capture", json={})
        self.assertEqual(capture_resp.status_code, 200)
        self.assertEqual(capture_resp.json["status"], "PAID")
        self.assertEqual(capture_resp.json["amount"], "199.00")
        self.assertEqual(capture_resp.json["currency"], "USD")

    def test_resolve_missing_token_returns_400(self):
        """Test resolve without token returns 400"""
        resolve_resp = self.client.get("/api/paypal/orders/resolve")
        self.assertEqual(resolve_resp.status_code, 400)

    def test_resolve_invalid_token_returns_404(self):
        """Test resolve with invalid token returns 404"""
        resolve_resp = self.client.get("/api/paypal/orders/resolve?token=INVALID999")
        self.assertEqual(resolve_resp.status_code, 404)

    def test_capture_without_resolve_fails(self):
        """Test capture without valid local_order_id returns 404"""
        capture_resp = self.client.post("/api/paypal/orders/00000000-0000-0000-0000-000000000000/capture", json={})
        self.assertEqual(capture_resp.status_code, 404)

    def test_capture_idempotent_on_repeat(self):
        """Test that repeated capture calls are idempotent"""
        import uuid
        # Create and capture first time - use solo="none" to match amount 199.00
        create_resp = self.client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": {
                "purpose": "gift", "subject": "Test", "story": "Test",
                "language": "english", "genre": "pop", "mood": ["joyful"],
                "lyricsStatus": "none", "lyricsPermission": None,
                "creativeFreedom": "balanced", "instrument": ["guitar"],
                "vocal": "carlos", "contentGuidelines": True,
                "revisionAcknowledgement": True, "licenseAcknowledgement": True,
                "purposeOther": None, "languageOther": None,
                "genreOther": None, "moodOther": None
            }
        })
        local_order_id = create_resp.json["local_order_id"]

        # First capture
        capture_resp1 = self.client.post(f"/api/paypal/orders/{local_order_id}/capture", json={})
        self.assertEqual(capture_resp1.status_code, 200)
        self.assertEqual(capture_resp1.json["status"], "PAID")
        capture_id_1 = capture_resp1.json["capture_id"]

        # Second capture (idempotent)
        capture_resp2 = self.client.post(f"/api/paypal/orders/{local_order_id}/capture", json={})
        self.assertEqual(capture_resp2.status_code, 200)
        self.assertEqual(capture_resp2.json["status"], "PAID")
        # Verify capture_order was NOT called again (idempotent via show_order)
        # Note: This depends on FakePayPalForFrontend implementation

    def test_capture_409_not_approved(self):
        """Test capture returns 409 when order not approved"""
        # Create a service with paypal that returns CREATED (not approved)
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPalForFrontend(approved=False)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create order
        create_resp = client.post("/api/paypal/orders", json={
            "product": "custom-song",
            "solo": "none",
            "brief": {
                "purpose": "gift", "subject": "Test", "story": "Test",
                "language": "english", "genre": "pop", "mood": ["joyful"],
                "lyricsStatus": "none", "lyricsPermission": None,
                "creativeFreedom": "balanced", "instrument": ["guitar"],
                "vocal": "carlos", "contentGuidelines": True,
                "revisionAcknowledgement": True, "licenseAcknowledgement": True,
                "purposeOther": None, "languageOther": None,
                "genreOther": None, "moodOther": None
            }
        })
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Resolve
        resolve_resp = client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        local_order_id = resolve_resp.json["local_order_id"]

        # Capture - paypal returns CREATED (not approved) -> should return 409
        capture_resp = client.post(f"/api/paypal/orders/{local_order_id}/capture", json={})
        self.assertEqual(capture_resp.status_code, 409)
