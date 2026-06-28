"""
UCAT Prep — a Streamlit study app.

Pages: Dashboard (analytics), Practice Questions, Flashcards (spaced repetition),
Study Scheduler, Strategy & Skills, and an AI Tutor powered by Claude.

Covers the four current UCAT subtests: Verbal Reasoning, Decision Making,
Quantitative Reasoning, and Situational Judgement.

Runs on Neon / any PostgreSQL when DATABASE_URL is set, otherwise on a local
SQLite file. Set ANTHROPIC_API_KEY to enable the AI Tutor and APP_PASSWORD to
gate access.
"""

import os
import random
from datetime import date, datetime, timedelta

# Pull secrets into the environment before the data layer reads DATABASE_URL.
try:
    import streamlit as _st_pre
    for _key in ("DATABASE_URL", "ANTHROPIC_API_KEY", "APP_PASSWORD"):
        if _key in _st_pre.secrets:
            os.environ.setdefault(_key, str(_st_pre.secrets[_key]))
except Exception:
    pass

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

import database as db

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UCAT Prep",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()


# ── Password gate ─────────────────────────────────────────────────────────────
def _check_password() -> bool:
    pwd = os.environ.get("APP_PASSWORD", "")
    if not pwd:
        return True
    if st.session_state.get("_authenticated"):
        return True
    st.markdown(
        "<div style='max-width:380px;margin:80px auto 0;text-align:center'>"
        "<div style='font-size:3rem;margin-bottom:8px'>🩺</div>"
        "<h2 style='margin-bottom:4px'>UCAT Prep</h2>"
        "<p style='color:#888;margin-bottom:28px;font-size:14px'>Sign in to start studying</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    col = st.columns([1, 2, 1])[1]
    with col:
        pw = st.text_input("Password", type="password", placeholder="Enter password", label_visibility="collapsed")
        if st.button("Sign in", type="primary", use_container_width=True):
            if pw == pwd:
                st.session_state["_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password — please try again.")
    st.stop()
    return False


_check_password()

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #11324D !important; }
[data-testid="stSidebar"] p, [data-testid="stSidebar"] span,
[data-testid="stSidebar"] label, [data-testid="stSidebar"] div { color: #CFE3F2 !important; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { color: #FFFFFF !important; }
[data-testid="stSidebar"] [data-testid="stMetricValue"] { color: #FFFFFF !important; font-size: 20px !important; }
[data-testid="stSidebar"] [data-testid="stMetricLabel"] { color: #9FC2DD !important; }
[data-testid="metric-container"] {
    background: white; border-radius: 10px; padding: 16px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #E5EDF5;
}
.stTabs [aria-selected="true"] { color: #2E86C1 !important; border-bottom-color: #2E86C1 !important; font-weight: 600 !important; }
.flashcard {
    background: white; border: 1px solid #DCE6F0; border-radius: 14px;
    padding: 38px 28px; text-align: center; font-size: 19px; color: #1B2B3A;
    box-shadow: 0 4px 14px rgba(0,0,0,0.06); min-height: 150px;
    display: flex; align-items: center; justify-content: center;
}
.pill { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; color:white; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
SUBJECTS = db.get_subjects()
SUB_BY_ID = {s["id"]: s for s in SUBJECTS}
SUB_BY_NAME = {s["name"]: s for s in SUBJECTS}


def pill(text, color):
    return f"<span class='pill' style='background:{color}'>{text}</span>"


def subject_selectbox(label, key=None, include_all=False, default_name=None):
    names = (["All subtests"] if include_all else []) + [s["name"] for s in SUBJECTS]
    idx = 0
    if default_name and default_name in names:
        idx = names.index(default_name)
    choice = st.selectbox(label, names, index=idx, key=key)
    if choice == "All subtests":
        return None
    return SUB_BY_NAME[choice]["id"]


# Cognitive subtests are reported on a 300–900 scale; SJT is reported in bands.
COGNITIVE_CODES = {"VR", "DM", "QR"}

# Official UCAT pacing (current 2025+ format): (questions, minutes) per subtest.
# Used to derive a realistic per-question time budget for mock exams.
SUBTEST_TIMING = {
    "VR":  (44, 21),
    "DM":  (35, 37),
    "QR":  (36, 26),
    "SJT": (69, 26),
}


def seconds_per_question(code):
    q, m = SUBTEST_TIMING.get(code, (1, 1))
    return (m * 60) / q


def fmt_mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def est_scaled_score(accuracy_pct):
    """Rough, indicative 300–900 scaled score from raw accuracy (motivational only)."""
    return int(round((300 + accuracy_pct / 100 * 600) / 10) * 10)


def est_sjt_band(accuracy_pct):
    if accuracy_pct >= 80:
        return "Band 1"
    if accuracy_pct >= 60:
        return "Band 2"
    if accuracy_pct >= 40:
        return "Band 3"
    return "Band 4"


def days_to_exam():
    iso = db.get_context("exam_date")
    if not iso:
        return None, None
    try:
        d = date.fromisoformat(iso)
        return (d - date.today()).days, d
    except ValueError:
        return None, None


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🩺 UCAT Prep")
    stats = db.get_overall_stats()
    acc = (stats["correct"] / stats["attempts"] * 100) if stats["attempts"] else 0
    st.metric("Overall accuracy", f"{acc:.0f}%", help="Across all answered practice questions")
    st.metric("Cards due today", stats["cards_due"])
    dte, exam_d = days_to_exam()
    if dte is not None:
        st.metric("Days to exam", dte)
    st.markdown("---")
    page = st.radio(
        "Navigate",
        ["📊 Dashboard", "📝 Practice Questions", "⏱️ Mock Exam", "🃏 Flashcards",
         "🗓️ Study Scheduler", "📚 Strategy & Skills", "🤖 AI Tutor", "⚙️ Manage"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption("Set an exam date in ⚙️ Manage to enable the countdown.")


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ════════════════════════════════════════════════════════════════════════════
def page_dashboard():
    st.title("📊 Dashboard")
    stats = db.get_overall_stats()
    acc = (stats["correct"] / stats["attempts"] * 100) if stats["attempts"] else 0

    dte, exam_d = days_to_exam()
    if dte is not None:
        if dte >= 0:
            st.info(f"🗓️ **{dte} days** until your UCAT on **{exam_d.strftime('%B %d, %Y')}**.")
        else:
            st.success("🎉 Your scheduled exam date has passed — good luck / well done!")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Questions answered", stats["attempts"])
    c2.metric("Accuracy", f"{acc:.0f}%")
    c3.metric("Cards mastered", f"{stats['cards_mastered']}/{stats['cards']}")
    task_pct = (stats["tasks_done"] / stats["tasks_total"] * 100) if stats["tasks_total"] else 0
    c4.metric("Study plan", f"{task_pct:.0f}%", help=f"{stats['tasks_done']} of {stats['tasks_total']} tasks done")

    st.markdown("### Estimated scores")
    rows = db.get_accuracy_by_subject()
    df = pd.DataFrame(rows)
    df["code"] = df["subject_id"].map(lambda sid: SUB_BY_ID.get(sid, {}).get("code", ""))
    df["accuracy"] = df.apply(lambda r: (r["correct"] / r["attempts"] * 100) if r["attempts"] else 0, axis=1)

    score_cols = st.columns(len(SUBJECTS))
    cog_total = 0
    for col, (_, r) in zip(score_cols, df.iterrows()):
        if r["code"] in COGNITIVE_CODES:
            sc = est_scaled_score(r["accuracy"]) if r["attempts"] else None
            cog_total += sc if sc else 0
            col.metric(r["subject_name"], f"{sc}" if sc else "—",
                       help="Indicative 300–900 scaled score from your accuracy")
        else:
            band = est_sjt_band(r["accuracy"]) if r["attempts"] else "—"
            col.metric(r["subject_name"], band, help="Indicative SJT band (1 = strongest)")
    cog_attempted = df[df["code"].isin(COGNITIVE_CODES)]["attempts"].sum()
    if cog_attempted:
        st.caption(f"🎯 Indicative cognitive total: **{cog_total} / 2700** "
                   f"(VR + DM + QR, each 300–900). Estimates from accuracy only — not official UCAT scores.")

    st.markdown("### Accuracy by subtest")
    if not df.empty and df["attempts"].sum() > 0:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df["subject_name"], y=df["accuracy"],
            marker_color=df["color"].tolist(),
            text=[f"{v:.0f}%" for v in df["accuracy"]], textposition="outside",
            customdata=df["attempts"],
            hovertemplate="%{x}<br>Accuracy: %{y:.0f}%<br>Attempts: %{customdata}<extra></extra>",
        ))
        fig.update_layout(yaxis_title="Accuracy (%)", yaxis_range=[0, 110],
                          height=340, margin=dict(t=10, b=10), plot_bgcolor="white")
        st.plotly_chart(fig, use_container_width=True)

        # Readiness — weakest subtests
        ready = df[df["attempts"] > 0].sort_values("accuracy")
        weakest = ready.head(2)["subject_name"].tolist()
        if weakest:
            st.caption(f"💡 Focus area: your lowest accuracy is in **{', '.join(weakest)}**.")
    else:
        st.info("No practice questions answered yet. Head to **📝 Practice Questions** to begin — your analytics will populate here.")

    colA, colB = st.columns(2)
    with colA:
        st.markdown("### Activity (last 30 days)")
        ts = pd.DataFrame(db.get_attempts_over_time(30))
        if not ts.empty:
            ts["accuracy"] = ts["correct"] / ts["attempts"] * 100
            fig2 = px.line(ts, x="day", y="attempts", markers=True)
            fig2.update_traces(line_color="#2E86C1")
            fig2.update_layout(height=280, margin=dict(t=10, b=10), plot_bgcolor="white",
                               yaxis_title="Questions", xaxis_title="")
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.caption("No activity recorded in the last 30 days.")
    with colB:
        st.markdown("### Question bank coverage")
        cov = pd.DataFrame(db.get_accuracy_by_subject())
        qcounts = []
        for s in SUBJECTS:
            qs = db.get_questions(subject_id=s["id"])
            qcounts.append({"subject_name": s["name"], "questions": len(qs), "color": s["color"]})
        qc = pd.DataFrame(qcounts)
        if not qc.empty and qc["questions"].sum() > 0:
            fig3 = go.Figure(go.Pie(labels=qc["subject_name"], values=qc["questions"],
                                    marker_colors=qc["color"].tolist(), hole=0.45))
            fig3.update_layout(height=280, margin=dict(t=10, b=10), showlegend=True)
            st.plotly_chart(fig3, use_container_width=True)

    # Upcoming tasks
    st.markdown("### 🗓️ Upcoming study tasks")
    tasks = [t for t in db.get_study_tasks() if t["status"] != "Done"][:5]
    if tasks:
        for t in tasks:
            cols = st.columns([4, 2, 2, 1])
            sub = SUB_BY_ID.get(t["subject_id"])
            cols[0].markdown(f"**{t['title']}**" + (f" · {sub['name']}" if sub else ""))
            cols[1].caption(f"⏱️ {t['duration_min']} min")
            cols[2].caption(f"📅 {t['due_date'] or '—'}")
            if cols[3].button("✓", key=f"dash_done_{t['id']}", help="Mark done"):
                db.set_task_status(t["id"], "Done")
                st.rerun()
    else:
        st.caption("No open tasks. Add some in **🗓️ Study Scheduler**.")


# ════════════════════════════════════════════════════════════════════════════
# PRACTICE QUESTIONS
# ════════════════════════════════════════════════════════════════════════════
def page_practice():
    st.title("📝 Practice Questions")
    ss = st.session_state

    with st.expander("⚙️ Quiz settings", expanded="quiz" not in ss):
        c1, c2, c3 = st.columns(3)
        with c1:
            sid = subject_selectbox("Subtest", key="quiz_subject", include_all=True)
        with c2:
            difficulty = st.selectbox("Difficulty", ["All", "Easy", "Medium", "Hard"], key="quiz_diff")
        with c3:
            n = st.number_input("Questions", 1, 50, 5, key="quiz_n")
        if st.button("▶️ Start quiz", type="primary"):
            pool = db.get_questions(subject_id=sid, difficulty=difficulty)
            random.shuffle(pool)
            pool = pool[:int(n)]
            if not pool:
                st.warning("No questions match those filters yet. Add some in ⚙️ Manage.")
            else:
                ss["quiz"] = pool
                ss["quiz_idx"] = 0
                ss["quiz_answered"] = {}
                ss["quiz_correct"] = 0
                ss["quiz_start"] = datetime.now().timestamp()
                st.rerun()

    if "quiz" not in ss:
        st.info("Configure your quiz above and press **Start quiz**. Every answer is logged so your Dashboard analytics stay current.")
        return

    quiz = ss["quiz"]
    idx = ss["quiz_idx"]

    # Finished
    if idx >= len(quiz):
        score = ss["quiz_correct"]
        total = len(quiz)
        st.success(f"## ✅ Quiz complete — {score}/{total} correct ({score/total*100:.0f}%)")
        st.progress(score / total)
        if st.button("🔄 New quiz"):
            for k in ("quiz", "quiz_idx", "quiz_answered", "quiz_correct"):
                ss.pop(k, None)
            st.rerun()
        return

    q = quiz[idx]
    sub = SUB_BY_ID.get(q["subject_id"])
    st.progress((idx) / len(quiz), text=f"Question {idx + 1} of {len(quiz)}")
    if sub:
        st.markdown(pill(sub["name"], sub["color"]) + f"  &nbsp; <span style='color:#888'>{q['difficulty']}</span>", unsafe_allow_html=True)
    st.markdown(f"### {q['stem']}")

    options = {"A": q["option_a"], "B": q["option_b"], "C": q["option_c"], "D": q["option_d"]}
    answered = ss["quiz_answered"].get(idx)

    if not answered:
        choice = st.radio("Choose one:", list(options.keys()),
                          format_func=lambda k: f"{k}. {options[k]}", key=f"q_{idx}")
        if st.button("Submit answer", type="primary"):
            is_correct = (choice == q["correct"])
            elapsed = datetime.now().timestamp() - ss.get("quiz_start", datetime.now().timestamp())
            db.record_attempt(q["id"], q["subject_id"], choice, is_correct, round(elapsed, 1))
            ss["quiz_answered"][idx] = choice
            if is_correct:
                ss["quiz_correct"] += 1
            ss["quiz_start"] = datetime.now().timestamp()
            st.rerun()
    else:
        for k, v in options.items():
            if k == q["correct"]:
                st.markdown(f"✅ **{k}. {v}**")
            elif k == answered:
                st.markdown(f"❌ ~~{k}. {v}~~")
            else:
                st.markdown(f"&nbsp;&nbsp;&nbsp;{k}. {v}", unsafe_allow_html=True)
        if answered == q["correct"]:
            st.success("Correct!")
        else:
            st.error(f"Not quite — the answer is **{q['correct']}**.")
        if q.get("explanation"):
            st.info(f"**Explanation.** {q['explanation']}")
        if st.button("Next ▶️", type="primary"):
            ss["quiz_idx"] += 1
            st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# FLASHCARDS
# ════════════════════════════════════════════════════════════════════════════
def page_flashcards():
    st.title("🃏 Flashcards")
    st.caption("Spaced repetition (SM-2). Rate each card honestly — harder cards come back sooner.")
    ss = st.session_state

    c1, c2 = st.columns([3, 1])
    with c1:
        sid = subject_selectbox("Subtest", key="fc_subject", include_all=True)
    with c2:
        due_only = st.toggle("Due only", value=True, key="fc_due")

    cards = db.get_flashcards(subject_id=sid, due_only=due_only)
    if not cards:
        if due_only:
            st.success("🎉 No cards due right now. Toggle off **Due only** to review ahead, or add cards in ⚙️ Manage.")
        else:
            st.info("No flashcards yet. Add some in ⚙️ Manage.")
        return

    if "fc_pos" not in ss or ss.get("fc_count") != len(cards):
        ss["fc_pos"] = 0
        ss["fc_count"] = len(cards)
        ss["fc_show_back"] = False

    pos = ss["fc_pos"] % len(cards)
    card = cards[pos]
    sub = SUB_BY_ID.get(card["subject_id"])

    st.progress((pos) / len(cards), text=f"Card {pos + 1} of {len(cards)} due")
    if sub:
        st.markdown(pill(sub["name"], sub["color"]), unsafe_allow_html=True)

    face = card["back"] if ss.get("fc_show_back") else card["front"]
    label = "ANSWER" if ss.get("fc_show_back") else "PROMPT"
    st.markdown(f"<div class='flashcard'><div><div style='font-size:11px;letter-spacing:1px;color:#9aa;margin-bottom:12px'>{label}</div>{face}</div></div>", unsafe_allow_html=True)
    st.write("")

    if not ss.get("fc_show_back"):
        if st.button("🔄 Show answer", type="primary", use_container_width=True):
            ss["fc_show_back"] = True
            st.rerun()
    else:
        st.caption("How well did you recall it?")
        cols = st.columns(4)
        ratings = [("😖 Again", 0), ("😬 Hard", 3), ("🙂 Good", 4), ("😎 Easy", 5)]
        for col, (lbl, quality) in zip(cols, ratings):
            if col.button(lbl, key=f"fc_rate_{quality}", use_container_width=True):
                db.review_flashcard(card["id"], quality)
                ss["fc_show_back"] = False
                ss["fc_pos"] = pos + 1
                ss["fc_count"] = None  # force refresh of the due list
                st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# STUDY SCHEDULER
# ════════════════════════════════════════════════════════════════════════════
def page_scheduler():
    st.title("🗓️ Study Scheduler")
    ss = st.session_state

    with st.expander("➕ Add a study task"):
        with st.form("add_task", clear_on_submit=True):
            c1, c2 = st.columns(2)
            title = c1.text_input("Task", placeholder="e.g. Review enzyme kinetics + 10 Qs")
            sid = c2.selectbox("Subtest", ["—"] + [s["name"] for s in SUBJECTS])
            c3, c4, c5 = st.columns(3)
            ttype = c3.selectbox("Type", ["Review", "Practice", "Flashcards", "Full-length", "CARS"])
            due = c4.date_input("Due date", value=date.today())
            dur = c5.number_input("Minutes", 15, 480, 60, step=15)
            notes = st.text_area("Notes", placeholder="Optional")
            if st.form_submit_button("Add task", type="primary"):
                if title.strip():
                    db.upsert_study_task({
                        "title": title.strip(),
                        "subject_id": SUB_BY_NAME[sid]["id"] if sid != "—" else None,
                        "task_type": ttype, "due_date": due.isoformat(),
                        "duration_min": int(dur), "notes": notes,
                    })
                    st.success("Task added.")
                    st.rerun()
                else:
                    st.warning("Give the task a title.")

    # Auto-generate a plan
    with st.expander("✨ Generate a study plan"):
        st.caption("Creates one strategy + one practice task per subtest, spread across the days you choose.")
        gc1, gc2 = st.columns(2)
        weeks = gc1.number_input("Spread over (days)", 3, 60, 14)
        per_day = gc2.number_input("Tasks per day", 1, 6, 2)
        if st.button("Generate plan"):
            plan_tasks = []
            for s in SUBJECTS:
                plan_tasks.append((f"Review: {s['name']} high-yield topics", s["id"], "Review"))
                plan_tasks.append((f"Practice: {s['name']} question set", s["id"], "Practice"))
                plan_tasks.append((f"Flashcards: {s['name']}", s["id"], "Flashcards"))
            day = 0
            for i, (title, sid, ttype) in enumerate(plan_tasks):
                due = date.today() + timedelta(days=int(i // per_day) % int(weeks))
                db.upsert_study_task({"title": title, "subject_id": sid, "task_type": ttype,
                                      "due_date": due.isoformat(), "duration_min": 60})
            st.success(f"Generated {len(plan_tasks)} tasks.")
            st.rerun()

    filt = st.radio("Show", ["All", "Todo", "In Progress", "Done"], horizontal=True)
    tasks = db.get_study_tasks(status=filt)
    if not tasks:
        st.info("No tasks. Add one above or generate a plan.")
        return

    today = date.today()
    for t in tasks:
        sub = SUB_BY_ID.get(t["subject_id"])
        overdue = t["due_date"] and t["due_date"] < today.isoformat() and t["status"] != "Done"
        cols = st.columns([0.5, 4, 2, 2, 1.5, 0.6])
        done = t["status"] == "Done"
        if cols[0].checkbox("", value=done, key=f"task_chk_{t['id']}", label_visibility="collapsed"):
            if not done:
                db.set_task_status(t["id"], "Done")
                st.rerun()
        else:
            if done:
                db.set_task_status(t["id"], "Todo")
                st.rerun()
        title_md = f"~~{t['title']}~~" if done else f"**{t['title']}**"
        badge = pill(sub["name"], sub["color"]) if sub else ""
        cols[1].markdown(f"{title_md}  {badge}", unsafe_allow_html=True)
        cols[2].caption(f"🏷️ {t['task_type']} · ⏱️ {t['duration_min']}m")
        date_txt = t["due_date"] or "—"
        cols[3].markdown(f"<span style='color:{'#c0392b' if overdue else '#888'}'>📅 {date_txt}{' (overdue)' if overdue else ''}</span>", unsafe_allow_html=True)
        status = cols[4].selectbox("", ["Todo", "In Progress", "Done"],
                                   index=["Todo", "In Progress", "Done"].index(t["status"]),
                                   key=f"task_status_{t['id']}", label_visibility="collapsed")
        if status != t["status"]:
            db.set_task_status(t["id"], status)
            st.rerun()
        if cols[5].button("🗑️", key=f"task_del_{t['id']}"):
            db.delete_study_task(t["id"])
            st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# CONTENT REVIEW
# ════════════════════════════════════════════════════════════════════════════
def page_content():
    st.title("📚 Strategy & Skills")
    c1, c2 = st.columns([3, 1])
    with c1:
        sid = subject_selectbox("Subtest", key="content_subject", include_all=True)
    with c2:
        hy = st.toggle("High-yield only", value=False, key="content_hy")

    topics = db.get_topics(subject_id=sid, high_yield_only=hy)
    if not topics:
        st.info("No topics found. Add review notes in ⚙️ Manage.")
        return

    # group by subject
    by_subject = {}
    for t in topics:
        by_subject.setdefault(t["subject_name"], []).append(t)

    for sname, items in by_subject.items():
        color = items[0]["color"]
        st.markdown(f"### {pill(sname, color)}", unsafe_allow_html=True)
        for t in items:
            label = ("⭐ " if t["high_yield"] else "") + t["name"]
            with st.expander(label):
                if t.get("summary"):
                    st.caption(t["summary"])
                st.markdown(t.get("content") or "_No notes yet._")
                ncards = len(db.get_flashcards(subject_id=t["subject_id"]))
                st.caption(f"💬 Ask the AI Tutor about this topic from the 🤖 AI Tutor page.")


# ════════════════════════════════════════════════════════════════════════════
# AI TUTOR
# ════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "You are an expert UCAT tutor. Help the student prepare for the four current UCAT "
    "subtests: Verbal Reasoning, Decision Making, Quantitative Reasoning, and Situational "
    "Judgement. The UCAT is a timed aptitude test — emphasise technique, speed, and "
    "time-management as much as accuracy. For Situational Judgement, ground answers in the "
    "GMC 'Good Medical Practice' principles (patient safety, confidentiality, integrity, "
    "working within competence). "
    "Explain clearly and concisely, use analogies where helpful, show the reasoning for "
    "quantitative problems step by step, and when relevant point out common UCAT traps and "
    "time-saving shortcuts. Keep answers focused and exam-oriented."
)


def page_tutor():
    st.title("🤖 AI Tutor")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _HAS_ANTHROPIC or not api_key:
        st.warning(
            "The AI Tutor needs the `anthropic` package and an `ANTHROPIC_API_KEY`. "
            "Add the key to your Streamlit secrets or environment to enable chat. "
            "Everything else in the app works without it."
        )
        return

    cols = st.columns([4, 1])
    cols[0].caption("Ask anything — concepts, practice problems, study strategy.")
    if cols[1].button("🗑️ Clear chat"):
        db.clear_chat_history()
        st.rerun()

    history = db.get_chat_history(40)
    for m in history:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    prompt = st.chat_input("e.g. Explain the difference between competitive and noncompetitive inhibition")
    if prompt:
        db.save_message("user", prompt)
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            try:
                client = anthropic.Anthropic(api_key=api_key)
                msgs = [{"role": m["role"], "content": m["content"]} for m in db.get_chat_history(20)]
                with st.spinner("Thinking…"):
                    resp = client.messages.create(
                        model="claude-opus-4-8",
                        max_tokens=1200,
                        system=SYSTEM_PROMPT,
                        messages=msgs,
                    )
                answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            except Exception as e:
                answer = f"⚠️ Sorry, the tutor hit an error: `{e}`"
            st.markdown(answer)
            db.save_message("assistant", answer)
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# MANAGE
# ════════════════════════════════════════════════════════════════════════════
def page_manage():
    st.title("⚙️ Manage")
    tabs = st.tabs(["Exam date", "Questions", "Flashcards", "Topics"])

    # Exam date
    with tabs[0]:
        cur = db.get_context("exam_date")
        cur_d = date.fromisoformat(cur) if cur else date.today() + timedelta(days=90)
        new_d = st.date_input("UCAT exam date", value=cur_d)
        if st.button("Save exam date", type="primary"):
            db.set_context("exam_date", new_d.isoformat())
            st.success("Saved.")
            st.rerun()

    # Questions
    with tabs[1]:
        with st.form("add_q", clear_on_submit=True):
            st.markdown("**Add a practice question**")
            sname = st.selectbox("Subtest", [s["name"] for s in SUBJECTS], key="mq_sub")
            stem = st.text_area("Question stem")
            c = st.columns(2)
            a = c[0].text_input("Option A")
            b = c[1].text_input("Option B")
            cc = c[0].text_input("Option C")
            d = c[1].text_input("Option D")
            c2 = st.columns(2)
            correct = c2[0].selectbox("Correct answer", ["A", "B", "C", "D"])
            diff = c2[1].selectbox("Difficulty", ["Easy", "Medium", "Hard"], index=1)
            expl = st.text_area("Explanation")
            if st.form_submit_button("Add question", type="primary"):
                if stem and a and b and cc and d:
                    db.upsert_question({
                        "subject_id": SUB_BY_NAME[sname]["id"], "stem": stem,
                        "option_a": a, "option_b": b, "option_c": cc, "option_d": d,
                        "correct": correct, "explanation": expl, "difficulty": diff,
                    })
                    st.success("Question added.")
                    st.rerun()
                else:
                    st.warning("Fill in the stem and all four options.")
        st.divider()
        qs = db.get_questions()
        st.caption(f"{len(qs)} questions in the bank")
        for q in qs:
            with st.expander(f"[{SUB_BY_ID.get(q['subject_id'],{}).get('name','?')}] {q['stem'][:70]}"):
                st.markdown(f"**Correct:** {q['correct']} · **Difficulty:** {q['difficulty']}")
                st.caption(q.get("explanation") or "")
                if st.button("Delete", key=f"delq_{q['id']}"):
                    db.delete_question(q["id"])
                    st.rerun()

    # Flashcards
    with tabs[2]:
        with st.form("add_fc", clear_on_submit=True):
            st.markdown("**Add a flashcard**")
            sname = st.selectbox("Subtest", [s["name"] for s in SUBJECTS], key="mfc_sub")
            front = st.text_area("Front (prompt)")
            back = st.text_area("Back (answer)")
            if st.form_submit_button("Add flashcard", type="primary"):
                if front and back:
                    db.upsert_flashcard({"subject_id": SUB_BY_NAME[sname]["id"], "front": front, "back": back})
                    st.success("Flashcard added.")
                    st.rerun()
                else:
                    st.warning("Fill in both sides.")
        st.divider()
        cards = db.get_flashcards()
        st.caption(f"{len(cards)} flashcards")
        for fc in cards:
            with st.expander(f"[{SUB_BY_ID.get(fc['subject_id'],{}).get('name','?')}] {fc['front'][:70]}"):
                st.markdown(f"**Back:** {fc['back']}")
                st.caption(f"Reps: {fc['reps']} · Due: {fc['due_date'] or '—'} · Ease: {fc['ease']}")
                if st.button("Delete", key=f"delfc_{fc['id']}"):
                    db.delete_flashcard(fc["id"])
                    st.rerun()

    # Topics
    with tabs[3]:
        with st.form("add_topic", clear_on_submit=True):
            st.markdown("**Add a review topic**")
            sname = st.selectbox("Subtest", [s["name"] for s in SUBJECTS], key="mt_sub")
            name = st.text_input("Topic name")
            hy = st.checkbox("High-yield ⭐")
            summary = st.text_input("One-line summary")
            content = st.text_area("Notes (Markdown supported)", height=160)
            if st.form_submit_button("Add topic", type="primary"):
                if name:
                    db.upsert_topic({"subject_id": SUB_BY_NAME[sname]["id"], "name": name,
                                     "high_yield": 1 if hy else 0, "summary": summary, "content": content})
                    st.success("Topic added.")
                    st.rerun()
                else:
                    st.warning("Give the topic a name.")
        st.divider()
        for t in db.get_topics():
            with st.expander(f"{'⭐ ' if t['high_yield'] else ''}[{t['subject_name']}] {t['name']}"):
                st.markdown(t.get("content") or "_No notes._")
                if st.button("Delete", key=f"delt_{t['id']}"):
                    db.delete_topic(t["id"])
                    st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# MOCK EXAM (timed)
# ════════════════════════════════════════════════════════════════════════════
def _build_mock(subtest_ids):
    """Assemble an ordered question list grouped by subtest, with a time budget."""
    questions = []
    for s in SUBJECTS:
        if subtest_ids and s["id"] not in subtest_ids:
            continue
        qs = db.get_questions(subject_id=s["id"])
        random.shuffle(qs)
        questions.extend(qs)
    budget = sum(seconds_per_question(SUB_BY_ID[q["subject_id"]]["code"]) for q in questions)
    return questions, int(budget)


def _finish_mock(ss, elapsed):
    """Record every answered question to analytics once, then flip to the results screen."""
    if not ss.get("mock_graded"):
        for i, q in enumerate(ss["mock"]):
            chosen = ss["mock_answers"].get(i)
            if chosen:
                db.record_attempt(q["id"], q["subject_id"], chosen, chosen == q["correct"], 0)
        ss["mock_graded"] = True
    ss["mock_elapsed"] = int(elapsed)
    ss["mock_done"] = True


def _mock_results(ss):
    quiz = ss["mock"]
    answers = ss["mock_answers"]
    rows = {}
    for i, q in enumerate(quiz):
        code = SUB_BY_ID[q["subject_id"]]["code"]
        r = rows.setdefault(code, {"name": SUB_BY_ID[q["subject_id"]]["name"],
                                   "color": SUB_BY_ID[q["subject_id"]]["color"],
                                   "correct": 0, "total": 0})
        r["total"] += 1
        if answers.get(i) == q["correct"]:
            r["correct"] += 1
    return rows


def page_mock():
    st.title("⏱️ Mock Exam")
    ss = st.session_state

    # ── Results screen ────────────────────────────────────────────────────────
    if ss.get("mock_done"):
        rows = _mock_results(ss)
        total_q = sum(r["total"] for r in rows.values())
        total_correct = sum(r["correct"] for r in rows.values())
        used = ss.get("mock_elapsed", 0)
        st.success(f"## ✅ Mock complete — {total_correct}/{total_q} correct "
                   f"({(total_correct/total_q*100) if total_q else 0:.0f}%)")
        st.caption(f"⏱️ Time used: {fmt_mmss(used)} of {fmt_mmss(ss.get('mock_budget', 0))}")

        cols = st.columns(len(rows) or 1)
        cog_total, cog_any = 0, False
        for col, (code, r) in zip(cols, rows.items()):
            acc = (r["correct"] / r["total"] * 100) if r["total"] else 0
            if code in COGNITIVE_CODES:
                sc = est_scaled_score(acc)
                cog_total += sc
                cog_any = True
                col.metric(r["name"], sc, help=f"{r['correct']}/{r['total']} correct · indicative 300–900")
            else:
                col.metric(r["name"], est_sjt_band(acc), help=f"{r['correct']}/{r['total']} correct · indicative band")
        if cog_any:
            st.caption(f"🎯 Indicative cognitive total: **{cog_total} / 2700**. "
                       "Estimates from accuracy only — not official UCAT scores. "
                       "All answers were saved to your analytics.")

        with st.expander("Review answers"):
            for i, q in enumerate(ss["mock"]):
                chosen = ss["mock_answers"].get(i)
                ok = chosen == q["correct"]
                mark = "✅" if ok else ("❌" if chosen else "⏭️")
                st.markdown(f"{mark} **{q['stem'][:90]}**")
                opts = {"A": q["option_a"], "B": q["option_b"], "C": q["option_c"], "D": q["option_d"]}
                st.caption(f"Your answer: {chosen or '— (skipped)'} · Correct: {q['correct']} ({opts[q['correct']]})")
                if q.get("explanation"):
                    st.caption(f"💡 {q['explanation']}")

        if st.button("🔄 New mock", type="primary"):
            for k in list(ss.keys()):
                if k.startswith("mock"):
                    ss.pop(k, None)
            st.rerun()
        return

    # ── Setup screen ──────────────────────────────────────────────────────────
    if "mock" not in ss:
        st.markdown("Sit a timed, UCAT-paced mock using your question bank. Each subtest is "
                    "timed at the real per-question rate, so the clock pressure mirrors the exam.")
        st.caption("Official pacing — VR 44Q/21m · DM 35Q/37m · QR 36Q/26m · SJT 69Q/26m. "
                   "Add more questions in ⚙️ Manage to lengthen your mocks.")
        mode = st.radio("Mode", ["Full mock (all subtests)", "Single subtest"], horizontal=True)
        subtest_ids = None
        if mode == "Single subtest":
            sid = subject_selectbox("Subtest", key="mock_subtest")
            subtest_ids = [sid] if sid else None

        # preview count + budget
        preview_q, preview_budget = _build_mock(subtest_ids)
        if not preview_q:
            st.warning("No questions available for that selection. Add some in ⚙️ Manage.")
            return
        st.info(f"📋 {len(preview_q)} questions · ⏱️ {fmt_mmss(preview_budget)} total")

        if st.button("▶️ Start mock", type="primary"):
            quiz, budget = _build_mock(subtest_ids)
            ss["mock"] = quiz
            ss["mock_idx"] = 0
            ss["mock_answers"] = {}
            ss["mock_budget"] = budget
            ss["mock_start"] = datetime.now().timestamp()
            st.rerun()
        return

    # ── In-progress exam ──────────────────────────────────────────────────────
    quiz = ss["mock"]
    budget = ss["mock_budget"]
    elapsed = datetime.now().timestamp() - ss["mock_start"]
    remaining = budget - elapsed

    # Time's up → grade automatically
    if remaining <= 0:
        _finish_mock(ss, budget)
        st.rerun()

    idx = ss["mock_idx"]
    if idx >= len(quiz):
        _finish_mock(ss, elapsed)
        st.rerun()

    # Header: live countdown (cosmetic client-side ticker) + progress
    top = st.columns([2, 3])
    with top[0]:
        components.html(f"""
            <div id='ucat-timer' style="font:600 26px/1.2 -apple-system,Segoe UI,Roboto,sans-serif;
                 color:{'#c0392b' if remaining < 60 else '#11324D'}"></div>
            <script>
              let r = {int(remaining)};
              const el = document.getElementById('ucat-timer');
              function tick() {{
                const m = Math.floor(Math.max(0,r)/60), s = Math.max(0,r)%60;
                el.textContent = '⏱️ ' + m + ':' + String(s).padStart(2,'0') + ' remaining';
                if (r > 0) {{ r--; setTimeout(tick, 1000); }}
              }}
              tick();
            </script>
        """, height=44)
    with top[1]:
        st.progress(idx / len(quiz), text=f"Question {idx + 1} of {len(quiz)}")

    q = quiz[idx]
    sub = SUB_BY_ID.get(q["subject_id"])
    if sub:
        st.markdown(pill(sub["name"], sub["color"]) + f"  &nbsp; <span style='color:#888'>{q['difficulty']}</span>",
                    unsafe_allow_html=True)
    st.markdown(f"### {q['stem']}")

    options = {"A": q["option_a"], "B": q["option_b"], "C": q["option_c"], "D": q["option_d"]}
    prev = ss["mock_answers"].get(idx)
    choice = st.radio("Choose one:", list(options.keys()),
                      format_func=lambda k: f"{k}. {options[k]}",
                      index=list(options).index(prev) if prev in options else 0,
                      key=f"mock_q_{idx}")

    nav = st.columns([1, 1, 1, 3])
    if nav[0].button("◀ Back", disabled=idx == 0):
        ss["mock_idx"] -= 1
        st.rerun()
    if nav[1].button("Skip ▶"):
        ss["mock_idx"] += 1
        st.rerun()
    if nav[2].button("Save & next ▶", type="primary"):
        ss["mock_answers"][idx] = choice
        ss["mock_idx"] += 1
        st.rerun()
    if nav[3].button("🏁 Finish & grade"):
        if choice:
            ss["mock_answers"][idx] = choice
        _finish_mock(ss, elapsed)
        st.rerun()


# ── Router ────────────────────────────────────────────────────────────────────
PAGES = {
    "📊 Dashboard": page_dashboard,
    "📝 Practice Questions": page_practice,
    "⏱️ Mock Exam": page_mock,
    "🃏 Flashcards": page_flashcards,
    "🗓️ Study Scheduler": page_scheduler,
    "📚 Strategy & Skills": page_content,
    "🤖 AI Tutor": page_tutor,
    "⚙️ Manage": page_manage,
}
PAGES[page]()
