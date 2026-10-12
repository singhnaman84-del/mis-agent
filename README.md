---
title: MIS Agent
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.64.0
app_file: app.py
pinned: false
---

# MIS Agent

Agentic MIS for the dealer insurance-retention business. It reads the daily MIS workbooks straight from Google Drive,
turns them into clean datasets, and gives the team:

- **OEM dashboards**: Royal Enfield, Audi, Skoda, Volkswagen, Volvo and Porsche, each drillable by ZM and RM. Every
  dashboard has KPI tiles, trend charts, FY/OY split, zone and RM rankings, priority dealers (largest gap to target),
  movers, penetration, Volvo not-renewed cases, AI calling, the incentive scheme, field visits and payouts.
- **Auto-insights**: plain-English findings on every dashboard, such as trend, gap to target, the first-year leak, weakest
  zone and where the renewals needed are concentrated.
- **Ask the analyst (Claude)**: the chat panel on the right. Claude answers any question by running queries on the
  data, builds tables and charts itself and ends with recommended actions; each answer opens in full in the main panel
  and exports like any dashboard. Without an Anthropic key a built-in offline analyst answers instead.
- **Click to drill down**: click any dealer, RM or ZM row in any table (or pick one in *Dealer / RM / ZM*) and its full
  dashboard opens at the top of the main panel, with HTML / PDF / PPT / CSV export.
- **Layout and theme** follow the RESP Retention Analyst: top bar with chips, KPI strip with target markers, tabbed
  main panel, analyst panel on the right.
- **Retention Analyst parity, every OEM**: zone picker scoping every tab; tabs for Overview (zone executive page),
  Dealers (search / OEM / RM / RAG filters), RMs (scorecards), RM dashboard, Dealer dashboard (tick one dealer for its
  dashboard or the dealer-facing meeting deck, several for a combined report), OEM dashboards, First year, Royal
  Enfield, Volvo, Open cases, Penetration, Payout and Reports; similar-base peers (±30% → ±50% → zone) with peer
  proof; consolidated dealer groups; user-set criteria (targets, amber band, minimum base) driving every RAG and flag.
- **Reports tab**: zone executive page, boom call pack (per ZM: scorecard, RM talking points, red flags, wins, Teams
  note), underperformers, first vs other year, movers, Volvo page, Royal Enfield page, open cases, dealer groups,
  dealer meeting deck, penetration, payout status, dealer list, RM scorecards, full RM / dealer review, data corrections.
- **Royal Enfield revenue**: ₹240 per retained first-year policy on dealer views, ₹24 on RM / ZM / national views.
- **Export anything**: every dashboard, lookup, data extract and chat answer downloads as **HTML, PDF, PowerPoint
  (editable native charts) or CSV**. The 📌 *Report pack* combines several into one file.
- **Data quality**: which file each number came from, and every problem found in the sources.

## Files it reads (newest of each type wins)

| Book | File name contains | Used for |
|---|---|---|
| Private car | `YTD-MTD Dealer And RM & ZM Wise Renewal Report` | Audi/Skoda/VW/Porsche dealer × month × FY/OY |
| Private car | `VOLVO Retention Summary` | Volvo dealer × month × FY/OY (Jan onward) |
| Private car | `VOLVO Daily Renewal Retention` | Volvo roster, policy-to-policy retention, expiry cohort, EV split |
| Private car | `Volvo Conversion … Not Renewed` | Volvo chase list (no customer identifiers are loaded) |
| Private car | `SAVWIPL Penetration` | New-car penetration + renewal by dealer × month |
| Private car | `SAVWIPL … UTR` | SAVWIPL + Volvo payouts |
| Royal Enfield | `RESP Renewal Report` | Region FY/OY, MTD vs last month, daily renewals, dealer MTD, EDME 360 |
| Royal Enfield | `Royal Enfield AI Calling` | AI calling by dealer, day and region |
| Royal Enfield | `Dealer Performance Incentive Tracker` | Incentive tiers, NOP to next tier |
| Royal Enfield | `High Base and Low Conversion` | Focus dealers |
| Royal Enfield | `Penetration Report as on` | New-vehicle penetration and cover mix |
| Royal Enfield | `RE April to … Renewal Performance` | North-zone monthly history |
| Royal Enfield | `RE_DEALER VISIT REPORT` | Field visits + dealer master (RM/ZM mapping) |
| Royal Enfield | `RE_UTR DETAILS` | RE payouts |

`docs/DATA_DICTIONARY.md` documents every sheet and column of every workbook, including what is used and why.

---

## Deploy for free (about 20 minutes, once)

You need three free things: a **GitHub** account, a **Google Cloud service account** (so the app can read Drive), and
an **Anthropic API key** for Claude (optional - without it the offline analyst answers).

### 1. Put the code on GitHub
1. Create a **private** repository on github.com, for example `mis-agent`.
2. Upload this folder's contents: drag them into *Add file → Upload files*, or use `git push`. `.gitignore` already
   keeps MIS workbooks and secrets out of the repository.

### 2. Let the app read Google Drive (service account)
1. Go to https://console.cloud.google.com, create a project (e.g. `mis-agent`).
2. *APIs & Services → Library* → enable **Google Drive API**.
3. *IAM & Admin → Service accounts → Create*, give it any name, then *Keys → Add key → JSON*. A `.json` file downloads.
4. In Google Drive, **share the folder that holds the MIS files** with the service account's e-mail
   (`…@…iam.gserviceaccount.com`) as **Viewer**. Copy the folder id from its URL
   (`drive.google.com/drive/folders/<this-part>`).

### 3. Get an Anthropic API key (for the Claude analyst)
- https://platform.claude.com → *API keys* → *Create key* (add a payment method under *Billing*). Usage is billed per
  question - roughly $0.10-0.40 on the default Claude Opus 5.5, about half on Claude Sonnet 5.5 (choose in
  *Data & settings*). Refusal fallbacks and prompt caching are on.

### 4a. Deploy on Streamlit Community Cloud (recommended)
1. https://share.streamlit.io → sign in with GitHub → **Create app** → pick the repo, branch `main`, file `app.py`.
2. *Advanced settings → Secrets*: paste the contents of `.streamlit/secrets.toml.example` with your values:
   `APP_PASSWORD`, `ANTHROPIC_API_KEY`, `DRIVE_FOLDER_ID`, and the service-account fields under
   `[gcp_service_account]` (copy each field from the JSON file; keep the `\n` in `private_key`).
3. Deploy. You get a URL like `https://mis-agent.streamlit.app`. In *Settings → Sharing*, restrict viewers to invited
   e-mails for a second lock on top of the password.

### 4b. Or deploy on Hugging Face Spaces
1. https://huggingface.co/new-space → SDK **Streamlit**, visibility **Private**.
2. Upload the files (this README's header already configures the Space).
3. *Settings → Variables and secrets*: add `APP_PASSWORD`, `ANTHROPIC_API_KEY`, `DRIVE_FOLDER_ID`, and
   `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole JSON file as one secret).

### Daily use
Open the app → **🔄 Sync latest MIS from Drive** (it runs automatically on first open) → everything refreshes. Drop
new daily files into the same Drive folder; the newest file of each type is picked by the date in its name.

---

## Run locally
```bash
pip install -r requirements.txt
```
```bash
streamlit run app.py
```
Locally you can skip Drive: put the workbooks in a folder and set `DATA_DIR = "/path/to/folder"` in
`.streamlit/secrets.toml`, or upload them in the sidebar.

## How it works
```
Google Drive ──sync──▶ mis/drive.py ──▶ mis/loaders.py (14 file parsers) ──▶ mis/model.py (unified retention fact,
joins, targets, data-quality checks) ──▶ mis/analytics.py (KPIs, insights, dashboards as Report objects)
                                                     │
          mis/agent.py (LLM + tools: list/describe datasets, query, chart, dashboard, find) ─┤
                                                     ▼
                       app.py (Streamlit UI) ──▶ mis/report.py (HTML · PDF · PPTX · CSV)
```
- The agent never sees raw spreadsheets. It calls tools, so every number it quotes comes from a query it ran. Tool
  schemas are kept shallow so free models accept them.
- Rules the whole app follows: retention % = Σ renewed ÷ Σ base (never an average of percentages). The running month
  is flagged as month-to-date and compared with the same date last month. Penetration and retention are never added
  together.
- Customer-level identifiers are never loaded: customer names, policy, chassis, engine and registration numbers,
  addresses, phone numbers, PAN, GSTIN and UTR numbers.

## Targets
Defaults: RE first-year 30% (from the RESP report's `Target%`), Volvo 75% (from the Volvo workbook), Audi/Skoda/VW
65 / 75 / 70 as placeholders. Change them in the sidebar (*🎯 Targets*) or with the `TARGETS_JSON` secret.
