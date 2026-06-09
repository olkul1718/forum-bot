"""AmoCRM API v4 — создание лида + контакта с тегом Движение-2026."""
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TAG = "Движение-2026"


@dataclass
class ContactData:
    name: str = ""
    company: str = ""
    phone: str = ""
    email: str = ""
    comment: str = ""
    interest: str = ""
    crm_account: Optional[str] = None  # "ida" | "lite"


class AmoCRM:
    def __init__(self, subdomain: str, token: str, pipeline_id: int = 0):
        self.base = f"https://{subdomain}.amocrm.ru/api/v4"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.pipeline_id = pipeline_id

    def _post(self, path: str, payload: list) -> dict:
        url = f"{self.base}{path}"
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: list) -> dict:
        url = f"{self.base}{path}"
        with httpx.Client(timeout=15) as client:
            resp = client.patch(url, json=payload, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def create_lead(self, data: ContactData) -> int:
        """Создаёт контакт, лид и линкует их. Возвращает lead_id."""
        contact_id = self._create_contact(data)
        lead_id = self._create_lead_record(data, contact_id)
        return lead_id

    def _create_contact(self, data: ContactData) -> int:
        name_parts = data.name.strip().split() if data.name else ["Контакт"]
        first = name_parts[0]
        last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        custom_fields = []
        if data.phone:
            custom_fields.append({
                "field_code": "PHONE",
                "values": [{"value": data.phone, "enum_code": "WORK"}],
            })
        if data.email:
            custom_fields.append({
                "field_code": "EMAIL",
                "values": [{"value": data.email, "enum_code": "WORK"}],
            })

        payload = [{
            "first_name": first,
            "last_name": last,
            "custom_fields_values": custom_fields,
        }]

        result = self._post("/contacts", payload)
        contact_id = result["_embedded"]["contacts"][0]["id"]
        logger.info("Contact created: %s", contact_id)
        return contact_id

    def _create_lead_record(self, data: ContactData, contact_id: int) -> int:
        contact_name = data.name or "—"
        lead_name = f"Форум Движение-2026 — {contact_name}"
        if data.company:
            lead_name += f" ({data.company})"

        note_parts = []
        if data.company:
            note_parts.append(f"Компания: {data.company}")
        if data.interest:
            note_parts.append(f"Интерес: {data.interest}")
        if data.comment:
            note_parts.append(f"Комментарий: {data.comment}")
        note_text = "\n".join(note_parts)

        lead: dict = {
            "name": lead_name,
            "_embedded": {
                "tags": [{"name": TAG}],
                "contacts": [{"id": contact_id}],
            },
        }
        if self.pipeline_id:
            lead["pipeline_id"] = self.pipeline_id
        if note_text:
            # AmoCRM не принимает note в теле лида напрямую —
            # передаём через поле custom_fields_values если есть поле «Примечание»,
            # иначе добавим note отдельным запросом после создания.
            lead["_note"] = note_text  # временное поле, используем ниже

        note = lead.pop("_note", "")
        result = self._post("/leads", [lead])
        lead_id = result["_embedded"]["leads"][0]["id"]
        logger.info("Lead created: %s", lead_id)

        if note:
            self._add_note(lead_id, note)

        return lead_id

    def _add_note(self, lead_id: int, text: str) -> None:
        payload = [{
            "entity_id": lead_id,
            "note_type": "common",
            "params": {"text": text},
        }]
        url = f"{self.base}/leads/{lead_id}/notes"
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload, headers=self.headers)
        if resp.status_code not in (200, 201):
            logger.warning("Note not added: %s %s", resp.status_code, resp.text)
