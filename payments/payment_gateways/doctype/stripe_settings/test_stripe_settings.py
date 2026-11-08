# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import frappe
from frappe.tests import IntegrationTestCase


class TestStripeSettings(IntegrationTestCase):
	def test_failed_payment_redirect_message_starts_query_string(self):
		controller = frappe.new_doc("Stripe Settings")
		controller.data = frappe._dict(redirect_message="Card declined")
		controller.integration_request = frappe._dict(status="Failed")

		response = controller.finalize_request()

		self.assertEqual(response["redirect_to"], "payment-failed?redirect_message=Card+declined")

	def test_failed_payment_redirect_message_follows_redirect_to(self):
		controller = frappe.new_doc("Stripe Settings")
		controller.data = frappe._dict(redirect_to="/orders", redirect_message="Card declined")
		controller.integration_request = frappe._dict(status="Failed")

		response = controller.finalize_request()

		self.assertEqual(
			response["redirect_to"],
			"payment-failed?redirect_to=%2Forders&redirect_message=Card+declined",
		)

	def test_amounts_are_in_the_currencys_smallest_unit(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import minor_units

		self.assertEqual(minor_units(293.29, "NZD"), 29329)
		self.assertEqual(minor_units(1000, "JPY"), 1000)
		self.assertEqual(minor_units(12.35, "KWD"), 12350)

	def test_webhook_needs_a_gateway_with_a_signing_secret(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import webhook

		with self.assertRaises(frappe.PermissionError):
			webhook(gateway="_Test Missing Gateway")
		settings = frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"gateway_name": "_Test Stripe",
				"publishable_key": "pk_test_x",
				"secret_key": "sk_test_x",
			}
		)
		settings.flags.ignore_mandatory = True
		settings.insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			webhook(gateway="_Test Stripe")

	def test_refund_takes_back_part_of_a_card_payment_once(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		settings = frappe.new_doc("Stripe Settings")
		client = MagicMock()
		client.refunds.create.return_value = frappe._dict(id="re_test_1")
		paid = frappe._dict(name="IR-1", request_id="pi_test_1", status="Completed")
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=paid),
			patch("frappe.db.get_value", return_value="NZD"),
			patch.object(stripe_settings, "create_request_log") as log,
		):
			self.assertEqual(settings.refund("Payment Request", "PR-1", 12.5, "CN-1"), "re_test_1")
		params = client.refunds.create.call_args.kwargs["params"]
		self.assertEqual((params["payment_intent"], params["amount"]), ("pi_test_1", 1250))
		self.assertIn("CN-1", client.refunds.create.call_args.kwargs["options"]["idempotency_key"])
		self.assertEqual(log.call_args.kwargs["request_id"], "re_test_1")
		self.assertEqual(
			(log.call_args.kwargs["reference_doctype"], log.call_args.kwargs["reference_docname"]),
			("Integration Request", "IR-1"),
		)
		with (
			patch.object(stripe_settings, "intent_request", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			settings.refund("Payment Request", "PR-2", 5, "CN-2")

	def test_pay_page_sends_a_payer_who_has_paid_on(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		settings.payment_intent.return_value = frappe._dict(status="succeeded", client_secret="cs_x")
		settings.record_payment_intent.return_value = "/order/SO-1"
		form = dict.fromkeys(stripe_checkout.expected_keys, "x")
		form.update(amount=10, currency="NZD", reference_doctype="Payment Request")
		get_doc = frappe.get_doc
		with (
			patch.object(frappe.local, "form_dict", frappe._dict(form)),
			patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
			patch.object(stripe_checkout, "get_api_key", return_value="pk_test_x"),
			patch.object(stripe_checkout, "get_header_image", return_value=None),
			patch.object(stripe_checkout, "is_a_subscription", return_value=False),
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: (
					settings if args[0] == "Stripe Settings" else get_doc(*args, **kwargs)
				),
			),
			self.assertRaises(frappe.Redirect),
		):
			stripe_checkout.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/order/SO-1")
		self.assertTrue(frappe.local.flags.commit)
		frappe.local.flags.commit = False

	def test_a_cancelled_payment_cant_be_paid(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		form = dict.fromkeys(stripe_checkout.expected_keys, "x")
		form.update(amount=10, currency="NZD", reference_doctype="Payment Request")
		get_doc, get_value = frappe.get_doc, frappe.db.get_value
		with (
			patch.object(frappe.local, "form_dict", frappe._dict(form)),
			patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
			patch.object(stripe_checkout, "get_api_key", return_value="pk_test_x"),
			patch.object(stripe_checkout, "get_header_image", return_value=None),
			patch.object(stripe_checkout, "is_a_subscription", return_value=False),
			patch(
				"frappe.db.get_value",
				side_effect=lambda *args, **kwargs: (
					2 if args[:3] == ("Payment Request", "x", "docstatus") else get_value(*args, **kwargs)
				),
			),
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: (
					settings if args[0] == "Stripe Settings" else get_doc(*args, **kwargs)
				),
			),
			self.assertRaises(frappe.Redirect),
		):
			stripe_checkout.get_context(frappe._dict())
		self.assertIn("/message", frappe.local.flags.redirect_location)
		settings.payment_intent.assert_not_called()

	def test_cancelling_a_document_cancels_its_open_intent(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		doc = frappe._dict(doctype="Payment Request", name="PR-1")
		open_request = frappe._dict(name="IR-1", request_id="pi_test_1", status="Queued")
		with (
			patch.object(stripe_settings, "intent_request", return_value=open_request),
			patch("frappe.enqueue") as enqueue,
		):
			stripe_settings.cancel_open_intent(doc)
		self.assertEqual(enqueue.call_args.kwargs["integration_request"], "IR-1")
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
		with (
			patch.object(
				stripe_settings, "intent_request", return_value=frappe._dict(open_request, status="Completed")
			),
			patch("frappe.enqueue") as enqueue,
		):
			stripe_settings.cancel_open_intent(doc)
		enqueue.assert_not_called()

		request = MagicMock(
			reference_doctype="Payment Request", reference_docname="PR-1", request_id="pi_test_1"
		)
		client = MagicMock()
		gateway = MagicMock()
		gateway.client.return_value = client
		for status, cancelled in (("requires_payment_method", True), ("succeeded", False)):
			client.reset_mock()
			client.payment_intents.retrieve.return_value = frappe._dict(id="pi_test_1", status=status)
			with (
				patch(
					"frappe.get_doc",
					side_effect=lambda doctype, name: request
					if doctype == "Integration Request"
					else gateway,
				),
				patch("frappe.db.get_value", return_value="_Test Stripe"),
				patch("frappe.db.exists", return_value=True),
			):
				stripe_settings.cancel_intent("IR-1")
			self.assertEqual(client.payment_intents.cancel.called, cancelled)

	def test_a_payment_for_a_cancelled_document_is_kept_for_staff(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		request = MagicMock(status="Queued")
		reference = MagicMock(docstatus=2)
		intent = frappe._dict(
			id="pi_test_1",
			status="succeeded",
			metadata={
				"reference_doctype": "Payment Request",
				"reference_docname": "PR-1",
				"site": frappe.local.site,
			},
		)
		settings = frappe.new_doc("Stripe Settings")
		with (
			patch("frappe.db.get_value", return_value="IR-1"),
			patch(
				"frappe.get_doc",
				side_effect=lambda doctype, name: request if doctype == "Integration Request" else reference,
			),
			patch("frappe.log_error") as log_error,
		):
			self.assertIsNone(settings.record_payment_intent(intent))
		self.assertEqual(request.db_set.call_args.args[0]["status"], "Failed")
		reference.run_method.assert_not_called()
		log_error.assert_called_once()

	def test_a_request_without_the_payers_email_still_opens(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		settings = frappe.new_doc("Stripe Settings")
		client = MagicMock()
		client.payment_intents.create.return_value = frappe._dict(id="pi_test_2")
		request = frappe._dict(grand_total=40, currency="USD", email_to="Administrator", subject=None)
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=None),
			patch("frappe.get_doc", return_value=request),
			patch.object(stripe_settings, "create_request_log"),
		):
			settings.payment_intent("Payment Request", "PR-3")
		self.assertIsNone(client.payment_intents.create.call_args.kwargs["params"]["receipt_email"])
