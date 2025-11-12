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


def upload_to_drive(file_bytes: bytes, filename: str) -> (Optional[str], Optional[str]):
    folder_id = st.secrets.get("drive", {}).get("folder_id", "")
    if not folder_id:
        return None, None

    service = get_drive_service()

    from googleapiclient.http import MediaIoBaseUpload
    media_body = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=None, resumable=False)

    file_metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(body=file_metadata, media_body=media_body, fields="id, webViewLink").execute()
    file_id = created.get("id")
    web_view = created.get("webViewLink")

    try:
        service.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
    except Exception:
        pass

    return file_id, web_view


def append_row(ws, row: list):
    ws.append_row(row, value_input_option="USER_ENTERED")


st.set_page_config(page_title="QA Toolkit", page_icon="🧰", layout="wide")

st.title("🧰 QA Toolkit — Streamlit Cloud")

with st.sidebar:
    st.markdown("**Connected Sheet:** ")
    try:
        sh, ws = get_sheet_handles()
        st.success(f"{sh.title} → {ws.title}")
    except Exception as e:
        st.error(f"Sheet error: {e}")

    if st.secrets.get("drive", {}).get("folder_id", ""):
        st.success("Google Drive uploads enabled")
    else:
        st.info("Drive uploads disabled (no folder_id)")

    st.markdown("**Tips**\n- Share the Sheet and Drive folder with the service account email.\n- Use Batch tab for CSV paste.")


tab1, tab2 = st.tabs(["Quick QA Note", "Batch Feedback"])

with tab1:
    st.subheader("Quick QA Note")

    with st.form("qa_quick_form"):
        project = st.text_input("Project (optional)")
        task_id = st.text_input("Task ID")
        job_id = st.text_input("Job ID")
        frame = st.text_input("Frame")
        frame_url = st.text_input("Frame URL (optional)")

        worker_list = get_workers()
        worker = st.selectbox("Worker", worker_list) if worker_list else st.text_input("Worker")

        label = st.text_input("Label / Class")
        severity = st.selectbox("Severity", ["Low", "Med", "High", "Blocker"], index=1)

        issue_text = st.text_area("Issue (short, literal)")
        guideline_ref = st.text_input("Guideline reference (optional)")
        shot = st.file_uploader("Screenshot (PNG/JPG)", type=["png", "jpg", "jpeg"])

        submitted = st.form_submit_button("Log QA Item", use_container_width=True)

    if submitted:
        try:
            sh, ws = get_sheet_handles()
            link = ensure_url(frame_url) if frame_url.strip() else (build_cvat_url(task_id, job_id, frame) or "")
            drive_id, drive_url = None, None

            if shot is not None:
                file_bytes = shot.read()
                filename = f"QA_{int(time.time())}_{shot.name}"
                drive_id, drive_url = upload_to_drive(file_bytes, filename)

            row = [
                datetime.utcnow().isoformat(), project, task_id, job_id, frame, link,
                worker, label, severity, issue_text, guideline_ref, drive_id or "", drive_url or "", "open"
            ]
            append_row(ws, row)

            st.success("QA item logged.")
            if link:
                st.markdown(f"**Frame:** {link}")
            if drive_url:
                st.markdown(f"**Screenshot:** {drive_url}")
        except Exception as e:
            st.error(f"Failed to log QA item: {e}")

with tab2:
    st.subheader("Batch Feedback Generator")

    st.markdown("Paste frame URLs or upload a CSV with `frame_url, task_id, job_id, frame, worker, label, severity, issue_text, guideline_ref`. Missing fields use fallback.")

    urls_text = st.text_area("Frame URLs (one per line)", height=150)
    csv_file = st.file_uploader("CSV upload (optional)", type=["csv"])

    st.divider()
    st.markdown("**Common fields:**")
    common_project = st.text_input("Project (optional)")
    common_worker = st.text_input("Worker (fallback)")
    common_label = st.text_input("Label (fallback)")
    common_sev = st.selectbox("Severity (fallback)", ["Low", "Med", "High", "Blocker"], index=1)
    common_issue = st.text_input("Issue (fallback)")
    common_guideline = st.text_input("Guideline ref (fallback)")

    if st.button("Create Rows", use_container_width=True):
        rows = []
        if csv_file is not None:
            df = pd.read_csv(csv_file)
            for _, r in df.iterrows():
                rows.append({k: r.get(k, "") for k in ["project", "task_id", "job_id", "frame", "frame_url", "worker", "label", "severity", "issue_text", "guideline_ref"]})
        for line in urls_text.splitlines():
            u = line.strip()
            if u:
                rows.append({"project": "", "task_id": "", "job_id": "", "frame": "", "frame_url": u, "worker": "", "label": "", "severity": "", "issue_text": "", "guideline_ref": ""})

        if not rows:
            st.warning("No rows to create.")
        else:
            sh, ws = get_sheet_handles()
            created = 0
            for r in rows:
                try:
                    row = [
                        datetime.utcnow().isoformat(),
                        r.get("project") or common_project,
                        r.get("task_id"),
                        r.get("job_id"),
                        r.get("frame"),
                        ensure_url(r.get("frame_url", "")),
                        r.get("worker") or common_worker,
                        r.get("label") or common_label,
                        r.get("severity") or common_sev,
                        r.get("issue_text") or common_issue,
                        r.get("guideline_ref") or common_guideline,
                        "", "", "open"
                    ]
                    append_row(ws, row)
                    created += 1
                except Exception as e:
                    st.error(f"Row failed: {e}")
            st.success(f"Created {created} rows.")
