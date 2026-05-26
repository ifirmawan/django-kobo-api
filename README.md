# KoboAPI Dummy Generator

Generate and bulk-submit realistic dummy data to any KoboToolbox form via the
OpenRosa XML submission API. Submissions are processed asynchronously by a
Celery worker and monitored via Flower.

---

## Quick Start

### 1. Configure credentials

Create a `.env` file in the project root (never commit this file):

```dotenv
KOBO_BASE_URL=https://kf.kobotoolbox.org
KOBO_KC_URL=https://kc.kobotoolbox.org
KOBO_USERNAME=your_username
KOBO_PASSWORD=your_password

CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

### 2. Start the stack

```bash
docker compose up -d --build
```

Services started:

| Service | URL | Purpose |
|---------|-----|---------|
| Django admin | http://localhost:8000/admin/ | Main interface |
| Flower | http://localhost:5555 | Monitor Celery tasks |

### 3. Create an admin user

```bash
docker compose exec web python manage.py createsuperuser
```

---

## Usage Workflows

### Workflow A — Auto Bulk Submission (no file fields)

Generates and submits N records directly to KoboToolbox via Celery.

```
Step 1 → Browse forms and create a job
Step 2 → Configure faker types for text fields
Step 3 → Run bulk submission
Step 4 → Monitor progress
```

**Step 1 — Browse forms and create a job**

1. Go to **Submission jobs** → http://localhost:8000/admin/dummygenerator/submissionjob/
2. Click **Browse Kobo Forms** (top-right button)
3. Find your form in the table → click **Create Job →**
4. The add form opens with `Form uid` pre-filled
5. Set **Count requested** (1–50) → click **Save**

> The form schema is fetched from the Kobo API automatically on save.

**Step 2 — Configure faker types for text fields**

After saving, the **Field Faker Configuration** table appears on the job detail page.

| Column | Description |
|--------|-------------|
| Field name | Question name from your form |
| Type | Field type (`text`, `select_one`, `geopoint`, etc.) |
| Faker type | Choose what kind of fake data to generate |

- Text fields → choose a faker type from the dropdown (name, email, phone, etc.)
- Other field types (`select_one`, `geopoint`, `select_multiple`, etc.) → automatically generated, no configuration needed

Available faker types:

| Value | Example output |
|-------|---------------|
| Name | Jane Smith |
| First name | Jane |
| Last name | Smith |
| Phone number | +1-555-867-5309 |
| Email | jane@example.com |
| Address | 123 Main St, Springfield |
| City | Springfield |
| Country | United States |
| Company | Acme Corp |
| URL | https://example.com |
| Sentence | The quick brown fox. |
| Paragraph | Lorem ipsum dolor sit amet… |
| Number (1–9999) | 4217 |

Click **Save** after setting faker types.

**Step 3 — Run bulk submission**

From the **Submission jobs list** (http://localhost:8000/admin/dummygenerator/submissionjob/):

1. Check the checkbox next to your job
2. Select **Run bulk submission** from the Action dropdown
3. Click **Go**

The job status changes to `running`. The Celery worker processes all submissions in the background.

**Step 4 — Monitor progress**

- **Job status** updates on the detail page: `pending → running → done / partial / failed`
- **Submission logs** appear in the inline table on the job detail page (one row per submission)
- **Flower** at http://localhost:5555 shows real-time Celery task state

| Status | Meaning |
|--------|---------|
| `pending` | Job created, not yet dispatched |
| `running` | Celery task is active |
| `done` | All submissions succeeded (HTTP 201) |
| `partial` | Some submissions failed |
| `failed` | Connection error stopped the loop |

---

### Workflow B — Manual Draft Submission (forms with image / audio / video fields)

Use this workflow when your form has file upload fields that require real attachments.

```
Step 1 → Create a job (same as Workflow A, steps 1–2)
Step 2 → Generate drafts
Step 3 → Upload files to each draft
Step 4 → Submit drafts
```

**Step 2 — Generate drafts**

From the **Submission jobs list**:

1. Check the checkbox next to your job
2. Select **Generate drafts** from the Action dropdown
3. Click **Go**

N draft rows are created under **Submission drafts** with all non-file fields
pre-filled using the faker configuration.

**Step 3 — Upload files to each draft**

1. Go to **Submission drafts** → http://localhost:8000/admin/dummygenerator/submissiondraft/
2. Open a draft
3. In the **Draft attachments** inline section, add one attachment per file field:
   - **Field name**: must match the field name in your form exactly (e.g. `D3_Please_capture_t_ekuhlukubetwe_somiso`)
   - **File**: upload the image/audio/video file

**Step 4 — Submit drafts**

1. Go back to the **Submission drafts** list
2. Check the drafts you want to submit
3. Select **Submit drafts to KoboToolbox** from the Action dropdown
4. Click **Go**

Each draft is submitted as a multipart POST with the XML data and file
attachments. Results are recorded in **Submission logs**.

---

## Admin Pages Reference

| Page | URL | Purpose |
|------|-----|---------|
| Submission jobs | `/admin/dummygenerator/submissionjob/` | Create and manage jobs |
| Form browser | `/admin/dummygenerator/submissionjob/form-browser/` | Browse all Kobo survey forms |
| Submission drafts | `/admin/dummygenerator/submissiondraft/` | View and manage drafts with file uploads |
| Submission logs | `/admin/dummygenerator/submissionlog/` | Per-submission HTTP results |
| Flower | `http://localhost:5555` | Celery task monitor |

---

## Field Types Reference

| Form field type | How dummy data is generated |
|----------------|----------------------------|
| `text` | Configurable via Field Faker Configuration (defaults to a random sentence) |
| `integer` | Random integer 1–100 |
| `decimal` | Random decimal 1.0–100.0 |
| `select_one` | Random choice from the field's choice list |
| `select_multiple` | Random subset of the field's choice list |
| `date` | Random date within the past year |
| `datetime` | Random datetime within the past year |
| `geopoint` | Random latitude/longitude (0 altitude, 0 accuracy) |
| `start` / `end` | Current UTC timestamp |
| `image` / `audio` / `video` | Skipped in auto-submission; requires manual upload in draft workflow |

---

## Actions Reference

### On Submission jobs list

| Action | When to use |
|--------|-------------|
| Run bulk submission | Auto-submit N records to Kobo (no file fields) |
| Generate drafts | Create draft rows for manual file upload workflow |
| Refresh schema | Re-fetch the form schema from Kobo API |

### On Submission drafts list

| Action | When to use |
|--------|-------------|
| Submit drafts to KoboToolbox | Submit pending drafts (with uploaded file attachments) |

---

## Troubleshooting

**Schema fetch fails on job save**
- Check `KOBO_USERNAME` and `KOBO_PASSWORD` in `.env`
- Verify `KOBO_BASE_URL` is reachable from inside the container:
  ```bash
  docker compose exec web curl -I https://kf.kobotoolbox.org
  ```

**Job stays in `running` state**
- Check the Celery worker is running: `docker compose ps`
- View worker logs: `docker compose logs worker`
- Check Flower at http://localhost:5555 for task errors

**Submissions fail with HTTP 401**
- Credentials in `.env` are incorrect or the user lacks submission rights on the form

**Submissions fail with HTTP 404**
- The `KOBO_KC_URL` or form UID is incorrect

**Form browser shows no forms**
- The credentials may not own any deployed survey forms
- Try `Refresh schema` to re-fetch an existing job's schema
