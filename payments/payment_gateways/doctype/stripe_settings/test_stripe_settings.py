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
