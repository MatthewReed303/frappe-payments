# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.utils import (
	call_hook_method,
	cint,
	convert_utc_to_system_timezone,
	flt,
	fmt_money,
	format_datetime,
	get_url,
	validate_email_address,
)

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


def major_units(units: int, currency: str) -> float:
	"""An amount Stripe gives in the currency's smallest unit, in the currency."""
	currency = currency.upper()
	if currency in ZERO_DECIMAL_CURRENCIES:
		return flt(units)
	return flt(units) / (1000 if currency in THREE_DECIMAL_CURRENCIES else 100)


def key_mode(key: str | None) -> str | None:
	"""Whether a Stripe API key is a test or a live key: Stripe's test mode and sandboxes use the same
	API with their own keys."""
	for mode in ("test", "live"):
		if (key or "").startswith((f"pk_{mode}_", f"sk_{mode}_", f"rk_{mode}_")):
			return mode
	return None


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

	def validate(self):
		secret = self.get_password(fieldname="secret_key", raise_exception=False)
		modes = key_mode(self.publishable_key), key_mode(secret)
		if all(modes) and modes[0] != modes[1]:
			frappe.throw(
				_(
					"The Publishable Key is a {0} key and the Secret Key a {1} key. Use both from the same mode."
				).format(_(modes[0]), _(modes[1]))
			)

	@property
	def test_mode(self) -> bool:
		return key_mode(self.publishable_key) == "test"

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
		if self.data.reference_doctype and self.data.reference_docname:
			reference = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
			if reference.meta.has_field("grand_total"):
				if reference.doctype != "Payment Request":
					# only a Payment Request records what is paid for a document with a total
					frappe.throw(
						_("Pay {0} {1} through its Payment Request").format(
							_(reference.doctype), reference.name
						)
					)
				# what the request asks, never what the browser sent
				self.data.amount, self.data.currency = amount_to_pay(reference)
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
				amount=minor_units(self.data.amount, self.data.currency),
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
		"""The PaymentIntent paying a document (usually a Payment Request), for what it still asks in
		its currency. It is made once and reused while the document is unpaid."""
		reference = frappe.get_doc(reference_doctype, reference_docname)
		client = self.client()
		existing = intent_request(reference_doctype, reference_docname)
		intent = client.payment_intents.retrieve(existing.request_id) if existing else None
		if intent and intent.status == "succeeded":
			return intent
		amount, currency = amount_to_pay(reference)
		self.validate_transaction_currency(currency)
		self.validate_minimum_transaction_amount(currency, amount)
		units = minor_units(amount, currency)
		if intent and intent.status != "canceled" and intent.currency == currency.lower():
			if intent.amount != units and intent.status != "processing":
				intent = client.payment_intents.update(intent.id, params={"amount": units})
			return intent
		attempt = frappe.db.count(
			"Integration Request",
			{
				"integration_request_service": "Stripe",
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"request_id": ["like", "pi_%"],
			},
		)
		intent = client.payment_intents.create(
			params={
				"amount": units,
				"currency": currency.lower(),
				"automatic_payment_methods": {"enabled": True},
				"description": reference.get("subject")
				or _("{0} {1}").format(_(reference_doctype), reference_docname),
				# a request made without the customer's email holds the user who made it
				"receipt_email": validate_email_address(reference.get("email_to") or "") or None,
				"metadata": {
					"reference_doctype": reference_doctype,
					"reference_docname": reference_docname,
					"site": frappe.local.site,
				},
			},
			options={
				"idempotency_key": f"{frappe.local.site}:{reference_doctype}:{reference_docname}:{units}:{attempt}"
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
		# the status is read with the lock: a plain read can see the moment before the webhook or the
		# payer's return recorded the same payment
		locked = frappe.db.get_value(
			"Integration Request",
			{"integration_request_service": "Stripe", "request_id": intent.id},
			["name", "status"],
			as_dict=True,
			for_update=True,
		)
		if not locked:
			return None
		request = frappe.get_doc("Integration Request", locked.name)
		request.status = locked.status
		reference = frappe.get_doc(reference_doctype, reference_docname)
		if reference.docstatus == 2 and request.status != "Completed":
			# paid in the moment the document was cancelled: kept for staff to refund or record
			request.db_set(
				{
					"status": "Failed",
					"error": _("Paid after {0} {1} was cancelled. Refund it in Stripe, or record it.").format(
						_(reference_doctype), reference_docname
					),
				},
				update_modified=False,
			)
			frappe.log_error(title="Stripe payment for a cancelled document", message=intent.id)
			note = _(
				"A card payment of {0} came in after {1} {2} was cancelled. Refund it in Stripe, or record it."
			).format(
				fmt_money(
					major_units(intent.amount_received, intent.currency), currency=intent.currency.upper()
				),
				_(reference_doctype),
				reference_docname,
			)
			tell_staff(reference, note)
			return frappe.redirect_to_message(
				_("Payment received after cancellation"),
				_(
					"Your payment came in after this was cancelled. We've told our team, and they'll refund it."
				),
			)
		if request.status == "Completed":
			# recorded already: the document only says where the payer goes next
			return reference.run_method("on_payment_authorized", "Completed")
		# intents are only made here, for what the document still asked, so the payment must be the
		# intent's whole amount, no more than the document's total
		if (
			intent.currency != (reference.get("currency") or "").lower()
			or intent.amount_received < intent.amount
			or intent.amount > minor_units(reference.get("grand_total"), reference.get("currency"))
		):
			request.db_set(
				{"status": "Failed", "error": _("The amount paid doesn't match")}, update_modified=False
			)
			frappe.log_error(title="Stripe payment doesn't match its document", message=intent.id)
			return None
		request.db_set("status", "Completed", update_modified=False)
		return reference.run_method("on_payment_authorized", "Completed")

	def refund(self, reference_doctype: str, reference_docname: str, amount: float, key: str) -> str:
		"""Refund part or all of what a document's PaymentIntent took, once per key (e.g. the credit
		note it is for). Returns the Stripe Refund id."""
		request = intent_request(reference_doctype, reference_docname)
		if not request or request.status != "Completed":
			frappe.throw(
				_("{0} {1} has no card payment to refund").format(_(reference_doctype), reference_docname)
			)
		currency = frappe.db.get_value(reference_doctype, reference_docname, "currency")
		units = minor_units(amount, currency)
		client = self.client()
		# Stripe forgets idempotency keys after a day: a refund made for this key before is reused
		for made in client.refunds.list(params={"payment_intent": request.request_id, "limit": 100}).data:
			if (made.metadata or {}).get("key") == key and made.status != "failed":
				return made.id
		refund = client.refunds.create(
			params={
				"payment_intent": request.request_id,
				"amount": units,
				"metadata": {
					"reference_doctype": reference_doctype,
					"reference_docname": reference_docname,
					"key": key,
				},
			},
			options={"idempotency_key": f"{frappe.local.site}:refund:{request.request_id}:{key}:{units}"},
		)
		create_request_log(
			{
				"refund": refund.id,
				"payment_intent": request.request_id,
				"amount": amount,
				"currency": currency,
			},
			service_name="Stripe",
			# logged against the payment it refunds: ERPNext refuses new requests for a paid Payment Request
			reference_doctype="Integration Request",
			reference_docname=request.name,
			request_id=refund.id,
		)
		return refund.id


def cancel_open_intent(doc, method=None):
	"""A cancelled document can't be paid: its PaymentIntent is cancelled at Stripe once the
	cancel is saved."""
	request = intent_request(doc.doctype, doc.name)
	if request and request.status not in ("Completed", "Cancelled"):
		frappe.enqueue(
			"payments.payment_gateways.doctype.stripe_settings.stripe_settings.cancel_intent",
			integration_request=request.name,
			enqueue_after_commit=True,
		)


def cancel_intent(integration_request: str) -> None:
	request = frappe.get_doc("Integration Request", integration_request)
	gateway = frappe.db.get_value(request.reference_doctype, request.reference_docname, "payment_gateway")
	controller = frappe.db.get_value("Payment Gateway", gateway, "gateway_controller") if gateway else None
	if not controller or not frappe.db.exists("Stripe Settings", controller):
		return
	client = frappe.get_doc("Stripe Settings", controller).client()
	intent = client.payment_intents.retrieve(request.request_id)
	if intent.status in ("canceled", "succeeded"):
		return
	client.payment_intents.cancel(intent.id)
	request.db_set("status", "Cancelled", update_modified=False)


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
	elif event.type in ("charge.dispute.created", "charge.dispute.closed"):
		record_dispute(settings.client().disputes.retrieve(event.data.object.id))
		frappe.db.commit()  # nosemgrep
	return {"received": True}


def record_dispute(dispute) -> None:
	"""Tell staff about a payment the customer disputed with their bank (a chargeback), on the
	documents it paid. Disputes are answered in Stripe; what was lost is recorded by staff."""
	paid = frappe.db.get_value(
		"Integration Request",
		{"integration_request_service": "Stripe", "request_id": dispute.payment_intent},
		["reference_doctype", "reference_docname"],
		as_dict=True,
	)
	if not paid or not frappe.db.exists(paid.reference_doctype, paid.reference_docname):
		return
	amount = fmt_money(major_units(dispute.amount, dispute.currency), currency=dispute.currency.upper())
	reason = (dispute.reason or "general").replace("_", " ")
	if dispute.status in ("won", "warning_closed"):
		note = _("The dispute over {0} was closed in your favour: the payment stands.").format(amount)
	elif dispute.status == "lost":
		note = _(
			"The customer won their dispute over {0}: Stripe has taken the money back, with its dispute fee. Record the loss in ERPNext, and don't refund it again."
		).format(amount)
	else:
		due = (dispute.get("evidence_details") or {}).get("due_by")
		note = _(
			"The customer disputed {0} with their bank ({1}). Answer it in Stripe by {2}, or the money goes back to them."
		).format(
			amount,
			reason,
			format_datetime(
				convert_utc_to_system_timezone(datetime.fromtimestamp(due, UTC).replace(tzinfo=None))
			)
			if due
			else _("the date Stripe gives"),
		)
	tell_staff(frappe.get_doc(paid.reference_doctype, paid.reference_docname), note)


def amount_to_pay(reference) -> tuple[float, str]:
	"""What a document still asks to be paid, in its currency."""
	if reference.docstatus != 1 or reference.get("status") in ("Paid", "Cancelled"):
		frappe.throw(_("{0} {1} has nothing left to pay").format(_(reference.doctype), reference.name))
	amount = reference.get("grand_total")
	if reference.meta.has_field("outstanding_amount") and reference.get("outstanding_amount") is not None:
		amount = reference.outstanding_amount
	if flt(amount) <= 0:
		frappe.throw(_("{0} {1} has nothing left to pay").format(_(reference.doctype), reference.name))
	return flt(amount), reference.get("currency")


def tell_staff(reference, note: str) -> None:
	"""Note something about a payment on its document (and the document that asks for it), and
	notify Accounts Managers."""
	documents = [reference]
	if reference.get("reference_doctype") and reference.get("reference_name"):
		documents.append(frappe.get_doc(reference.reference_doctype, reference.reference_name))
	for doc in documents:
		if not frappe.db.exists(
			"Comment", {"reference_doctype": doc.doctype, "reference_name": doc.name, "content": note}
		):
			doc.add_comment("Info", note)
	staff = frappe.get_all(
		"Has Role",
		filters={
			"role": "Accounts Manager",
			"parenttype": "User",
			"parent": ["not in", ["Administrator", "Guest"]],
		},
		pluck="parent",
		distinct=True,
	)
	staff = [user for user in staff if frappe.db.get_value("User", user, "enabled")] or ["Administrator"]
	enqueue_create_notification(
		staff,
		{
			"type": "Alert",
			"document_type": documents[-1].doctype,
			"document_name": documents[-1].name,
			"subject": note,
			"from_user": "Administrator",
		},
	)


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
