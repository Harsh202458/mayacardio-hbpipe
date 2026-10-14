# Deploying the cloud side for free (no credit card)

The cloud runs as a **Render free web service** with storage in **Neon free Postgres**. Neither
needs a card. It takes about 20 minutes.

## 1. GitHub (hosts the code; also the submission repository)
1. Create an account at https://github.com (email only).
2. Create a **new public repository**, e.g. `mayacardio-hbpipe`. Don't add a README.
3. Upload this project. Either:
   - drag and drop all files on the repo's "uploading an existing file" page; or
   - run `git push`. The commands are printed by GitHub after you create the repo.

## 2. Neon (Postgres database)
1. Sign up at https://neon.tech with your GitHub account.
2. Create a project. Region: pick the one closest to Render's **Oregon** or **Frankfurt**,
   e.g. *AWS US West 2 (Oregon)*. Postgres 16.
3. Copy the **connection string**. It looks like
   `postgresql://user:password@ep-xxxx.us-west-2.aws.neon.tech/neondb?sslmode=require`.
   Keep it secret.

## 3. Render (web service)
1. Sign up at https://render.com with your GitHub account.
2. **New → Blueprint**, select your repository. Render reads `render.yaml`.
3. When asked for `HB_CLOUD_DB`, paste the Neon connection string.
   `HB_CLOUD_TOKEN` is generated automatically.
4. Pick the region matching Neon (Oregon), then **Apply**. The first build takes ~3 minutes.
5. Open `https://<your-service>.onrender.com/health`. It should return `{"ok": true, ...}`.
6. In Render → your service → **Environment**, reveal and copy `HB_CLOUD_TOKEN`.

## 4. Point the device at the cloud
- **Raspberry Pi (judges):**
  ```
  sudo ./deploy/install-device.sh https://<your-service>.onrender.com <HB_CLOUD_TOKEN>
  ```
- **ARM64 Docker device (our tests):** pass the same values as environment variables:
  ```
  docker run -d --name hb-dev --platform linux/arm64 --cpus 4 --memory 2g -v hb-data:/data \
    -e HB_CLOUD_URL=https://<your-service>.onrender.com -e HB_CLOUD_TOKEN=<token> hbpipe-device
  ```

## Notes on the free tiers
- **Render free sleeps after 15 minutes without requests** and takes ~1 minute to wake. The
  device pings every second, so it stays awake while the device runs. **Before judging, open
  the `/health` URL once** to wake it. While it is waking, the device simply runs in offline mode
  and then catches up, which is exactly the designed behaviour.
- **Neon free** gives 0.5 GB of storage, enough for ≈ 15 hours of recordings (≈ 30 MB/h). It
  suspends when idle and wakes in about a second.
- Neither service has a payment method attached, so no charges are possible. This satisfies the
  "free services only" rule.
