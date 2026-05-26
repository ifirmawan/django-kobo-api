import uuid

import requests
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse

from .models import DraftAttachment, SubmissionDraft, SubmissionJob, SubmissionLog
from .services import (
    FAKER_TYPE_CHOICES,
    DataGenerator,
    KoboClient,
    build_submission_xml,
    parse_schema,
)
from .tasks import run_submission_task


class SubmissionLogInline(admin.TabularInline):
    model = SubmissionLog
    extra = 0
    readonly_fields = ("instance_id", "status_code", "error_detail", "created_at")
    can_delete = False


class DraftAttachmentInline(admin.TabularInline):
    model = DraftAttachment
    extra = 0


@admin.register(SubmissionJob)
class SubmissionJobAdmin(admin.ModelAdmin):
    change_form_template = "admin/dummygenerator/submissionjob/change_form.html"
    list_display = (
        "form_uid",
        "count_requested",
        "count_succeeded",
        "count_failed",
        "status",
        "celery_task_id",
        "created_at",
    )
    readonly_fields = (
        "status",
        "count_succeeded",
        "count_failed",
        "celery_task_id",
        "completed_at",
        "schema_snapshot",
    )
    exclude = ("field_faker_map",)
    inlines = [SubmissionLogInline]
    actions = ["run_bulk_submission", "refresh_schema", "generate_drafts"]

    change_list_template = "admin/dummygenerator/submissionjob/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "form-browser/",
                self.admin_site.admin_view(self.form_browser_view),
                name="dummygenerator_form_browser",
            )
        ]
        return custom + urls

    def get_changeform_initial_data(self, request):
        data = super().get_changeform_initial_data(request)
        if uid := request.GET.get("form_uid"):
            data["form_uid"] = uid
        return data

    def form_browser_view(self, request):
        forms = []
        error = None
        try:
            forms = KoboClient.from_env().list_forms()
        except (requests.HTTPError, requests.ConnectionError, KeyError) as exc:
            error = str(exc)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Kobo Form Browser",
            "forms": forms,
            "error": error,
        }
        return render(request, "admin/dummygenerator/submissionjob/form_browser.html", context)

    def save_model(self, request, obj, form, change):
        if not change:
            client = KoboClient.from_env()
            try:
                api_response = client.fetch_schema(obj.form_uid)
                obj.schema_snapshot = parse_schema(api_response)
            except requests.HTTPError as exc:
                raise ValidationError(f"Failed to fetch schema from Kobo API: {exc}")

        field_faker_map: dict = {}
        for group in obj.schema_snapshot.get("groups", []):
            for field in group.get("fields", []):
                if field.get("type") == "text":
                    value = request.POST.get(f"faker_{field['name']}", "")
                    if value:
                        field_faker_map[field["name"]] = value
        obj.field_faker_map = field_faker_map

        super().save_model(request, obj, form, change)

    def _build_faker_context(self, schema_snapshot: dict, field_faker_map: dict) -> dict:
        faker_fields = []
        for group in schema_snapshot.get("groups", []):
            for field in group.get("fields", []):
                faker_fields.append({
                    "name": field["name"],
                    "type": field["type"],
                    "is_text": field.get("type") == "text",
                    "current_value": field_faker_map.get(field["name"], ""),
                })
        return {"faker_fields": faker_fields, "faker_type_choices": FAKER_TYPE_CHOICES}

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            obj = self.get_object(request, object_id)
            if obj and obj.schema_snapshot:
                extra_context.update(
                    self._build_faker_context(obj.schema_snapshot, obj.field_faker_map or {})
                )
        elif request.method == "GET":
            form_uid = request.GET.get("form_uid", "").strip()
            if form_uid:
                try:
                    api_response = KoboClient.from_env().fetch_schema(form_uid)
                    schema_snapshot = parse_schema(api_response)
                    extra_context.update(self._build_faker_context(schema_snapshot, {}))
                except (requests.HTTPError, requests.ConnectionError, KeyError):
                    pass
        return super().changeform_view(request, object_id, form_url, extra_context)

    @admin.action(description="Run bulk submission")
    def run_bulk_submission(self, request, queryset):
        for job in queryset:
            if job.status != "pending":
                self.message_user(
                    request,
                    f"Job {job.id} is not pending (status={job.status}), skipped.",
                    level="warning",
                )
                continue
            job.status = "running"
            job.save(update_fields=["status"])
            task = run_submission_task.delay(job.id)
            job.celery_task_id = task.id
            job.save(update_fields=["celery_task_id"])
            self.message_user(request, f"Job {job.id} queued — task {task.id}")

    @admin.action(description="Refresh schema from Kobo API")
    def refresh_schema(self, request, queryset):
        client = KoboClient.from_env()
        count = 0
        for job in queryset:
            try:
                api_response = client.fetch_schema(job.form_uid)
                job.schema_snapshot = parse_schema(api_response)
                job.save(update_fields=["schema_snapshot"])
                count += 1
            except requests.HTTPError as exc:
                self.message_user(
                    request,
                    f"Job {job.id}: failed to refresh schema — {exc}",
                    level="error",
                )
        if count:
            self.message_user(request, f"Refreshed schema for {count} job(s).")

    @admin.action(description="Generate drafts")
    def generate_drafts(self, request, queryset):
        for job in queryset:
            snapshot = job.schema_snapshot
            groups = snapshot.get("groups", [])
            choices_map = snapshot.get("choices_map", {})
            field_faker_map = job.field_faker_map or {}
            generator = DataGenerator(choices_map=choices_map)

            for _ in range(job.count_requested):
                field_values: dict = {}
                for group in groups:
                    for field in group.get("fields", []):
                        if field.get("is_file"):
                            continue
                        field_values[field["name"]] = generator.generate(
                            field_type=field["type"],
                            list_name=field.get("list_name"),
                            faker_type=field_faker_map.get(field["name"]),
                        )
                SubmissionDraft.objects.create(job=job, field_values=field_values)

            self.message_user(request, f"{job.count_requested} drafts created for job {job.id}.")


@admin.register(SubmissionLog)
class SubmissionLogAdmin(admin.ModelAdmin):
    list_display = ("job", "instance_id", "status_code", "created_at")
    readonly_fields = ("job", "instance_id", "status_code", "error_detail", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(SubmissionDraft)
class SubmissionDraftAdmin(admin.ModelAdmin):
    list_display = ("job", "status", "created_at")

    def has_add_permission(self, request):
        return False
    readonly_fields = ("status", "created_at")
    inlines = [DraftAttachmentInline]
    actions = ["submit_drafts"]

    @admin.action(description="Submit drafts to KoboToolbox")
    def submit_drafts(self, request, queryset):
        client = KoboClient.from_env()
        for draft in queryset:
            if draft.status != "pending":
                self.message_user(
                    request,
                    f"Draft {draft.id} is not pending (status={draft.status}), skipped.",
                    level="warning",
                )
                continue

            job = draft.job
            snapshot = job.schema_snapshot
            groups = snapshot.get("groups", [])
            form_uid = snapshot.get("form_uid", job.form_uid)
            version_id = snapshot.get("version_id", "")

            field_values = dict(draft.field_values)
            attachments: dict[str, bytes] = {}
            for attachment in draft.attachments.all():
                filename = attachment.file.name.split("/")[-1]
                field_values[attachment.field_name] = filename
                with attachment.file.open("rb") as f:
                    attachments[filename] = f.read()

            instance_id = str(uuid.uuid4())
            xml_bytes = build_submission_xml(
                form_uid, version_id, groups, field_values, instance_id=instance_id
            )

            try:
                status_code, response_text = client.submit_xml(xml_bytes, attachments=attachments)
            except requests.ConnectionError as exc:
                SubmissionLog.objects.create(
                    job=job,
                    instance_id=instance_id,
                    status_code=None,
                    error_detail=str(exc),
                )
                job.count_failed += 1
                job.save(update_fields=["count_failed"])
                self.message_user(request, f"Draft {draft.id}: connection error — {exc}", level="error")
                continue

            success = status_code == 201
            SubmissionLog.objects.create(
                job=job,
                instance_id=instance_id,
                status_code=status_code,
                error_detail="" if success else response_text,
            )
            if success:
                draft.status = "submitted"
                draft.save(update_fields=["status"])
                job.count_succeeded += 1
            else:
                job.count_failed += 1
                self.message_user(request, f"Draft {draft.id}: HTTP {status_code}", level="warning")

            job.save(update_fields=["count_succeeded", "count_failed"])
