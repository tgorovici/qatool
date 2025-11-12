# 📦 Streamlit Cloud QA Toolkit

This repo provides a ready-to-deploy Streamlit app with two production-ready utilities:

1) **Quick QA Note** — paste a CVAT frame URL, add a short description, attach a screenshot, pick a worker, and log to Google Sheets in seconds (optionally uploads the screenshot to Google Drive).
2) **Batch Feedback** — paste a list of frame URLs or upload a CSV and bulk‑create QA rows to your Google Sheet, with per‑row or common notes.

It’s optimized for Streamlit Community Cloud (Secrets + Service Account). Minimal setup: share the Sheet (and optional Drive folder) with your service account email, set `secrets.toml`, deploy.

---

## 🗂 Repo Structure

```
.
├─ app.py
├─ requirements.txt
└─ .streamlit/
   └─ secrets.toml  (not committed; set in Streamlit Cloud UI)
```

---

## 🔐 Secrets (Streamlit Cloud)
Create these in **Streamlit Cloud → App → Settings → Secrets**. Example template:

```toml
# .streamlit/secrets.toml

# Google Service Account JSON (paste full JSON as a multiline TOML string)
[gcp_service_account]
# contents of your service_account.json go here as key/value pairs, e.g.:
# type = "service_account"
# project_id = "..."
# private_key_id = "..."
# private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
# client_email = "...@...gserviceaccount.com"
# ...

# Google Sheets configuration
[sheets]
spreadsheet_name = "QA_Log_Master"        # will be created if missing
worksheet_name   = "QA_Log"               # main log worksheet name

# Optional: list of workers for the dropdown. If not set, app will fallback to a text input.
workers = ["Aaron", "Dean", "Ido"]

# Optional: Google Drive folder for screenshots (share with service account)
[drive]
folder_id = ""  # e.g., "1AbCDefGhIJklmNOP...". Leave empty to disable uploads.

# Optional: build CVAT deep links if user supplies task/job/frame instead of URL
[cvat]
base_url = "https://your-cvat.example.com"  # no trailing slash; optional
```

> **Important:** Share your Google Sheet and (optionally) your Drive **folder** with the service account email (found in the JSON `client_email`). If you use a Shared Drive, also add the service account to that drive.

---

## 📦 requirements.txt

```
streamlit==1.39.0
gspread==6.1.4
google-auth==2.35.0
google-auth-oauthlib==1.2.1
google-api-python-client==2.151.0
pandas==2.2.3
python-dateutil==2.9.0.post0
```

---

## 🚀 app.py

```python
import io
import time
from datetime import datetime, date
from typing import Optional, List

import pandas as pd
import streamlit as st

import google.auth
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

# -----------------------------
# Auth helpers
# -----------------------------

def get_credentials() -> Credentials:
    """Build service-account Credentials from st.secrets.gcp_service_account."""
    info = dict(st.secrets["gcp_service_account"])  # type: ignore
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    ]
    return Credentials.from_service_account_info(info, scopes=scopes)


@st.cache_resource(show_spinner=False)
def get_gspread_client() -> gspread.Client:
    creds = get_credentials()
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def get_drive_service():
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


# -----------------------------
# Config helpers
# -----------------------------

def get_workers() -> List[str]:
    try:
        workers = list(st.secrets.get("sheets", {}).get("workers", []))  # type: ignore
        return workers
    except Exception:
        return []


def get_sheet_handles():
    gc = get_gspread_client()
    ss_name = st.secrets["sheets"]["spreadsheet_name"]
    ws_name = st.secrets["sheets"]["worksheet_name"]
    try:
        sh = gc.open(ss_name)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(ss_name)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=30)
        # set header
        ws.update("A1:N1", [[
            "timestamp", "project", "task_id", "job_id", "frame",
            "frame_url", "worker", "label", "severity", "issue_text",
            "guideline_ref", "attachment_drive_id", "attachment_view_url", "status"
        ]])
    return sh, ws


def ensure_url(val: str) -> str:
    return val.strip()


def build_cvat_url(task_id: str, job_id: str, frame: str) -> Optional[str]:
    base = st.secrets.get("cvat", {}).get("base_url", "")
    if not base or not task_id or not job_id or not frame:
        return None
    return f"{base}/tasks/{task_id}/jobs/{job_id}?frame={frame}"


# -----------------------------
# Drive upload
# -----------------------------

def upload_to_drive(file_bytes: bytes, filename: str) -> (Optional[str], Optional[str]):
    folder_id = st.secrets.get("drive", {}).get("folder_id", "")
    if not folder_id:
        return None, None  # uploads disabled

    service = get_drive_service()

    media = {"mimeType": None}  # handled by Drive per extension; we’ll rely on filename
    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }

    # Use media upload via googleapiclient (simple upload)
    from googleapiclient.http import MediaIoBaseUpload
    media_body = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=None, resumable=False)

    created = service.files().create(body=file_metadata, media_body=media_body, fields="id, webViewLink").execute()
    file_id = created.get("id")
    web_view = created.get("webViewLink")

    # Make sure it’s at least accessible to link viewers (optional; requires Drive policy)
    try:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
        ).execute()
    except Exception:
        pass  # ignore if org policy forbids public links

    return file_id, web_view


# -----------------------------
# Row append
# -----------------------------

def append_row(ws, row: list):
    ws.append_row(row, value_input_option="USER_ENTERED")


# -----------------------------
# UI
# -----------------------------

st.set_page_config(page_title="QA Toolkit", page_icon="🧰", layout="wide")

st.title("🧰 QA Toolkit — Streamlit Cloud")
with st.sidebar:
    st.markdown("**Connected Sheet:** ")
    try:
        sh, ws = get_sheet_handles()
        st.success(f"{sh.title} → {ws.title}")
    except Exception as e:
        st.error(f"Sheet error: {e}")

    workers = get_workers()
    st.markdown("**Screenshot uploads:** ")
    if st.secrets.get("drive", {}).get("folder_id", ""):
        st.success("Google Drive uploads enabled")
    else:
        st.info("Drive uploads disabled (no folder_id)")

    st.markdown("**Tips**\n- Share the Sheet and Drive folder with the service account email.\n- Use Batch tab for CSV paste.")


tab1, tab2 = st.tabs(["Quick QA Note", "Batch Feedback"])  # could add Dashboard later

# -----------------------------
# Tab 1 — Quick QA Note
# -----------------------------
with tab1:
    st.subheader("Quick QA Note")
    colA, colB = st.columns(2)

    with st.form("qa_quick_form"):
        project = st.text_input("Project (optional)")
        t1, t2, t3 = st.columns(3)
        with t1:
            task_id = st.text_input("Task ID", placeholder="e.g., 6057")
        with t2:
            job_id = st.text_input("Job ID", placeholder="e.g., 102345")
        with t3:
            frame = st.text_input("Frame", placeholder="e.g., 123")

        frame_url = st.text_input("Frame URL (if provided, overrides built link)")

        worker_list = get_workers()
        if worker_list:
            worker = st.selectbox("Worker", worker_list, index=0)
        else:
            worker = st.text_input("Worker")

        label = st.text_input("Label / Class", placeholder="e.g., Balcony")
        severity = st.selectbox("Severity", ["Low", "Med", "High", "Blocker"], index=1)

        issue_text = st.text_area("Issue (short, literal)\n", placeholder="e.g., Box too wide; include window frame")
        guideline_ref = st.text_input("Guideline reference (optional)", placeholder="e.g., G-12 Balcony vs Window")

        shot = st.file_uploader("Screenshot (PNG/JPG)", type=["png", "jpg", "jpeg"])

        submitted = st.form_submit_button("Log QA Item", use_container_width=True)

    if submitted:
        try:
            sh, ws = get_sheet_handles()
            # decide frame link
            link = ensure_url(frame_url) if frame_url.strip() else (build_cvat_url(task_id, job_id, frame) or "")

            drive_id, drive_url = None, None
            if shot is not None:
                file_bytes = shot.read()
                filename = f"QA_{int(time.time())}_{shot.name}"
                drive_id, drive_url = upload_to_drive(file_bytes, filename)

            row = [
                datetime.utcnow().isoformat(),
                project,
                task_id,
                job_id,
                frame,
                link,
                worker,
                label,
                severity,
                issue_text,
                guideline_ref,
                drive_id or "",
                drive_url or "",
                "open",
            ]
            append_row(ws, row)

            st.success("QA item logged.")
            if link:
                st.markdown(f"**Frame:** {link}")
            if drive_url:
                st.markdown(f"**Screenshot:** {drive_url}")
        except Exception as e:
            st.error(f"Failed to log QA item: {e}")


# -----------------------------
# Tab 2 — Batch Feedback
# -----------------------------
with tab2:
    st.subheader("Batch Feedback Generator")

    st.markdown("Paste frame URLs (one per line) **or** upload a CSV with columns: `frame_url, task_id, job_id, frame, worker, label, severity, issue_text, guideline_ref`. Missing fields will be filled from the sidebar/common form.")

    urls_text = st.text_area("Frame URLs (one per line)", height=150)
    csv_file = st.file_uploader("CSV upload (optional)", type=["csv"])  # schema flexible

    st.divider()
    st.markdown("**Common fields (applied when missing per row):**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        common_project = st.text_input("Project (optional)")
    with c2:
        common_worker_list = get_workers()
        if common_worker_list:
            common_worker = st.selectbox("Worker (fallback)", common_worker_list, index=0)
        else:
            common_worker = st.text_input("Worker (fallback)")
    with c3:
        common_label = st.text_input("Label (fallback)")
    with c4:
        common_sev = st.selectbox("Severity (fallback)", ["Low", "Med", "High", "Blocker"], index=1)

    common_issue = st.text_input("Issue (fallback)", placeholder="Short note applied when per-row missing")
    common_guideline = st.text_input("Guideline ref (fallback)")

    if st.button("Create Rows", use_container_width=True):
        # Build dataframe from inputs
        rows = []
        # From CSV
        if csv_file is not None:
            try:
                df = pd.read_csv(csv_file)
            except Exception:
                csv_file.seek(0)
                df = pd.read_csv(csv_file, encoding_errors="ignore")
            for _, r in df.iterrows():
                rows.append({k: r.get(k, "") for k in [
                    "project","task_id","job_id","frame","frame_url",
                    "worker","label","severity","issue_text","guideline_ref"
                ]})
        # From text area
        for line in urls_text.splitlines():
            u = line.strip()
            if not u:
                continue
            rows.append({
                "project": "",
                "task_id": "",
                "job_id": "",
                "frame": "",
                "frame_url": u,
                "worker": "",
                "label": "",
                "severity": "",
                "issue_text": "",
                "guideline_ref": "",
            })

        if not rows:
            st.warning("No rows to create. Paste URLs or upload a CSV.")
        else:
            created = 0
            errors = 0
            sh, ws = get_sheet_handles()
            for r in rows:
                # fill fallbacks
                project = r.get("project") or common_project
                task_id = str(r.get("task_id") or "")
                job_id  = str(r.get("job_id") or "")
                frame   = str(r.get("frame") or "")
                frame_url = ensure_url(str(r.get("frame_url") or ""))
                worker  = r.get("worker") or common_worker
                label   = r.get("label") or common_label
                severity = r.get("severity") or common_sev
                issue_text = r.get("issue_text") or common_issue
                guideline_ref = r.get("guideline_ref") or common_guideline

                link = frame_url if frame_url else (build_cvat_url(task_id, job_id, frame) or "")

                try:
                    append_row(ws, [
                        datetime.utcnow().isoformat(),
                        project,
                        task_id,
                        job_id,
                        frame,
                        link,
                        worker,
                        label,
                        severity,
                        issue_text,
                        guideline_ref,
                        "",
                        "",
                        "open",
                    ])
                    created += 1
                except Exception as e:
                    errors += 1
                    st.error(f"Row failed: {e}\n→ {r}")

            st.success(f"Created {created} rows. {errors} errors.")
            st.toast(f"Batch done: {created} created", icon="✅")
```

---

## 🧪 How to Deploy on Streamlit Community Cloud

1. **Create the Google Sheet** (or let the app create it):
   - Name it as in secrets: `QA_Log_Master` (or your choice).
   - Add the service account **client_email** as an editor.
   - If screenshot uploads are enabled, also share your **Drive folder** with the same service account.

2. **Push this repo to GitHub.**

3. **Create the app on Streamlit Cloud:**
   - New app → select your repo/branch → main file `app.py`.
   - In **App → Settings → Secrets**, paste the `secrets.toml` content (edited for your keys).

4. **Run.** The sidebar should show a green connection to your Sheet. Try logging a QA item.

---

## 📄 Sheet Schema (created automatically)

`QA_Log` header:
```
[timestamp, project, task_id, job_id, frame, frame_url, worker, label, severity, issue_text, guideline_ref, attachment_drive_id, attachment_view_url, status]
```

> You can add filters/conditional formatting in the Sheet (e.g., color by severity, pivot by worker).

---

## ✅ Optional Enhancements (drop-in)
- **Validation rules**: add a small panel that enforces `severity` ∈ {Low, Med, High, Blocker} and non-empty `issue_text`.
- **Worker Source**: load workers from a `Workers` worksheet instead of secrets.
- **Status toggle**: add a simple page that lists open issues with a “Close” button to update status in place.
- **Slack webhook**: post a message when `severity` is High/Blocker.
- **Client PDF**: generate a one-page daily QA summary using `st.download_button`.

---

## 🆘 Troubleshooting
- **PERMISSION_DENIED**: The Sheet or Drive folder is not shared with the service account email.
- **File upload works but link 403**: Your domain forbids `anyone` links; remove the permission call or use org-sharing.
- **Streamlit secrets parsing**: Ensure `private_key` lines in secrets are properly escaped with `\n`.

---

Happy QA’ing! 🧰
