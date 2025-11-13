import io
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st


# ============================================================
# Geometry helpers
# ============================================================

@dataclass
class Box:
    frame: int
    xtl: float
    ytl: float
    xbr: float
    ybr: float
    outside: int
    occluded: int
    attributes: Dict[str, Optional[str]] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return max(0.0, self.xbr - self.xtl)

    @property
    def height(self) -> float:
        return max(0.0, self.ybr - self.ytl)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self):
        return ((self.xtl + self.xbr) / 2.0, (self.ytl + self.ybr) / 2.0)


@dataclass
class Track:
    track_id: int
    label: str
    boxes: List[Box]


def compute_iou(b1: Box, b2: Box) -> float:
    x_left = max(b1.xtl, b2.xtl)
    y_top = max(b1.ytl, b2.ytl)
    x_right = min(b1.xbr, b2.xbr)
    y_bottom = min(b1.ybr, b2.ybr)

    if x_right <= x_left or y_bottom <= y_top:
        return 0.0

    inter_area = (x_right - x_left) * (y_bottom - y_top)
    area1 = b1.area
    area2 = b2.area
    if area1 <= 0 or area2 <= 0:
        return 0.0

    union = area1 + area2 - inter_area
    if union <= 0:
        return 0.0

    return inter_area / union


def clamp01(x: float) -> float:
    if math.isnan(x):
        return 0.0
    return max(0.0, min(1.0, x))


# ============================================================
# Parse CVAT video XML
# ============================================================

def parse_cvat_video_xml(file_obj) -> List[Track]:
    """Parse CVAT video XML (1.1) with <track><box> into Track objects."""
    if isinstance(file_obj, (bytes, bytearray)):
        f = io.BytesIO(file_obj)
    else:
        f = file_obj

    tree = ET.parse(f)
    root = tree.getroot()

    tracks: List[Track] = []

    for track_el in root.findall("track"):
        track_id = int(track_el.get("id", -1))
        label = track_el.get("label", "")

        boxes: List[Box] = []
        for box_el in track_el.findall("box"):
            try:
                frame = int(box_el.get("frame"))
                xtl = float(box_el.get("xtl"))
                ytl = float(box_el.get("ytl"))
                xbr = float(box_el.get("xbr"))
                ybr = float(box_el.get("ybr"))
            except (TypeError, ValueError):
                # skip invalid box
                continue

            outside = int(box_el.get("outside", "0"))
            occluded = int(box_el.get("occluded", "0"))

            attrs: Dict[str, Optional[str]] = {}
            for attr_el in box_el.findall("attribute"):
                name = attr_el.get("name") or ""
                value = (attr_el.text or "").strip()
                if value == "":
                    value = None
                attrs[name] = value

            boxes.append(
                Box(
                    frame=frame,
                    xtl=xtl,
                    ytl=ytl,
                    xbr=xbr,
                    ybr=ybr,
                    outside=outside,
                    occluded=occluded,
                    attributes=attrs,
                )
            )

        boxes.sort(key=lambda b: b.frame)
        tracks.append(Track(track_id=track_id, label=label, boxes=boxes))

    return tracks


# ============================================================
# Metrics per track
# ============================================================

def compute_track_metrics(
    track: Track,
    iou_consistency_threshold: float,
    ignore_outside: bool = True,
) -> Dict:
    boxes_all = track.boxes
    if ignore_outside:
        boxes_geom = [b for b in boxes_all if b.outside == 0]
    else:
        boxes_geom = boxes_all

    num_boxes_total = len(boxes_all)
    num_boxes_geom = len(boxes_geom)

    # Basic frame range
    first_frame = boxes_all[0].frame if boxes_all else None
    last_frame = boxes_all[-1].frame if boxes_all else None
    num_frames_span = (last_frame - first_frame + 1) if first_frame is not None else 0

    # 1) IoU / box consistency
    ious = []
    area_changes = []
    ar_list = []
    center_moves = []
    frame_gaps = 0

    if num_boxes_geom >= 2:
        for b1, b2 in zip(boxes_geom[:-1], boxes_geom[1:]):
            if b1.frame == b2.frame:
                continue

            # IoU
            iou = compute_iou(b1, b2)
            ious.append(iou)

            # Area change
            if b1.area > 0:
                rel_area_change = abs(b2.area - b1.area) / b1.area
                area_changes.append(rel_area_change)

            # Aspect ratio
            if b1.height > 0 and b2.height > 0:
                ar1 = b1.width / b1.height
                ar2 = b2.width / b2.height
                ar_list.extend([ar1, ar2])

            # Center movement
            c1x, c1y = b1.center
            c2x, c2y = b2.center
            dist = math.sqrt((c2x - c1x) ** 2 + (c2y - c1y) ** 2)
            center_moves.append(dist)

            # Frame gaps (continuity)
            gap = b2.frame - b1.frame
            if gap > 1:
                frame_gaps += gap - 1

    # IoU stats
    if len(ious) > 0:
        avg_iou = sum(ious) / len(ious)
        min_iou = min(ious)
        max_iou = max(ious)
        consistency_ratio = sum(1 for x in ious if x >= iou_consistency_threshold) / len(ious)
    else:
        avg_iou = math.nan
        min_iou = math.nan
        max_iou = math.nan
        consistency_ratio = math.nan

    # Size jitter
    if len(area_changes) > 0:
        mean_area_jitter = sum(area_changes) / len(area_changes)  # 0..∞
    else:
        mean_area_jitter = math.nan

    # Aspect ratio stability
    if len(ar_list) > 1:
        ar_series = pd.Series(ar_list)
        ar_std = float(ar_series.std())
    else:
        ar_std = math.nan

    # Continuity: how many missing frames inside the span
    if num_frames_span > 1:
        continuity_score = 1.0 - clamp01(frame_gaps / num_frames_span)
    else:
        continuity_score = 1.0

    # Attribute changes
    attr_change_counts: Dict[str, int] = {}
    total_attr_changes = 0
    total_attr_events = 0

    all_attr_names = set()
    for b in boxes_all:
        all_attr_names.update(b.attributes.keys())

    for attr_name in sorted(all_attr_names):
        prev_val = None
        changes = 0
        events = 0
        for b in boxes_all:
            val = b.attributes.get(attr_name)
            if val is not None:
                val = val.strip()
            if val == "":
                val = None

            if prev_val is None and val is None:
                pass
            elif prev_val is None and val is not None:
                # first seen
                if events > 0:
                    changes += 1
            elif prev_val != val:
                changes += 1

            prev_val = val
            events += 1

        attr_change_counts[attr_name] = changes
        total_attr_changes += changes
        total_attr_events += events

    if total_attr_events > 0:
        attr_change_rate = total_attr_changes / total_attr_events
    else:
        attr_change_rate = 0.0

    # --------------------------------------------------------
    # Turn raw metrics into [0..1] scores
    # --------------------------------------------------------

    # Box consistency score: just average IoU
    box_consistency_score = clamp01(avg_iou)

    # Drift (we approximate as "how often iou >= threshold")
    drift_score = clamp01(consistency_ratio)

    # Size jitter score: high jitter -> lower score
    # Treat mean_area_jitter = 0.0 as perfect, >= 0.3 as very bad
    if math.isnan(mean_area_jitter):
        size_jitter_score = 1.0
    else:
        size_jitter_score = 1.0 - clamp01(mean_area_jitter / 0.3)

    # Aspect ratio stability: std = 0 is perfect, >= 0.5 is bad
    if math.isnan(ar_std):
        ar_stability_score = 1.0
    else:
        ar_stability_score = 1.0 - clamp01(ar_std / 0.5)

    # Attribute stability: higher change rate -> lower score
    # change_rate 0 good, >= 0.3 bad
    attr_stability_score = 1.0 - clamp01(attr_change_rate / 0.3)

    # continuity_score already in [0..1]

    # Weighted overall track score
    overall_track_score = (
        0.35 * box_consistency_score
        + 0.20 * drift_score
        + 0.10 * size_jitter_score
        + 0.05 * ar_stability_score
        + 0.10 * attr_stability_score
        + 0.20 * continuity_score
    )

    metrics = {
        "track_id": track.track_id,
        "label": track.label,
        "num_boxes": num_boxes_total,
        "num_boxes_for_iou": num_boxes_geom,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "avg_iou": avg_iou,
        "min_iou": min_iou,
        "max_iou": max_iou,
        "consistency_ratio": consistency_ratio,
        "mean_area_jitter": mean_area_jitter,
        "ar_std": ar_std,
        "frame_gaps": frame_gaps,
        "num_frames_span": num_frames_span,
        "continuity_score": continuity_score,
        "total_attr_changes": total_attr_changes,
        "attr_change_rate": attr_change_rate,
        "box_consistency_score": box_consistency_score,
        "drift_score": drift_score,
        "size_jitter_score": size_jitter_score,
        "ar_stability_score": ar_stability_score,
        "attr_stability_score": attr_stability_score,
        "overall_track_score": overall_track_score,
    }

    # include per-attribute change counts as extra columns
    for attr_name, changes in attr_change_counts.items():
        col = f"attr_changes::{attr_name}"
        metrics[col] = changes

    return metrics


def compute_all_metrics(
    tracks: List[Track],
    iou_consistency_threshold: float,
    ignore_outside: bool,
) -> pd.DataFrame:
    rows = []
    for t in tracks:
        rows.append(
            compute_track_metrics(
                t,
                iou_consistency_threshold=iou_consistency_threshold,
                ignore_outside=ignore_outside,
            )
        )
    df = pd.DataFrame(rows)
    return df


# ============================================================
# Streamlit UI
# ============================================================

def main():
    st.set_page_config(
        page_title="CVAT Video QA — Geometry & Accuracy Score",
        layout="wide",
    )
    st.title("📊 CVAT Video Annotation QA — Geometry & Accuracy Score")

    st.markdown(
        """
Upload a **CVAT for video 1.1 XML** file to get:

- Geometry-based QA metrics per **track**
- A per-track **0–100 accuracy score**
- A global **task score**
- A list of **suspicious tracks** (low consistency, high jitter, or unstable attributes)
"""
    )

    st.sidebar.header("⚙️ Settings")

    iou_threshold = st.sidebar.slider(
        "IoU threshold for consistency (drift check)",
        0.0,
        1.0,
        0.7,
        0.05,
    )
    ignore_outside = st.sidebar.checkbox(
        "Ignore boxes with outside=1 for geometry checks",
        value=True,
    )
    suspicious_score_cutoff = st.sidebar.slider(
        "Flag tracks with overall score below",
        0.0,
        1.0,
        0.7,
        0.05,
    )

    uploaded_file = st.file_uploader(
        "Upload CVAT video XML",
        type=["xml"],
        help="Use the 'CVAT for video 1.1' export format.",
    )

    if not uploaded_file:
        st.info("⬆️ Upload a CVAT video XML file to start.")
        return

    # Parse
    try:
        tracks = parse_cvat_video_xml(uploaded_file)
    except Exception as e:
        st.error(f"Failed to parse XML: {e}")
        return

    if not tracks:
        st.warning("No <track> elements found in XML.")
        return

    st.success(f"Parsed {len(tracks)} tracks from XML.")

    # Compute metrics
    df = compute_all_metrics(
        tracks,
        iou_consistency_threshold=iou_threshold,
        ignore_outside=ignore_outside,
    )

    # Global scores
    mean_track_score = float(df["overall_track_score"].mean())
    median_track_score = float(df["overall_track_score"].median())
    num_tracks = len(df)
    total_boxes = int(df["num_boxes"].sum())

    # Main KPIs
    st.subheader("🔝 Task Summary (Geometry-based Accuracy)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tracks", num_tracks)
    c2.metric("Total boxes", total_boxes)
    c3.metric("Mean track score (0–100)", f"{mean_track_score * 100:.1f}")
    c4.metric("Median track score (0–100)", f"{median_track_score * 100:.1f}")

    st.caption(
        "Scores are computed from IoU consistency, size jitter, aspect ratio stability, "
        "attribute stability, and continuity."
    )

    # Per-track overview
    st.subheader("📋 Per-track Scores & Metrics")

    display_cols = [
        "track_id",
        "label",
        "num_boxes",
        "first_frame",
        "last_frame",
        "avg_iou",
        "consistency_ratio",
        "mean_area_jitter",
        "ar_std",
        "continuity_score",
        "attr_change_rate",
        "overall_track_score",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    df_view = df[display_cols].copy()

    # Convert scores to % for nicer view
    for col in ["avg_iou", "consistency_ratio", "continuity_score", "overall_track_score"]:
        if col in df_view.columns:
            df_view[col] = df_view[col].astype(float)

    # Round numeric
    for col in df_view.columns:
        if df_view[col].dtype in ["float64", "float32"]:
            df_view[col] = df_view[col].round(3)

    st.dataframe(df_view, use_container_width=True)

    # Suspicious tracks
    st.subheader("🚨 Suspicious Tracks")
    suspicious = df[df["overall_track_score"] < suspicious_score_cutoff].copy()

    if suspicious.empty:
        st.success(
            f"No tracks below score threshold of {suspicious_score_cutoff:.2f} "
            f"({suspicious_score_cutoff * 100:.0f}%)."
        )
    else:
        st.warning(
            f"{len(suspicious)} tracks flagged below {suspicious_score_cutoff:.2f} "
            f"({suspicious_score_cutoff * 100:.0f}%)."
        )
        sus_view = suspicious[display_cols].copy()
        for col in sus_view.columns:
            if sus_view[col].dtype in ["float64", "float32"]:
                sus_view[col] = sus_view[col].round(3)
        st.dataframe(sus_view, use_container_width=True)

    # Download CSV
    st.subheader("⬇️ Export")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download full metrics as CSV",
        data=csv_bytes,
        file_name="cvat_video_geometry_metrics.csv",
        mime="text/csv",
    )

    st.markdown("---")
    st.caption(
        "Hint: You can run this per-task and join with worker IDs (from CVAT or your logs) "
        "to build per-annotator QA dashboards."
    )


if
