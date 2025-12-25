import os
import json
import plaid
from plaid.api import plaid_api
from plaid.exceptions import ApiException
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.accounts_get_request import AccountsGetRequest

# Put your 6 candidates in a JSON file that you do NOT commit
# Example file: C:\DATA\personal_finance\config\env\token_candidates.json
# [
#   {"label":"candidate1","item_id":"...","access_token":"..."},
#   ...
# ]

CANDIDATES_PATH = r"C:\DATA\personal_finance\config\env\token_candidates.json"

ENV_MAP = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

def make_client():
    plaid_env = (os.getenv("PLAID_ENV") or "sandbox").lower()
    host = ENV_MAP.get(plaid_env, ENV_MAP["sandbox"])
    cfg = plaid.Configuration(
        host=host,
        api_key={"clientId": os.getenv("PLAID_CLIENT_ID"), "secret": os.getenv("PLAID_SECRET")},
    )
    api_client = plaid.ApiClient(cfg)
    return plaid_api.PlaidApi(api_client), plaid_env

def main():
    client, plaid_env = make_client()
    with open(CANDIDATES_PATH, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    print(f"PLAID_ENV={plaid_env} candidates={len(candidates)}")
    for c in candidates:
        label = c.get("label", "")
        item_id = c.get("item_id")
        access_token = c.get("access_token")

        print("\n---")
        print(f"{label} item_id={item_id}")

        try:
            # 1) ItemGet is a good validation call
            item_res = client.item_get(ItemGetRequest(access_token=access_token)).to_dict()
            inst_id = item_res.get("institution_id")
            print(f"OK item_get institution_id={inst_id}")

            # 2) accounts_get proves token usable for your workflow
            acc_res = client.accounts_get(AccountsGetRequest(access_token=access_token)).to_dict()
            accounts = acc_res.get("accounts", []) or []
            print(f"OK accounts_get accounts={len(accounts)}")

        except ApiException as e:
            print(f"FAIL ApiException: {e}")
        except Exception as e:
            print(f"FAIL Exception: {e}")

if __name__ == "__main__":
    main()