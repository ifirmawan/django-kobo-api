from django.db import models

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("running", "Running"),
    ("done", "Done"),
    ("partial", "Partial"),
    ("failed", "Failed"),
]

DRAFT_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("submitted", "Submitted"),
    ("skipped", "Skipped"),
]


class SubmissionJob(models.Model):
    form_uid = models.CharField(max_length=64)
    count_requested = models.PositiveSmallIntegerField()
    count_succeeded = models.PositiveSmallIntegerField(default=0)
    count_failed = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    schema_snapshot = models.JSONField(default=dict, blank=True)
    field_faker_map = models.JSONField(default=dict, blank=True)
    celery_task_id = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Job {self.id} — {self.form_uid} ({self.status})"


class SubmissionLog(models.Model):
    job = models.ForeignKey(SubmissionJob, on_delete=models.CASCADE, related_name="logs")
    instance_id = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True)
    error_detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Log {self.id} — job {self.job_id} — {self.status_code}"


class SubmissionDraft(models.Model):
    job = models.ForeignKey(SubmissionJob, on_delete=models.CASCADE, related_name="drafts")
    field_values = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=DRAFT_STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Draft {self.id} — job {self.job_id} ({self.status})"


class DraftAttachment(models.Model):
    draft = models.ForeignKey(SubmissionDraft, on_delete=models.CASCADE, related_name="attachments")
    field_name = models.CharField(max_length=128)
    file = models.FileField(upload_to="draft_attachments/")

    def __str__(self) -> str:
        return f"Attachment {self.id} — {self.field_name}"
