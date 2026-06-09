# NiftyBot Dashboard — Deployment Guide

One-time setup, roughly 20 minutes. After this the dashboard runs forever at a
live URL, re-reading the Google Sheet every 60 seconds. No more file uploads.

---

## Part 1 — Google service account (one time, ~8 min)

You may already have `credentials.json` on Drive at
`/MyDrive/NiftyBot/credentials.json` from the Archiver work. If that file is a
**service account** key (it contains `"type": "service_account"` and a
`client_email` ending in `.iam.gserviceaccount.com`), skip to step 5.
If it's an OAuth client file, create a service account fresh:

1. Go to https://console.cloud.google.com → select your NiftyBot project
   (or create one).
2. **APIs & Services → Library** → search **Google Drive API** → Enable.
3. **IAM & Admin → Service Accounts → Create Service Account.** Name it
   `niftybot-dashboard`. No roles needed. Create.
4. Open the new service account → **Keys → Add key → Create new key → JSON.**
   A JSON file downloads. Keep it private — it's a password.
5. **Critical step:** open the JSON, copy the `client_email` value
   (looks like `niftybot-dashboard@yourproject.iam.gserviceaccount.com`).
   Open the NiftyBot Google Sheet → **Share** → paste that email →
   **Viewer** → Send. Without this the dashboard gets a 404.

## Part 2 — GitHub repo (~5 min)

1. Create a free account at https://github.com if you don't have one.
2. New repository → name it `niftybot-dashboard` → **Private** → Create.
3. Upload these three files (Add file → Upload files):
   - `dashboard.py`
   - `requirements.txt`
   - this `README_DEPLOY.md` (optional)

## Part 3 — Streamlit Cloud (~5 min)

1. Go to https://share.streamlit.io → **Sign in with GitHub.**
2. **New app** → pick the `niftybot-dashboard` repo → branch `main` →
   main file `dashboard.py`.
3. Before deploying, open **Advanced settings → Secrets** and paste the
   service-account JSON in TOML form, like this (copy each value from the
   downloaded JSON — keep the triple quotes around the private key):

```toml
[gcp_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "xxxxxxxxxxxxxxxx"
private_key = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBg...full key, keep the line breaks...
-----END PRIVATE KEY-----
"""
client_email = "niftybot-dashboard@yourproject.iam.gserviceaccount.com"
client_id = "123456789012345678901"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/niftybot-dashboard%40yourproject.iam.gserviceaccount.com"
```

4. Click **Deploy.** First build takes 2–3 minutes. You get a permanent URL
   like `https://niftybot-dashboard.streamlit.app`.

## Daily use

- Nothing. The bot writes to the sheet; the dashboard re-reads it every 60 s
  (`st.cache_data(ttl=60)`). The sidebar has a **Force refresh** button.
- The app sleeps after ~7 days without visits on the free tier; first visit
  after sleep takes ~30 s to wake. Data is never lost — it lives in the sheet.

## Updating the dashboard later

Edit `dashboard.py` in the GitHub repo (the pencil icon works fine in the
browser) → commit → Streamlit redeploys automatically in ~1 minute.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Google service-account secret not found" | Secrets TOML missing or section not named `[gcp_service_account]` |
| `HttpError 404` reading the sheet | Sheet not shared with the `client_email` (Part 1, step 5) |
| `invalid_grant` / JWT error | `private_key` pasted without line breaks — use the triple-quoted form above |
| Tabs empty / wrong columns | Tab or column names changed in the sheet — the code matches lowercase names from the handover schema |
