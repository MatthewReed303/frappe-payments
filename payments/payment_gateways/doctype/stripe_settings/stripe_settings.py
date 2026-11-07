# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.utils import call_hook_method, cint, flt, get_url

from payments.utils import create_payment_gateway

currency_wise_minimum_charge_amount = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}


# currencies Stripe takes in whole units, and those it takes in thousandths
ZERO_DECIMAL_CURRENCIES = frozenset(
	(
		"BIF",
		"CLP",
		"DJF",
		"GNF",
		"JPY",
		"KMF",
		"KRW",
		"MGA",
		"PYG",
		"RWF",
		"UGX",
		"VND",
		"VUV",
		"XAF",
		"XOF",
		"XPF",
	)
)
THREE_DECIMAL_CURRENCIES = frozenset(("BHD", "JOD", "KWD", "OMR", "TND"))


def minor_units(amount: float, currency: str) -> int:
	"""An amount in the smallest unit Stripe charges in for the currency."""
	currency = currency.upper()
	if currency in ZERO_DECIMAL_CURRENCIES:
		return cint(round(flt(amount)))
	if currency in THREE_DECIMAL_CURRENCIES:
		# Stripe takes these to the nearest ten thousandths
		return cint(round(flt(amount) * 100)) * 10
	return cint(round(flt(amount) * 100))


class StripeSettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RUB",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	currency_wise_minimum_charge_amount = MappingProxyType(currency_wise_minimum_charge_amount)

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentails()

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				make_get_request(url="https://api.stripe.com/v1/charges", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if currency in self.currency_wise_minimum_charge_amount:
			if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
				frappe.throw(
					_("For currency {0}, the minimum transaction amount should be {1}").format(
						currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
					)
				)

	def get_payment_url(self, **kwargs):
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		import stripe

		self.data = frappe._dict(data)
		stripe.api_key = self.get_password(fieldname="secret_key", raise_exception=False)
		stripe.default_http_client = stripe.http_client.RequestsClient()

		try:
			self.integration_request = create_request_log(self.data, service_name="Stripe")
			return self.create_charge_on_stripe()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def create_charge_on_stripe(self):
		import stripe

		try:
			charge = stripe.Charge.create(
				amount=cint(flt(self.data.amount) * 100),
				currency=self.data.currency,
				source=self.data.stripe_token_id,
				description=self.data.description,
				receipt_email=self.data.payer_email,
			)

			if charge.captured is True:
				self.integration_request.db_set("status", "Completed", update_modified=False)
				self.flags.status_changed_to = "Completed"

			else:
				frappe.log_error(charge.failure_message, "Stripe Payment not completed")

		except Exception:
			frappe.log_error(frappe.get_traceback())

		return self.finalize_request()

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode(
				{"redirect_message": redirect_message}
			)

		return {"redirect_to": redirect_url, "status": status}

	# --- PaymentIntents ---------------------------------------------------------------

	def client(self):
		import stripe

		return stripe.StripeClient(self.get_password(fieldname="secret_key", raise_exception=False))

	def payment_intent(self, reference_doctype: str, reference_docname: str):
		"""The PaymentIntent paying a document (usually a Payment Request), for its grand total in
		its currency. It is made once and reused while the document is unpaid."""
		reference = frappe.get_doc(reference_doctype, reference_docname)
		amount, currency = flt(reference.get("grand_total")), reference.get("currency")
		self.validate_transaction_currency(currency)
		self.validate_minimum_transaction_amount(currency, amount)
		units = minor_units(amount, currency)
		client = self.client()
		existing = intent_request(reference_doctype, reference_docname)
		if existing:
			intent = client.payment_intents.retrieve(existing.request_id)
			if intent.status not in ("canceled", "succeeded") and intent.currency == currency.lower():
				if intent.amount != units and intent.status != "processing":
					intent = client.payment_intents.update(intent.id, params={"amount": units})
				return intent
			if intent.status == "succeeded":
				return intent
		intent = client.payment_intents.create(
			params={
				"amount": units,
				"currency": currency.lower(),
				"automatic_payment_methods": {"enabled": True},
				"description": reference.get("subject")
				or _("{0} {1}").format(_(reference_doctype), reference_docname),
				"receipt_email": reference.get("email_to") or None,
				"metadata": {
					"reference_doctype": reference_doctype,
					"reference_docname": reference_docname,
					"site": frappe.local.site,
				},
			},
			options={
				"idempotency_key": f"{frappe.local.site}:{reference_doctype}:{reference_docname}:{units}"
			},
		)
		create_request_log(
			{"payment_intent": intent.id, "amount": amount, "currency": currency},
			service_name="Stripe",
			reference_doctype=reference_doctype,
			reference_docname=reference_docname,
			request_id=intent.id,
		)
		return intent

	def record_payment_intent(self, intent) -> str | None:
		"""Record a PaymentIntent Stripe reports as succeeded against the document it pays, once.
		Returns where to send the payer next."""
		metadata = intent.metadata or {}
		reference_doctype, reference_docname = (
			metadata.get("reference_doctype"),
			metadata.get("reference_docname"),
		)
		if intent.status != "succeeded" or metadata.get("site") != frappe.local.site:
			return None
		name = frappe.db.get_value(
			"Integration Request",
			{"integration_request_service": "Stripe", "request_id": intent.id},
			"name",
			for_update=True,
		)
		if not name:
			return None
		request = frappe.get_doc("Integration Request", name)
		reference = frappe.get_doc(reference_doctype, reference_docname)
		if request.status == "Completed":
			# recorded already: the document only says where the payer goes next
			return reference.run_method("on_payment_authorized", "Completed")
		if intent.currency != (
			reference.get("currency") or ""
		).lower() or intent.amount_received < minor_units(
			reference.get("grand_total"), reference.get("currency")
		):
			request.db_set(
				{"status": "Failed", "error": _("The amount paid doesn't match")}, update_modified=False
			)
			frappe.log_error(title="Stripe payment doesn't match its document", message=intent.id)
			return None
		request.db_set("status", "Completed", update_modified=False)
		return reference.run_method("on_payment_authorized", "Completed")


def intent_request(reference_doctype: str, reference_docname: str):
	"""The Integration Request of the latest PaymentIntent made for a document."""
	return frappe.db.get_value(
		"Integration Request",
		{
			"integration_request_service": "Stripe",
			"reference_doctype": reference_doctype,
			"reference_docname": reference_docname,
			"request_id": ["like", "pi_%"],
		},
		["name", "request_id", "status"],
		as_dict=True,
		order_by="creation desc",
	)


# Stripe calls this; the event's signature is checked against the gateway's webhook secret.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def webhook(gateway: str | None = None):
	"""Record payments Stripe reports with payment_intent.succeeded, whether or not the payer
	came back to the site."""
	import stripe

	# Stripe posts JSON, so the gateway named in the webhook's URL is read from the URL
	gateway = gateway or frappe.request.args.get("gateway")
	if not gateway or not frappe.db.exists("Stripe Settings", gateway):
		raise frappe.PermissionError
	settings = frappe.get_doc("Stripe Settings", gateway)
	secret = settings.get_password(fieldname="webhook_secret", raise_exception=False)
	if not secret:
		raise frappe.PermissionError
	try:
		event = stripe.Webhook.construct_event(
			frappe.request.get_data(), frappe.get_request_header("Stripe-Signature"), secret
		)
	except (ValueError, stripe.SignatureVerificationError):
		raise frappe.PermissionError
	if event.type == "payment_intent.succeeded":
		# the PaymentIntent is fetched from Stripe rather than taken from the event
		intent = settings.client().payment_intents.retrieve(event.data.object.id)
		settings.record_payment_intent(intent)
		# Stripe retries until the payment is stored, and webhooks are not committed otherwise
		frappe.db.commit()  # nosemgrep
	return {"received": True}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
