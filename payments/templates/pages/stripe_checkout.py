# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
	"payment_gateway",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)
		elif frappe.get_meta(context.reference_doctype).has_field("grand_total"):
			# a one-off payment: a PaymentIntent for what the document asks, so the bank can
			# authenticate the payer (3-D Secure) and wallets work
			settings = frappe.get_doc("Stripe Settings", gateway_controller)
			intent = settings.payment_intent(context.reference_doctype, context.reference_docname)
			if intent.status == "succeeded":
				# paid already (the payer came back to this page): recorded once, then on as after paying
				frappe.local.flags.commit = True
				frappe.local.flags.redirect_location = (
					settings.record_payment_intent(intent)
					or f"/payment-success?doctype={context.reference_doctype}&docname={context.reference_docname}"
				)
				raise frappe.Redirect(302)
			if intent.status == "processing":
				frappe.redirect_to_message(
					_("Payment processing"),
					_("Your payment is processing. You'll get an email once it's confirmed."),
				)
				frappe.local.flags.redirect_location = frappe.local.response.location
				raise frappe.Redirect(302)
			context.client_secret = intent.client_secret
			reference = frappe.get_doc(context.reference_doctype, context.reference_docname)
			context["amount"] = fmt_money(amount=reference.grand_total, currency=reference.currency)

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key

	return publishable_key


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


# Guest checkout: buyers pay without a site account.
@frappe.whitelist(allow_guest=True)  # nosemgrep
def make_payment(
	stripe_token_id: str,
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	data = json.loads(data)

	data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	# Also reachable over GET, which Frappe does not commit, and the payment must be recorded.
	frappe.db.commit()  # nosemgrep
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")


# Guest checkout: the payer returns here from Stripe without a site account.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=20, seconds=60)
def confirm_payment(
	payment_intent: str, reference_doctype: str, reference_docname: str, payment_gateway: str
):
	"""After Stripe confirms a payment in the browser: check the PaymentIntent with Stripe and record
	the payment against its document."""
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	intent = settings.client().payment_intents.retrieve(payment_intent)
	metadata = intent.metadata or {}
	if (metadata.get("reference_doctype"), metadata.get("reference_docname")) != (
		reference_doctype,
		reference_docname,
	):
		raise frappe.PermissionError
	if intent.status == "succeeded":
		redirect_to = settings.record_payment_intent(intent)
		# reachable as a guest, and the payment must be recorded
		frappe.db.commit()  # nosemgrep
		return {
			"status": "Completed",
			"redirect_to": redirect_to
			or f"payment-success?doctype={reference_doctype}&docname={reference_docname}",
		}
	if intent.status == "processing":
		return {
			"status": "Processing",
			"message": _("Your payment is processing. You'll get an email once it's confirmed."),
		}
	return {
		"status": "Failed",
		"message": _("The payment didn't go through. Please try again or use another card."),
	}
