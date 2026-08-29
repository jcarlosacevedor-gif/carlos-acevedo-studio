import tempfile
import unittest
from pathlib import Path

from backend.app import create_app
from backend.order_store import OrderStore
from backend.order_service import OrderService


class FakePayPalForFrontend:
    """Fake PayPal for integration tests - no real network calls."""
    def __init__(self):
        self.calls = []
    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.calls.append(("create_order", amount_cents, currency, request_id, kwargs))
        order_id = f"PAYPALORDER{len(self.calls):03d}"
        return {
            "order_id": order_id,
            "status": "CREATED",
            "approval_url": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
        }
    def show_order(self, order_id):
        return {"order_id": order_id, "order_status": "CREATED"}
    def capture_order(self, order_id, request_id):
        return {"order_id": order_id, "order_status": "COMPLETED"}


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
        self.assertIn('contentGuidelines: f("contentGuidelines").checked', self.brief_section)
        self.assertIn('revisionAcknowledgement: f("revisionAcknowledgement").checked', self.brief_section)
        self.assertIn('licenseAcknowledgement: f("licenseAcknowledgement").checked', self.brief_section)
