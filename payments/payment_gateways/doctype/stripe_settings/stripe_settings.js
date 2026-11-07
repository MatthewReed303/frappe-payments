// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stripe Settings", {
	refresh(frm) {
		if (frm.is_new()) return;
		const url = frappe.urllib.get_full_url(
			"/api/method/payments.payment_gateways.doctype.stripe_settings.stripe_settings.webhook?gateway=" +
				encodeURIComponent(frm.doc.name)
		);
		frm.set_df_property(
			"webhook_secret",
			"description",
			__(
				"The signing secret of a Stripe webhook sending payment_intent.succeeded to {0}. Payments are then recorded even when the payer doesn't return to the site.",
				[`<code>${frappe.utils.escape_html(url)}</code>`]
			)
		);
	},
});
