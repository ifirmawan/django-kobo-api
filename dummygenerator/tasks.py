import logging
import uuid
from datetime import datetime, timezone

import requests
from celery import shared_task

from .models import SubmissionJob, SubmissionLog
from .services import DataGenerator, KoboClient, build_submission_xml

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def run_submission_task(self, job_id: int) -> dict:
    """Process all submissions for a SubmissionJob."""
    try:
        job = SubmissionJob.objects.get(pk=job_id)
    except SubmissionJob.DoesNotExist:
        logger.error("SubmissionJob %d not found", job_id)
        return {"error": "job not found"}

    if job.status != "running":
        logger.warning("Job %d has status=%s, expected running — skipping", job_id, job.status)
        return {"skipped": True}

    snapshot = job.schema_snapshot
    field_faker_map = job.field_faker_map or {}
    groups = snapshot.get("groups", [])
    choices_map = snapshot.get("choices_map", {})
    form_uid = snapshot.get("form_uid", job.form_uid)
    version_id = snapshot.get("version_id", "")

    client = KoboClient.from_env()
    generator = DataGenerator(choices_map=choices_map)

    connection_error = False
    for _ in range(job.count_requested):
        field_values: dict[str, str] = {}
        for group in groups:
            for field in group.get("fields", []):
                if field.get("is_file"):
                    continue
                field_values[field["name"]] = generator.generate(
                    field_type=field["type"],
                    list_name=field.get("list_name"),
                    faker_type=field_faker_map.get(field["name"]),
                )

        instance_id = str(uuid.uuid4())
        xml_bytes = build_submission_xml(form_uid, version_id, groups, field_values, instance_id=instance_id)

        try:
            status_code, response_text = client.submit_xml(xml_bytes)
        except requests.ConnectionError as exc:
            logger.error("Connection error for job %d: %s", job_id, exc)
            SubmissionLog.objects.create(
                job=job,
                instance_id=instance_id,
                status_code=None,
                error_detail=str(exc),
            )
            job.count_failed += 1
            connection_error = True
            break

        success = status_code == 201
        SubmissionLog.objects.create(
            job=job,
            instance_id=instance_id,
            status_code=status_code,
            error_detail="" if success else response_text,
        )
        if success:
            job.count_succeeded += 1
        else:
            logger.warning("HTTP %d for job %d instance %s", status_code, job_id, instance_id)
            job.count_failed += 1

    if connection_error:
        job.status = "failed"
    elif job.count_failed > 0:
        job.status = "partial"
    else:
        job.status = "done"

    job.completed_at = datetime.now(tz=timezone.utc)
    job.save()

    return {"succeeded": job.count_succeeded, "failed": job.count_failed}
