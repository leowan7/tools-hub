"""Billing layer for the Ranomics tools hub.

Stripe price -> tier + credit grant mapping lives in ``tiers.py``. The
webhook handler under ``tools_hub.webhooks.stripe`` consumes this module
when a Stripe event lands.
"""
