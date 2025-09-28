# src/ingestion/webhooks.py
from flask import Blueprint, request, jsonify, current_app

webhooks_bp = Blueprint("webhooks", __name__)

@webhooks_bp.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    current_app.logger.info("Plaid webhook: %s", payload)

    wt = payload.get("webhook_type")
    wc = payload.get("webhook_code")

    # Example: react to new/updated transactions
    if wt == "TRANSACTIONS" and wc in ("DEFAULT_UPDATE", "INITIAL_UPDATE", "HISTORICAL_UPDATE"):
        item_id = payload.get("item_id")
        new_tx = payload.get("new_transactions", 0)
        current_app.logger.info("Transactions update for item %s (new: %s)", item_id, new_tx)
        # TODO: call your fetch/save pipeline here (e.g., pull latest transactions and persist)

    return jsonify({"ok": True}), 200
