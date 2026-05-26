import os
import random
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import requests
from faker import Faker

FAKER_TYPE_CHOICES = [
    ("name", "Name"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("phone_number", "Phone number"),
    ("email", "Email"),
    ("address", "Address"),
    ("city", "City"),
    ("country", "Country"),
    ("company", "Company"),
    ("url", "URL"),
    ("sentence", "Sentence"),
    ("paragraph", "Paragraph"),
    ("random_int", "Number (1–9999)"),
]

_ALWAYS_SKIP = {"note", "begin_repeat", "end_repeat"}
_FILE_TYPES = {"image", "audio", "video", "file"}


class KoboClient:
    def __init__(self, base_url: str, kc_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.kc_url = kc_url.rstrip("/")
        self._auth = requests.auth.HTTPBasicAuth(username, password)

    @classmethod
    def from_env(cls) -> "KoboClient":
        return cls(
            base_url=os.environ["KOBO_BASE_URL"],
            kc_url=os.environ["KOBO_KC_URL"],
            username=os.environ["KOBO_USERNAME"],
            password=os.environ["KOBO_PASSWORD"],
        )

    def fetch_schema(self, form_uid: str) -> dict:
        """GET /api/v2/assets/{form_uid}/?format=json — raises HTTPError on failure."""
        url = f"{self.base_url}/api/v2/assets/{form_uid}/?format=json"
        response = requests.get(url, auth=self._auth, timeout=30)
        response.raise_for_status()
        return response.json()

    def list_forms(self) -> list[dict]:
        """GET /api/v2/assets/?asset_type=survey&format=json — follows pagination.

        Returns one dict per form with keys:
            uid, name, date_modified, has_deployment, deployment__submission_count.
        Raises requests.HTTPError on non-2xx.
        """
        _KEEP = {"uid", "name", "date_modified", "has_deployment", "deployment__submission_count"}
        url: Optional[str] = (
            f"{self.base_url}/api/v2/assets/?asset_type=survey&format=json"
        )
        results: list[dict] = []
        while url:
            response = requests.get(url, auth=self._auth, timeout=30)
            response.raise_for_status()
            data = response.json()
            for form in data.get("results", []):
                results.append({k: form.get(k) for k in _KEEP})
            url = data.get("next")
        return results

    def submit_xml(
        self,
        xml_bytes: bytes,
        attachments: Optional[dict[str, bytes]] = None,
    ) -> tuple[int, str]:
        """POST xml_bytes to {kc_url}/api/v1/submissions as multipart."""
        url = f"{self.kc_url}/api/v1/submissions"
        files: dict = {"xml_submission_file": ("submission.xml", xml_bytes, "text/xml")}
        if attachments:
            for filename, content in attachments.items():
                files[filename] = (filename, content)
        response = requests.post(url, auth=self._auth, files=files, timeout=30)
        return response.status_code, response.text


def parse_schema(api_response: dict, keep_file_types: bool = True) -> dict:
    """Extract groups, fields, and choices_map from a Kobo asset API response.

    Args:
        api_response: Raw JSON dict from GET /api/v2/assets/{uid}/.
        keep_file_types: When True, image/audio/video/file fields are retained
            with ``is_file: True`` so the draft-upload path can reference them.
            When False, those types are skipped entirely.
    """
    form_uid = api_response.get("uid", "")
    version_id = api_response.get("version_id", "")
    survey = api_response.get("content", {}).get("survey", [])
    choices_raw = api_response.get("content", {}).get("choices", [])

    choices_map: dict[str, list[str]] = {}
    for choice in choices_raw:
        list_name = choice.get("list_name", "")
        name = choice.get("name", "")
        if list_name and name:
            choices_map.setdefault(list_name, []).append(name)

    root_group: dict = {"name": "__root__", "fields": []}
    named_groups: dict[str, dict] = {}
    ordered_groups: list[dict] = []
    group_stack: list[str] = []

    def _current_group() -> dict:
        return named_groups[group_stack[-1]] if group_stack else root_group

    for row in survey:
        row_type = row.get("type", "")
        row_name = row.get("$autoname") or row.get("name", "")

        if row_type == "begin_group":
            grp_name = row.get("name") or row_name
            if grp_name not in named_groups:
                grp: dict = {"name": grp_name, "fields": []}
                named_groups[grp_name] = grp
                ordered_groups.append(grp)
            group_stack.append(grp_name)
        elif row_type == "end_group":
            if group_stack:
                group_stack.pop()
        elif row_type in ("start", "end"):
            root_group["fields"].append({"name": row_name, "type": row_type, "list_name": None, "is_file": False})
        elif row_type in _ALWAYS_SKIP:
            continue
        elif row_type in _FILE_TYPES:
            if not keep_file_types:
                continue
            _current_group()["fields"].append(
                {"name": row_name, "type": row_type, "list_name": None, "is_file": True}
            )
        else:
            list_name: Optional[str] = row.get("select_from_list_name")
            _current_group()["fields"].append(
                {"name": row_name, "type": row_type, "list_name": list_name, "is_file": False}
            )

    groups = ordered_groups + ([root_group] if root_group["fields"] else [])

    return {
        "form_uid": form_uid,
        "version_id": version_id,
        "groups": groups,
        "choices_map": choices_map,
    }


class DataGenerator:
    def __init__(self, choices_map: dict[str, list[str]], locale: str = "en_US") -> None:
        self.choices_map = choices_map
        self._faker = Faker(locale)

    def generate(
        self,
        field_type: str,
        list_name: Optional[str],
        faker_type: Optional[str] = None,
    ) -> str:
        """Return a single string value for the given field type."""
        if field_type == "text":
            return self._faker_dispatch(faker_type) if faker_type else self._faker.sentence(nb_words=4)
        if field_type == "integer":
            return str(random.randint(1, 100))
        if field_type == "decimal":
            return str(round(random.uniform(1.0, 100.0), 2))
        if field_type == "select_one":
            pool = self.choices_map.get(list_name or "", [])
            return random.choice(pool) if pool else ""
        if field_type == "select_multiple":
            pool = self.choices_map.get(list_name or "", [])
            if not pool:
                return ""
            k = random.randint(1, len(pool))
            return " ".join(random.sample(pool, k=k))
        if field_type == "date":
            return self._faker.date_between("-1y", "today").isoformat()
        if field_type == "datetime":
            return self._faker.date_time_between("-1y", "now").isoformat() + "+00:00"
        if field_type == "geopoint":
            return f"{self._faker.latitude()} {self._faker.longitude()} 0 0"
        if field_type in ("start", "end"):
            return datetime.now(tz=timezone.utc).isoformat()
        return ""

    def _faker_dispatch(self, faker_type: Optional[str]) -> str:
        dispatch: dict = {
            "name": self._faker.name,
            "first_name": self._faker.first_name,
            "last_name": self._faker.last_name,
            "phone_number": self._faker.phone_number,
            "email": self._faker.email,
            "address": self._faker.address,
            "city": self._faker.city,
            "country": self._faker.country,
            "company": self._faker.company,
            "url": self._faker.url,
            "sentence": self._faker.sentence,
            "paragraph": self._faker.paragraph,
            "random_int": lambda: str(random.randint(1, 9999)),
        }
        fn = dispatch.get(faker_type or "")
        return fn() if fn else self._faker.sentence(nb_words=4)


def build_submission_xml(
    form_uid: str,
    version_id: str,
    groups: list[dict],
    field_values: dict[str, str],
    instance_id: Optional[str] = None,
) -> bytes:
    """Build OpenRosa XML bytes for one submission instance.

    Args:
        instance_id: UUID string (without ``uuid:`` prefix). Generated if not provided.
    """
    root = ET.Element("data", {"id": form_uid, "version": version_id})

    for group in groups:
        parent = root if group["name"] == "__root__" else ET.SubElement(root, group["name"])
        for field in group["fields"]:
            value = field_values.get(field["name"], "")
            if not value:
                continue
            el = ET.SubElement(parent, field["name"])
            el.text = value

    meta = ET.SubElement(root, "meta")
    instance_id_el = ET.SubElement(meta, "instanceID")
    instance_id_el.text = f"uuid:{instance_id or uuid.uuid4()}"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
