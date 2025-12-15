import frappe
import requests
from frappe.utils import now_datetime, add_to_date
from frappe.utils.password import get_decrypted_password

# -----------------------------------------
# HARDCODED GOOGLE OAUTH CREDENTIALS
# -----------------------------------------
settings = frappe.get_single("Google OAuth Settings")

CLIENT_ID = settings.client_id
CLIENT_SECRET = get_decrypted_password("Google OAuth Settings", "Google OAuth Settings", "client_secret")
REDIRECT_URI = settings.redirect_url


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# SCOPES NEEDED
SCOPE = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive"


# ------------------------------------------------------------
# Generate OAuth URL for a specific user
# ------------------------------------------------------------
@frappe.whitelist()
def get_auth_url(confirra_user):
    """Generate Google Login URL for a specific Confirra User"""

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": SCOPE,
        "state": confirra_user,
    }
    
    query_string = "&".join([f"{k}={requests.utils.quote(v)}" for k, v in params.items()])
    return f"{GOOGLE_AUTH_URL}?{query_string}"


# ------------------------------------------------------------
# OAuth CALLBACK - Google redirects here
# ------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def callback(code=None, state=None):
    """Receives token from Google and saves it into the Confirra User"""

    if not code:
        return "Error: Missing authorization code"

    if not state:
        return "Error: Missing Confirra User identifier"

    data = {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    token_response = requests.post(GOOGLE_TOKEN_URL, data=data).json()

    # Debug return if token fails
    if "access_token" not in token_response:
        return f"Google Token Error: {token_response}"

    # Save tokens to Confirra User
    user = frappe.get_doc("Confirra Users", state)
    user.google_access_token = token_response.get("access_token")
    user.google_refresh_token = token_response.get("refresh_token")
    user.google_token_expiry = add_to_date(now_datetime(), seconds=token_response.get("expires_in", 3600))

    user.save(ignore_permissions=True)
    frappe.db.commit()

    return "<h3>Google Authorization Successful!</h3>"


# ------------------------------------------------------------
# Refresh token if expired
# ------------------------------------------------------------
def refresh_token(user):
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": user.google_refresh_token,
        "grant_type": "refresh_token",
    }

    response = requests.post(GOOGLE_TOKEN_URL, data=data).json()

    if "access_token" not in response:
        frappe.throw(f"Google Refresh Error: {response}")

    user.google_access_token = response.get("access_token")
    user.google_token_expiry = add_to_date(now_datetime(), seconds=response.get("expires_in", 3600))
    user.save(ignore_permissions=True)
    frappe.db.commit()

    return user.google_access_token


# ------------------------------------------------------------
# Helper: return valid token (auto-refresh)
# ------------------------------------------------------------
@frappe.whitelist()
def get_valid_token(confirra_user):
    user = frappe.get_doc("Confirra Users", confirra_user)

    if not user.google_access_token:
        frappe.throw("User has not connected Google.")

    if user.google_token_expiry <= now_datetime():
        return refresh_token(user)

    return user.google_access_token


# ------------------------------------------------------------
# Example: create Google Sheet
# ------------------------------------------------------------
@frappe.whitelist()
def create_google_sheet(confirra_user):
    token = get_valid_token(confirra_user)

    url = "https://sheets.googleapis.com/v4/spreadsheets"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    body = {"properties": {"title": f"{confirra_user} Sheet"}}

    return requests.post(url, headers=headers, json=body).json()
