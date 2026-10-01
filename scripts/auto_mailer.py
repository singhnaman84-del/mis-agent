"""Automatic performance mailer - run on a schedule (GitHub Actions, see .github/workflows/auto_mailer.yml).

1. Sync the newest MIS files (and the 'Dealer Directory' file) from the Google Drive folder.
2. If the performance reports changed since the last run (report_signature), build one performance mail per dealer in
   the directory and send it (dealer in To, RM in CC). Each dealer gets at most one mail per report version.
3. Write mailer_state.json (last signature + who was mailed) and append to mailer_log.csv.

Environment (GitHub repository secrets):
  GCP_SERVICE_ACCOUNT_JSON, DRIVE_FOLDER_ID, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_FROM_NAME,
  MAIL_REPLY_TO, MAILER_TEST_TO, MAIL_CC_ZM, MAILER_MODE (dry | test | live - default dry), MAILER_OEMS (optional,
  e.g. "SKODA,VOLKSWAGEN"), MAILER_MAX (safety cap per run, default 1500).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mis import mailer  # noqa: E402
from mis.drive import sync  # noqa: E402
from mis.loaders import load_folder  # noqa: E402

STATE = os.environ.get("MAILER_STATE", "mailer_state.json")
LOG = os.environ.get("MAILER_LOG", "mailer_log.csv")


def env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else d


def main() -> int:
    mode = env("MAILER_MODE", "dry").lower()
    data = env("DATA_DIR") or os.path.join(tempfile.gettempdir(), "mis_mailer_data")
    sa = env("GCP_SERVICE_ACCOUNT_JSON")
    if sa:
        sync(json.loads(sa), data, env("DRIVE_FOLDER_ID"))
    pack = load_folder(data)
    d = pack.get("dealer_directory")
    if d.empty:
        print("No 'Dealer Directory' file in the Drive folder - nothing to do."); return 0
    sig = mailer.report_signature(pack)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if state.get("performance_signature") == sig and mode == "live":
        print(f"Reports unchanged since the last mail run ({sig}) - nothing to send."); return 0
    oems = {o.strip().upper() for o in (env("MAILER_OEMS") or "").split(",") if o.strip()}
    done = set(state.get("mailed", {}).get(sig, []))
    codes = [c for c in d["code"] if c not in done]
    mails = mailer.build(pack, d, "performance", codes, cc_zm=mailer.smtp_config(env)["cc_zm"])
    if oems: mails = [m for m in mails if m.get("oem") in oems]
    mails = mails[: int(env("MAILER_MAX", 1500))]
    print(f"Report version {sig}: {len(mails)} performance mails to {mode.upper()}")
    log = mailer.send(mails, mailer.smtp_config(env), mode=mode, log_path=LOG)
    if len(log):
        print(log["status"].value_counts().to_string())
    if mode == "live":
        sent = set(log.loc[log["status"] == "sent", "code"]) if len(log) else set()
        state.setdefault("mailed", {})[sig] = sorted(done | sent)
        state["mailed"] = {sig: state["mailed"][sig]}          # keep only the current version
        if not (len(log) and (log["status"].str.startswith("failed")).any()):
            state["performance_signature"] = sig               # retry failures next run
        json.dump(state, open(STATE, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
