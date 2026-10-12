"""Send a locally signed example lead to the running service."""
import hashlib
import hmac
import json
import os
import requests

BASE_URL = os.getenv("LEADS_BASE_URL", "http://localhost:8000")
SECRET = os.environ["LEAD_SHARED_SECRET"]
payload = {
    "id": "partner-demo-001",
    "name": "Taylor Morgan",
    "email": "taylor@example.com",
    "company": "Example Co",
    "source": "partner-website",
}
body = json.dumps(payload, separators=(",", ":")).encode()
signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
response = requests.post(
    BASE_URL + "/api/leads",
    data=body,
    headers={"Content-Type": "application/json", "X-Lead-Signature": "sha256=" + signature},
    timeout=10,
)
print("HTTP", response.status_code)
print(json.dumps(response.json(), indent=2))
response.raise_for_status()
