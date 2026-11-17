# Job Agent Spec

Branch: `main-agenticAI`. Reporting and approvals go to **both chat and Gmail**. The Reporter has two channels: email (Gmail API, built first) and chat via **WhatsApp (Meta WhatsApp Cloud API; no Twilio)**. Sender is Meta's test number; Raja's number is the verified recipient. Inbound replies arrive at a webhook route in the Flask app (`jobmonitor.onlinegbc.com`).

## Mission
Find and apply to AI leadership roles for Raja Gopalan (VP / Director / Chief level) on LinkedIn and employer sites, fully autonomously under the rules below. Execute orders as given; where no order covers a choice, ask and wait.

## Processes (four, all reporting to Raja by chat and email)
1. **Discovery scan** (every 10 min, 24/7, 60-min cap): search and queue only, never applies.
2. **Application process** (every 10 min, 24/7, 60-min cap): works one queued posting per run.
3. **Outreach sender** (every 10 min, 24/7, 60-min cap): sends only Raja-approved invites and follow-up messages.
4. **Invite acceptance check** (daily): reports by email.

Plus a **Reporter**: the only component that sends messages. The scan, applier and sender write events to an outbox (`data/agent/outbox.json`); the Reporter posts each to the chat and emails it via the Gmail API. Start state: all paused; enable only on Raja's word.

## Models
Core check: Sonnet. Form answers: Sonnet (Haiku was considered; Sonnet chosen because a wrong or invented answer is the costly failure). Both overridable in `agent/config.py` or by environment variable.

## Step 1: Search (Discovery)
- One deterministic title search: (leadership word: VP, Vice President, Vice-President, SVP, EVP, Director, Head, Leader, Chief AND AI word: AI, Artificial Intelligence, in the title) OR a top-officer title alone (CAIO, CTO, Chief Technology Officer, CIO, Chief Information Officer, Fractional CTO, Fractional CIO).
- No company list in the search; company exclusions live in Gate 1 only.
- US geography, Past 24 hours, Remote only, Most recent sort, Under-10-applicants filter applied on screen. Verify all three filter badges are visibly active before reading results. If the sort will not hold, record that and continue with page 1.
- Discovery searches Easy Apply postings only (decision 2026-11-16). Company-site postings are not searched by Discovery; the monitor emails still list them. Contract roles welcome.
- Only IDs not in the seen ledger are new. Open every new posting on page 1; if page 1 has no new IDs, check page 2.
- Load check: the posting's own page must load live with matching title and company. Removed/invalid page or mismatch = extraction error: do not ledger, do not queue. A live page saying it is not accepting applications is skipped for good and ledgered.
- Ledger every genuinely new processed ID in `seen_jobs.json` at once. A ledgered ID is never reopened or re-judged.

### TEST-ONLY discovery profile (2026-11-16, temporary). **Ended 2026-11-16: switched back to `spec`** after Easy Apply was proven
To validate Easy Apply on more postings, Discovery has a second profile, `test`, selected with
`python -m agent.discovery --set-profile test` (and back with `--set-profile spec`; `spec` is the default). Compared with the spec's search it:
- adds role words (Architect, Principal, Lead, Manager) and domain words (ML, Machine Learning, GenAI, Generative AI, LLM,
  Data Science, Data Scientist, Agentic) to the title rule;
- restricts the search to **Easy Apply**;
- widens the window from the past 24 hours to the **past week**, and **drops the "under 10 applicants" limit**;
- reads up to 4 result pages per run instead of 2.
Everything else, including the gates and "Remote only", is unchanged. The run stops without reading anything if LinkedIn's
filter panel does not show exactly these settings. It is not the spec's search. Switch back to `spec` when testing is done.

## Step 2: Gate 1 (`criteria_gate.py`)
Skip if: excluded company (exact name): Jobgether, Archer, Hackajob, Axial Search, Crossover, Get IT, Ethos, Talentify, DataAnnotation, Toptal, Stealth Stanford AI Startup, Lensa, Tempus, Harnham, Swooped (Ladders is eligible; RemoteJobsOne is a standing ban); title contains "partnerships" or "business development"; marketing/revenue/GTM title with no AI-type word; title contains "process intelligence" or "process mining"; equity-only terms; PhD/doctorate required (including "PhD or equivalent"; "preferred" does not block).
No leadership-title, applicant-count, workplace or location check in Gate 1.

## Step 3: Core check (`core_check.py`)
Applies only when the title carries none of: AI, artificial intelligence, GenAI, generative AI, machine learning, ML, LLM, data science, data analytics, analytics. Otherwise judge the full description: the role must exist to build, lead, or own products, platforms, models, or programs in those fields. Fail if the field is only a tool, a passing mention, or company background, or if none is mentioned.

## Step 4: Gate 2 (`dup_gate.py`)
Same company + same role title already in the tracker = duplicate (including reposts). Same client role via a different company is not. Queue twins: apply to one, drop the rest at once. Qualification is sticky.

## Step 5: Queueing
Passers are queued in `pending_survivors.json` through `queue_tool.py` only, with job ID, title, company, location, workplace, applicant count, apply type, poster, and the note "QUEUED — awaiting the application process." Remote passers are queued. **Hybrid, On-site and unknown-workplace postings are dropped (decision 2026-11-16):** their IDs stay ledgered and they are never queued, with no override. ~~Mark "I'm interested" on each passing company's page once per company per run; log failures; never report marks.~~ **Not implemented (decision 2026-11-16).** LinkedIn company pages return a Cloudflare bot check to headless browsers, which every scheduled process uses; they only load in a visible browser. Rather than work around that check, Raja chose to skip this feature. A visible-browser batch (run by hand) or a visible window per scan were the alternatives.

## Step 6: Applying
- Claim one queued posting atomically (oldest eligible; skip AWAITING RAJA / DO NOT ATTEMPT / PICKUP IN PROGRESS; a live claim blocks the same company; claims go stale after 70 min).
- Verify not already submitted (posting page, LinkedIn Applied list, employer record, Gmail). Re-run Gate 2.
- Resume mapping: VP titles → VP resume; Director family → SAP resume (`RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx`; the BDO and FDE resumes are reserved for later); Chief → Chief; Head → VP; CEO → Chief. Resumes live in `resumes/`. Uploads pre-authorized.
- Accounts use raja.gbc@gmail.com; if not accepted, use rgopalan@onlinegbc.com without asking. Use a saved session if signed in.
- Answer only required questions from `resumes/StandingAnswers.txt` and the profile. Never invent. Never auto-answer 0 years. A credential question naming something he does not hold, or a question with no basis, stops the application and is reported word for word. Optional questions stay blank.
- Hard blockers: required education attendance dates, or risk-appetite / financial-capacity questions: skip. Applications carry Columbia, Harvard, and Manhattanville only. References required: "Will be provided once interview is confirmed"; may-contact-references No. Salary: Open/blank; forced range uses the one containing 206000; forced numeric field pauses.
- Settle only on LinkedIn's or the employer's own record, reserve the number from `next_number.py`, then book the tracker row. A paused application is marked AWAITING RAJA.

**Submission (decision 2026-11-16).** Raja turned on automatic submission after the Easy Apply drafts proved reliable. The applier now
clicks Submit itself when the form reached the final review page with every required question answered. It still stops, and does not
submit, for an unanswered question, a hard blocker, a company-site application, or any failed safety check (the dialog must name the
right company and be the final page). A submission is recorded as awaiting LinkedIn's confirmation; only `agent.reconcile`
(Applied tab AND posting page agree) marks it SUBMITTED and sends the "applied" report. If LinkedIn does not confirm within 60 minutes
Raja is told once. The mode is a saved setting: `python -m agent.applier --set-submit on|off` (off = fill, save a draft, stop; this is
also the default if nothing is saved). `python -m agent.submit_drafts <numbers>` submits specific saved drafts on demand.

## Step 7: Outreach
For every application with a named job poster, draft the invite note and email Raja the exact text. Nothing is sent without his approval of the exact text. Voice: short plain sentences, no em dashes, never "thanks" (always "thank you"), sign-off "Thank you, Raja,", invite notes within 300 characters, meeting line exactly "I would love to discuss next steps." Approved sends go through the outreach queue; the sender sends the exact text and settles on LinkedIn's confirmation. Sent invites join the acceptance watch list. On acceptance, draft the follow-up for approval.

**Approval:** each draft carries an ID and goes out in chat and by email. Raja approves by replying `APPROVE <id>` in either place (or replies `APPROVE <id>` followed by edited text). The sender polls Gmail and the chat for replies. Silence is never approval.

## Reporting (chat and email via Gmail)
- Report only submitted applications and settled pauses, with the LinkedIn link for every job and the current New York time. Never report skipped matches or employer rejections.
- No claim is reported until settled on an independent record (Applied list + posting page for LinkedIn; employer confirmation for company sites; a tracker draft for pauses).
- Only the Reporter sends messages. Everything is posted to chat and emailed. Anything needing Raja's action (sign-in or password card, a code only he can supply, an approval, a question) gets a subject ending "— updates needed".
- If a run hits the 60-minute cap, report which run, when it started, and what it completed.
- "What is pending" is answered by a daily digest email and on request (reply `PENDING` to any agent email): paused applications, queued postings, anything awaiting him.

## WhatsApp rules
- Free-form messages are only deliverable within 24 hours of Raja's last message to the sender number. Outside that window only approved templates can be sent.
- Keepalive: every 12 hours the Reporter sends an approved template (template `job_agent_checkin`, Utility: "Account update: your job agent notifications are active. Reply OK to confirm you are receiving them."). Raja's reply, or any message from him, reopens the 24-hour window. A message from the agent does not extend it.
- If WhatsApp cannot deliver (window closed), the same item goes by email only, and is marked so in the outbox. Nothing is lost.
- Reply commands (`APPROVE <id>`, `PENDING`, `OK`) are accepted only from Raja's verified number.

## Credentials
Gmail uses OAuth (one-time consent in the browser); the refresh token is stored in the Windows Credential Manager (DPAPI-encrypted) via `keyring`. The WhatsApp access token (a permanent system-user token) and phone number ID go in the same vault. Other passwords the agent needs (employer-site accounts) go in the same vault. No secrets in the repo or `.env`.
