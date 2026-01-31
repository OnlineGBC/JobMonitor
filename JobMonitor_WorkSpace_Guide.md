\# JobMonitor: Workspace, Git, and Troubleshooting Guide



Generated: 2026-01-30 22:40 UTC



This document explains the local workspace vs. GitHub repository concepts, how to commit and push changes to main, how to verify which branches are merged, and how to troubleshoot LinkedIn monitoring issues in JobMonitor.



\## 1) Workspace vs. Git repository

Workspace refers to the local folder on your machine where the code lives. Example: `C:\\Users\\raja\\Downloads\\JobMonitor`. This is where you edit files, run scripts, and see logs.



The Git repository refers to the remote copy hosted on GitHub (e.g., `https://github.com/OnlineGBC/JobMonitor`). A local commit does not appear on GitHub until you push it to the remote.



\## 2) Local vs. Remote branches

Local branches live in your workspace. Remote branches live in GitHub under `origin/branch-name`. You can see both with:



```

git branch -a

```



\## 3) Commit to main (local) and push to main (remote)

Steps to commit locally on main:



1\. `git checkout main`

2\. `git status`

3\. `git add <file or .>`

4\. `git commit -m "Describe your change"`



Steps to push to GitHub:



1\. `git push origin main`



\## 4) Verify main tracks origin/main

Run:



```

git branch -vv

```



You should see main showing `\[origin/main]`. If not, set the upstream with:



```

git branch --set-upstream-to=origin/main

```



\## 5) Check if codex/fix\* branches are merged

To list remote branches merged into main:



```

git branch -r --merged origin/main

```



To list remote branches NOT merged into main:



```

git branch -r --no-merged origin/main

```



Delete a merged remote branch:



```

git push origin --delete <branch>

```



\## 6) LinkedIn monitoring behavior (JobMonitor)

JobMonitor uses Playwright to load LinkedIn search pages and extract job cards. If LinkedIn returns a login/challenge page or blocks automation, JobMonitor may log:



\- LinkedIn results container not found

\- Structural check: real\_count=0

\- LinkedIn auth/challenge page detected; skipping update



When this happens, the monitor skips updates to avoid false alerts. Emails won't be sent until real job results load.



\## 7) Troubleshooting steps for LinkedIn

Recommended steps:



\- Set `headless: false` in `monitors.yaml` to use a visible browser.

\- Increase `timeout\_seconds` (e.g., 120) to allow LinkedIn to load.

\- Set `wait\_until: "load"` for full page load.

\- Delete `linkedin\_state.json` and re-login once to refresh cookies.

\- Watch for captcha or verification prompts in the visible browser.



\## 8) Where to add YAML changes

Per-monitor settings like `headless`, `wait\_until`, and `timeout\_seconds` go inside the specific monitor block.



Example:



```

\- name: "RemoteUSA"

&nbsp; url: "https://www.linkedin.com/jobs/search/?..."

&nbsp; headless: false

&nbsp; wait\_until: "load"

&nbsp; timeout\_seconds: 120

&nbsp; wait\_selector: "ul.jobs-search\_\_results-list"

&nbsp; css\_selector: "ul.jobs-search\_\_results-list"

```



\## 9) Extract relevant log lines

PowerShell command to extract only key lines:



```

Select-String -Path .\\logs\\monitor.log -Pattern 'Early skip|Structural check|auth/challenge|No new jobs|Change detected|Email sent|Fetch error' | ForEach-Object { $\_.Line } | Set-Content .\\logs\\monitor\_filtered.log

```



\## 10) About secrets (.env)

Avoid committing .env files or secrets into Git. If you share config for troubleshooting, redact passwords and API keys.



