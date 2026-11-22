# Deploying to Google Cloud Run

This puts the app online at a private `https://…run.app` link, hosted in
Madrid (`europe-southwest1`), protected by a password.

How it fits together:

- **Cloud Run** runs the Streamlit app from the `Dockerfile` and scales to
  zero when nobody is using it.
- **Cloud Storage** holds uploads. The browser sends files straight to a
  private bucket, which avoids Cloud Run's 32 MB request limit. Files are
  deleted after transcription, and a lifecycle rule deletes anything left
  over after one day.
- **Secret Manager** holds the AssemblyAI key and the app password.

The commands below are for **PowerShell on Windows**. In Claude Code you can
say *"follow deploy/README.md to deploy the app"* and let it run them.

---

## 0. One-time setup on your computer

1. Create a Google Cloud account and a billing account at
   <https://console.cloud.google.com> (a card is required; new accounts get free
   credits).
2. Install the Google Cloud CLI: <https://cloud.google.com/sdk/docs/install>
3. Sign in (opens your browser):

   ```powershell
   gcloud auth login
   ```

## 1. Choose names

```powershell
$PROJECT = "fabrizio-transcription"          # must be globally unique; change if taken
$REGION  = "europe-southwest1"               # Madrid
$BUCKET  = "$PROJECT-uploads"                # must be globally unique
$SA      = "transcription-app@$PROJECT.iam.gserviceaccount.com"
```

## 2. Create the project and turn on the services

```powershell
gcloud projects create $PROJECT
gcloud billing accounts list                  # copy the ACCOUNT_ID
gcloud billing projects link $PROJECT --billing-account=ACCOUNT_ID
gcloud config set project $PROJECT

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com iamcredentials.googleapis.com
```

**Set a budget alert now** (for example €10/month): Console → Billing →
Budgets & alerts.

## 3. Service account the app runs as

```powershell
gcloud iam service-accounts create transcription-app --display-name="Transcription app"

# Lets the app sign the short-lived upload links (signing goes through IAM)
gcloud iam service-accounts add-iam-policy-binding $SA --member="serviceAccount:$SA" --role="roles/iam.serviceAccountTokenCreator"
```

## 4. Private upload bucket

```powershell
gcloud storage buckets create "gs://$BUCKET" --location=$REGION --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$BUCKET" --cors-file=deploy/cors.json --lifecycle-file=deploy/lifecycle.json
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
```

The bucket stays private. `cors.json` only lets browsers *send* uploads, and
each upload still needs a signed link that the app issues per session (valid
for 2 hours, only for that session's folder).

## 5. Secrets

Write each value to a temporary file, store it, then delete the file:

```powershell
Set-Content -NoNewline -Path key.txt -Value "YOUR_ASSEMBLYAI_KEY"
gcloud secrets create assemblyai-api-key --data-file=key.txt
Set-Content -NoNewline -Path key.txt -Value "CHOOSE_A_PASSWORD"
gcloud secrets create app-password --data-file=key.txt
Remove-Item key.txt

gcloud secrets add-iam-policy-binding assemblyai-api-key --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding app-password --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

## 6. Deploy

Run from the repository folder. The first build takes 10–15 minutes (it
installs PyTorch and bakes in the Whisper models). Later builds are faster.

```powershell
gcloud run deploy transcription-app --source . --region $REGION --service-account $SA --allow-unauthenticated --memory 4Gi --cpu 2 --cpu-boost --min-instances 0 --max-instances 1 --session-affinity --timeout 3600 --set-env-vars "GCS_UPLOAD_BUCKET=$BUCKET" --set-secrets "ASSEMBLYAI_API_KEY=assemblyai-api-key:latest,APP_PASSWORD=app-password:latest"
```

When it finishes it prints the `Service URL`. Open it and enter your password.

**Settings explained:** `--allow-unauthenticated` lets the page load so the
app's own password screen can appear. `--max-instances 1` and
`--session-affinity` keep each user on the same server, which Streamlit
needs. `--timeout 3600` is Cloud Run's maximum: one connection lasts at most
60 minutes, so a single very long Whisper job can be cut off (AssemblyAI is
fast enough not to be affected).

## Updating the app later

Commit your changes, then run the same `gcloud run deploy …` command from
step 6 again (after setting the variables from step 1).

To change a secret:

```powershell
Set-Content -NoNewline -Path key.txt -Value "NEW_VALUE"
gcloud secrets versions add app-password --data-file=key.txt
Remove-Item key.txt
```

Then redeploy so the app picks it up.

## Costs

You pay only while the app is open in a browser: an open tab keeps a
connection alive, so close it when you're done. Cloud Run's monthly free tier
covers roughly 25 hours of use at 2 vCPU / 4 GiB. Beyond that it is roughly
US$0.20 per hour. Storage is cents, because files are deleted quickly.

## Optional changes

- **Larger Whisper models:** `medium` needs about 8 GiB. Add it to
  `WHISPER_MODELS_ALLOWED` and the download line in the `Dockerfile`, and
  deploy with `--memory 8Gi`.
- **Upload size cap:** 1 GB per file by default. Set the `MAX_UPLOAD_MB`
  environment variable to change it, and keep it well below the memory setting.
- **Google sign-in instead of a password:** if the project belongs to a Google
  Cloud organization (for example IESE's), you can turn on Identity-Aware Proxy
  in the Cloud Run service's *Security* tab. Then deploy with
  `--no-allow-unauthenticated` and drop `APP_PASSWORD`.

## Troubleshooting

- **`allUsers` / organization policy error on deploy:** your organization
  blocks public services. Replace `--allow-unauthenticated` with
  `--no-invoker-iam-check`, or use IAP (above).
- **Build fails with a permission error on a brand-new project:** grant the
  build account the builder role, then retry:
  `gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com" --role="roles/run.builder"`
- **"Cloud upload is not available" in the app:** usually step 3's
  token-creator role or the `iamcredentials` API is missing.
- **Upload shows "HTTP 403":** the upload link expired (reload the page) or
  the bucket permissions in step 4 are missing.
- **Logs:** `gcloud run services logs read transcription-app --region $REGION`
