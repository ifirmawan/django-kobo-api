import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase

from .models import SubmissionJob
from .services import DataGenerator, build_submission_xml, parse_schema
from .tasks import run_submission_task

IKS_JSON = Path(settings.BASE_DIR) / "docs" / "kobo-forms" / "iks.json"


def _load_iks() -> dict:
    with open(IKS_JSON) as f:
        return json.load(f)


class ParseSchemaTests(TestCase):
    def setUp(self):
        self.schema = parse_schema(_load_iks())

    def test_parse_schema_iks_group_count(self):
        named = [g for g in self.schema["groups"] if g["name"] != "__root__"]
        self.assertEqual(len(named), 4)
        root = [g for g in self.schema["groups"] if g["name"] == "__root__"]
        self.assertEqual(len(root), 1)

    def test_parse_schema_iks_list_names(self):
        expected = {"do2ug34", "bz2rx27", "bu8bi51", "du7ql49", "xq24n12", "fg74l33", "yc7cj58", "lh9ot52"}
        self.assertEqual(set(self.schema["choices_map"].keys()), expected)

    def test_parse_schema_iks_form_uid(self):
        self.assertEqual(self.schema["form_uid"], "a3ytas3GLhSewNTZByCCsd")

    def test_parse_schema_iks_version_id(self):
        self.assertNotEqual(self.schema["version_id"], "")


class DataGeneratorTests(TestCase):
    def setUp(self):
        schema = parse_schema(_load_iks())
        self.generator = DataGenerator(choices_map=schema["choices_map"])
        self.choices_map = schema["choices_map"]

    def test_data_generator_select_one(self):
        list_name = "do2ug34"
        result = self.generator.generate("select_one", list_name)
        self.assertIn(result, self.choices_map[list_name])

    def test_data_generator_select_multiple(self):
        list_name = "do2ug34"
        result = self.generator.generate("select_multiple", list_name)
        self.assertTrue(result)
        for token in result.split(" "):
            self.assertIn(token, self.choices_map[list_name])

    def test_data_generator_text_default(self):
        result = self.generator.generate("text", None)
        self.assertTrue(result)

    def test_data_generator_text_faker_type(self):
        result = self.generator.generate("text", None, faker_type="email")
        self.assertIn("@", result)

    def test_data_generator_integer(self):
        result = self.generator.generate("integer", None)
        self.assertTrue(result.isdigit())

    def test_data_generator_geopoint(self):
        result = self.generator.generate("geopoint", None)
        parts = result.split(" ")
        self.assertEqual(len(parts), 4)

    def test_data_generator_unknown_returns_empty(self):
        result = self.generator.generate("unknown_type", None)
        self.assertEqual(result, "")

    def test_data_generator_missing_list_name_returns_empty(self):
        result = self.generator.generate("select_one", "nonexistent_list")
        self.assertEqual(result, "")


class XmlBuilderTests(TestCase):
    def setUp(self):
        self.schema = parse_schema(_load_iks())
        self.groups = self.schema["groups"]
        self.form_uid = self.schema["form_uid"]
        self.version_id = self.schema["version_id"]

    def _generate_values(self) -> dict[str, str]:
        generator = DataGenerator(choices_map=self.schema["choices_map"])
        values: dict[str, str] = {}
        for group in self.groups:
            for field in group["fields"]:
                if field.get("is_file"):
                    continue
                values[field["name"]] = generator.generate(
                    field["type"], field.get("list_name")
                )
        return values

    def test_build_submission_xml_structure(self):
        xml_bytes = build_submission_xml(
            self.form_uid, self.version_id, self.groups, self._generate_values()
        )
        root = ET.fromstring(xml_bytes.decode("utf-8").split("\n", 1)[-1])
        self.assertEqual(root.attrib["id"], self.form_uid)
        self.assertEqual(root.attrib["version"], self.version_id)
        meta = root.find("meta")
        self.assertIsNotNone(meta)
        instance_id = meta.find("instanceID")
        self.assertIsNotNone(instance_id)
        self.assertTrue(instance_id.text.startswith("uuid:"))

    def test_build_submission_xml_groups_nested(self):
        xml_bytes = build_submission_xml(
            self.form_uid, self.version_id, self.groups, self._generate_values()
        )
        root = ET.fromstring(xml_bytes.decode("utf-8").split("\n", 1)[-1])
        group_el = root.find("group_yc81a25")
        self.assertIsNotNone(group_el)

    def test_build_submission_xml_skips_empty(self):
        field_values = {"start": "2024-01-01T00:00:00+00:00"}
        xml_bytes = build_submission_xml(
            self.form_uid, self.version_id, self.groups, field_values
        )
        root = ET.fromstring(xml_bytes.decode("utf-8").split("\n", 1)[-1])
        group_el = root.find("group_yc81a25")
        self.assertIsNotNone(group_el)
        self.assertEqual(len(list(group_el)), 0)

    def test_build_submission_xml_custom_instance_id(self):
        custom_id = "test-uuid-1234"
        xml_bytes = build_submission_xml(
            self.form_uid, self.version_id, self.groups, {}, instance_id=custom_id
        )
        root = ET.fromstring(xml_bytes.decode("utf-8").split("\n", 1)[-1])
        instance_id_el = root.find("meta/instanceID")
        self.assertEqual(instance_id_el.text, f"uuid:{custom_id}")


class SubmissionTaskTests(TestCase):
    def _make_job(self) -> SubmissionJob:
        schema = parse_schema(_load_iks())
        return SubmissionJob.objects.create(
            form_uid=schema["form_uid"],
            count_requested=3,
            status="running",
            schema_snapshot=schema,
        )

    @patch("dummygenerator.tasks.KoboClient.from_env")
    def test_run_submission_task_success(self, mock_from_env):
        mock_client = MagicMock()
        mock_client.submit_xml.return_value = (201, "")
        mock_from_env.return_value = mock_client

        job = self._make_job()
        result = run_submission_task.apply(args=[job.id]).get()

        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(job.count_succeeded, 3)
        self.assertEqual(job.count_failed, 0)
        self.assertEqual(result["succeeded"], 3)

    @patch("dummygenerator.tasks.KoboClient.from_env")
    def test_run_submission_task_partial(self, mock_from_env):
        mock_client = MagicMock()
        mock_client.submit_xml.side_effect = [(201, ""), (500, "server error"), (500, "server error")]
        mock_from_env.return_value = mock_client

        job = self._make_job()
        run_submission_task.apply(args=[job.id]).get()

        job.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.assertEqual(job.count_succeeded, 1)
        self.assertEqual(job.count_failed, 2)

    @patch("dummygenerator.tasks.KoboClient.from_env")
    def test_run_submission_task_skips_non_running(self, mock_from_env):
        mock_client = MagicMock()
        mock_from_env.return_value = mock_client

        schema = parse_schema(_load_iks())
        job = SubmissionJob.objects.create(
            form_uid=schema["form_uid"],
            count_requested=2,
            status="pending",
            schema_snapshot=schema,
        )
        result = run_submission_task.apply(args=[job.id]).get()
        self.assertEqual(result, {"skipped": True})
        mock_client.submit_xml.assert_not_called()
