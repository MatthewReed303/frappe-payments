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
		with (
			patch.object(stripe_settings, "intent_request", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			settings.refund("Payment Request", "PR-2", 5, "CN-2")
