# Financial Planning Playground

Streamlit host for the Financial Plan engine handed off by the CRM team
(Infinite internal tool, snapshot 2026-07-17). The goal: a more dynamic
"playground" on top of the same engine — v1 hosts the tool as-is, iterations
come later.

**Ground rule (v1):** everything under `code/` is a **byte-identical copy** of
the handoff package — do not edit it casually (see `00_START_HERE.md`,
"Ground rules for the fork"). [streamlit_app.py](streamlit_app.py) is only a UI
wrapper: its form model is a port of `code/frontend/planForm.js` (the CRM
form), and its output shaping mirrors the pure helpers in
`code/app/planning/service.py`.

## Run locally

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Sanity checks (both need nothing but the pip install above):

```
python run_example.py           # engine solves a sample plan
pytest code/tests -q            # 117 passed, 6 skipped expected (~90s)
```

## Deploy (Streamlit Community Cloud)

1. Go to https://share.streamlit.io → **Create app** → pick this repo,
   branch `main`, main file `streamlit_app.py`.
2. Python version 3.12 (pinned in `.python-version`), dependencies from
   `requirements.txt` — no secrets, no database.

## Reading order

Start with [00_START_HERE.md](00_START_HERE.md) — it explains the two-part
architecture, what's portable vs app-coupled, and the docs reading order
(`docs/` = contracts, `v3_docs/` = the canonical simulation model + decisions).
