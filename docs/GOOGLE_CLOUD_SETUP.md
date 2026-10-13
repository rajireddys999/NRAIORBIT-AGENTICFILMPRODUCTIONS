# Google Cloud setup (one time, about 20 minutes)

One Google Cloud project holds everything this film needs, on one account and one bill:

| What | Google service | When |
|---|---|---|
| Video clips | Veo, through the Gemini API key | Trailer test |
| Character photos and first frames | Gemini image, same key | Trailer test |
| Telugu voices | Gemini TTS, same key | Trailer test |
| Backup of paid clips and masters | Cloud Storage | Optional |
| Lip-sync | One NVIDIA L4 GPU machine (Compute Engine) | Later |

Steps 1 to 7 are needed for the trailer test. Steps 8 and 9 come later.

**Before you start**
- Use a **personal Gmail account**. Work or school accounts are often blocked by their
  organisation's rules, which is a common cause of "permission denied".
- Use one browser window with only that account signed in, or an Incognito window.
- Have a **Visa or Mastercard credit card** ready. RuPay and prepaid cards may be refused.

## 1. Create the account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Choose **India** as the country and accept the Terms of Service.
3. Click **Start free** (or **Activate** in the banner at the top).
4. Account type **Individual**. Enter your name, address and card. Google may place a small
   hold on the card to check it; the hold is released, not charged.
5. You get **$300 of free credit for 90 days**.

If the card or billing check fails, use **Help (?) → Billing support** in the console.
Support can offer another way to verify, which Indian individual accounts sometimes need.

## 2. Upgrade to a paid account (do it now)

**☰ menu → Billing → Overview → Activate full account** (or **Activate** in the top banner).

Why now:
- A free-trial account **cannot use GPUs or ask for GPU quota**, which lip-sync needs.
- Forum users report that usage through a Gemini API key is charged to the card, not to
  the $300. If the key reports "free tier" limits, upgrading fixes it.
- You **keep the unused credit**. Without the upgrade, everything stops on day 90.

You stay in control of spending: the pipeline shows the cost and asks before every paid
run, and it stops at your cap. Step 6 adds an email alert as a backup.

## 3. Create the project

1. Click the **project picker** in the top bar → **New project**.
2. Project name `nraiorbit-film`, location **No organization** → **Create**.
3. Select `nraiorbit-film` in the top bar. **Every step below is done inside this project.**
4. **☰ → Billing**. If it says "This project has no billing account", click **Link a
   billing account**, choose your account and click **Set account**.

## 4. Turn on the Gemini API

**☰ → APIs & Services → Library** → search **Generative Language API** → **Enable**.

This is the Gemini API. Veo, Gemini image and Gemini TTS all go through it.

## 5. Create the API key

1. **☰ → APIs & Services → Credentials → + Create credentials → API key**.
2. Click **Edit API key**. Name it `nraiorbit-gemini`.
3. Under **API restrictions** choose **Restrict key**, tick **Generative Language API**,
   then **Save**. A restricted key can't be used for other Google services if it leaks.
4. Click **Show key** and copy it. It starts with `AIza`.

**Never paste the key into a chat, an email, or any file in this repository.**

Optional: on [aistudio.google.com](https://aistudio.google.com), **Get API key → Import
project** → `nraiorbit-film` shows your usage there. If AI Studio says "permission
denied", skip this; the key still works.

## 6. Budget alert

**☰ → Billing → Budgets & alerts → Create budget**:
- Name `nraiorbit POC`. Scope: project `nraiorbit-film`, all services.
- Amount **$100** (enter the rupee amount if your billing account is in INR).
- Alerts at **50%, 90% and 100%**, emailed to billing admins.

**A budget alert only sends an email; it does not stop spending.** The pipeline's cap
(`--max-spend`, and `budget.max_usd` in the project) is what stops it.

## 7. Save the key where the pipeline can read it

**Claude Code cloud session.** Open the cloud environment menu in the session's title
bar → **Edit**:
- **Environment variables:** `GEMINI_API_KEY=` followed by your key.
- **Network access:** allow `pypi.org`, `files.pythonhosted.org` and
  `generativelanguage.googleapis.com`, with **Allow package managers** ticked
  ([steps](https://code.claude.com/docs/en/cloud-environments#network-access)).
- Start a **new session**, which picks up the change, and paste the prompt from
  [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md).

**Your Mac:**
```bash
export GEMINI_API_KEY='AIza...'      # this terminal only; add the line to ~/.zshrc to keep it
nraiorbit google models              # free: lists the Veo, image and TTS models your key can use
```

## 8. Later: the lip-sync GPU (not needed for the trailer test)

1. **☰ → APIs & Services → Library → Compute Engine API → Enable**.
2. **☰ → IAM & Admin → Quotas & system limits**. Search for each quota, tick it,
   click **Edit** and ask for **1**:
   - **GPUs (all regions)**
   - **NVIDIA L4 GPUs** in region **asia-south1** (Mumbai)

   Reason: "One L4 GPU to lip-sync a short film." Approval takes from minutes to a couple
   of days.
3. The machine will be a **g2-standard-4** (1 × L4), about **$0.74 an hour** in Mumbai.
   Lip-sync for the 5-minute POC is about $1.50. A machine bills every hour it is running,
   even when idle, so **delete it when the job is done**.

## 9. Optional: Cloud Storage backup

Every clip you pay for is already saved where the pipeline runs (`projects/<slug>/inbox/`).
For a copy off that machine:

1. **☰ → Cloud Storage → Buckets → Create**. Name: anything unique, e.g.
   `nraiorbit-film-media-<yourname>`. Location type **Region**, **asia-south1 (Mumbai)**.
   Storage class **Standard**. Keep **Prevent public access** on → **Create**.
2. Copy from your Mac:
   ```bash
   brew install --cask google-cloud-sdk
   gcloud auth login
   gcloud storage cp -r projects/godavari5/inbox projects/godavari5/11_master gs://<bucket>/godavari5/
   ```

It costs about $0.023 per GB per month. Downloading back to your Mac costs about $0.12 per GB.

## Costs and tax

- Indian Google Cloud bills add **18% GST**. The pipeline's estimates and caps are before tax.
  For example, the trailer test is about $56 + GST ≈ $66.
- Prices come from third-party trackers (2026-10). Check Google's own pricing pages.

## If something goes wrong

| You see | Fix |
|---|---|
| "Permission denied" creating a project or key | Personal Gmail with only that account signed in; accept the Terms of Service; otherwise Billing support |
| `API key not valid` | The key is restricted to the wrong API, or Generative Language API is not enabled in the same project (steps 4–5) |
| "Free tier" limits, `429` or quota errors | Billing is not linked to the project, or the account is still a trial (steps 2–3) |
| No Veo model in `nraiorbit google models` | Billing not active on the project; check steps 2–3, then try again |
| GPU quota request refused | The account is still a trial (step 2), or ask for a different zone in asia-south1 |

Sources: [Google Cloud free trial rules](https://docs.cloud.google.com/free/docs/free-cloud-features.md.txt),
[Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key),
[forum: AI Studio and Cloud credits](https://discuss.ai.google.dev/t/google-ai-studio-do-not-use-my-1300-credits-shown-on-google-cloud/140442),
[forum: Indian individual billing](https://discuss.google.dev/t/being-an-indian-individual-how-can-i-setup-and-verify-billing-gcp-account/173113),
[GCP free tier in India (third party)](https://unicoconnect.com/blogs/google-cloud-free-tier-credits-india),
[g2-standard-4 prices](https://gcloud-compute.com/g2-standard-4.html).
