# Automatic deploys from GitHub

Every merge to `claude/telugu-ai-movie-generator-yhb6po` goes live by itself:

1. CI (`.github/workflows/ci.yml`) tests the merged commit.
2. When CI passes, `.github/workflows/deploy.yml`:
   - builds the runner and control room images from that commit;
   - tags each image with the commit;
   - points the runner job and then the control room at those images.
3. Only the image changes. Settings, secrets and the job's arguments stay as they are, and a tick that is already
   running finishes on the image it started with.
4. The control room's footer shows what is live: `VERSION · CONTROL ROOM <commit> · RUNNER <commit>`.

Nothing deploys until the one-time setup below is done. Until then, the deploy run ends with the note "the one-time
setup is not done".

## One-time setup

1. **Open Cloud Shell.** Go to <https://console.cloud.google.com/> and choose project
   `nraiorbit-agentic-film-product`. Click the terminal icon (Activate Cloud Shell) at the top right.
2. **Get the repository and run the script:**

   ```bash
   git clone https://github.com/rajireddys999/NRAIORBIT-AGENTICFILMPRODUCTIONS.git nraiorbit
   cd nraiorbit && git checkout claude/telugu-ai-movie-generator-yhb6po
   bash deploy/github/setup.sh
   ```

   The script does three things:
   - creates the account `nraiorbit-deployer`, which may only push images to the `nraiorbit` repository and point
     the job and the service at a new image;
   - lets GitHub sign in as that account, but only from `deploy.yml` on the default branch of this repository;
   - stores no keys anywhere.

   It is safe to run again.
3. **Add the three values it prints** as repository variables. In GitHub, open the repository, then Settings >
   Secrets and variables > Actions > **Variables** tab > **New repository variable**:

   | Variable | Value |
   |---|---|
   | `GCP_PROJECT` | `nraiorbit-agentic-film-product` |
   | `GCP_WIF_PROVIDER` | what the script printed, beginning `projects/` |
   | `GCP_DEPLOYER_SA` | `nraiorbit-deployer@nraiorbit-agentic-film-product.iam.gserviceaccount.com` |

4. **Do the first deploy by hand** to prove it works. In GitHub, open Actions > deploy > **Run workflow**, keep the
   branch, leave the commit empty and click Run.
   - When it finishes, its last step prints the two live images.
   - The control room's footer shows the same commit.

## Roll back

Open Actions > deploy > **Run workflow** and enter an earlier commit, full or short. Only a commit that was merged
to the default branch and passed CI is accepted. Its images are built if they are missing, and the job and the
control room are pointed at them.

## Recommended: make CI required

Today nothing in GitHub stops a pull request from being merged while CI is red. To block that:

1. Open Settings > Rules > Rulesets > **New branch ruleset**.
2. Target the default branch.
3. Turn on "Require a pull request before merging" and "Require status checks to pass".
4. Add the check `test`.
5. Leave the bypass list empty.
