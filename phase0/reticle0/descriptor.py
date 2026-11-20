"""Parse the UPnP device description to recover the camera API endpoint.

The SSDP response only gives a ``LOCATION``. Sony puts the actionable data in
``<av:X_ScalarWebAPI_DeviceInfo>``: a service list of ``(type, ActionList_URL)``
pairs. The endpoint for a service is ``<ActionList_URL>/<service type>``.

Namespaces vary between firmware builds, so elements are matched by local name.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

DESCRIPTOR_TIMEOUT = 5.0


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return None


@dataclass
class DeviceDescription:
    """The parts of the descriptor Phase 0 cares about."""

    location: str
    model_name: str | None = None
    friendly_name: str | None = None
    api_version: str | None = None
    services: dict[str, str] = field(default_factory=dict)

    def endpoint_for(self, service: str = "camera") -> str | None:
        base = self.services.get(service)
        if not base:
            return None
        return f"{base.rstrip('/')}/{service}"

    @property
    def camera_endpoint(self) -> str | None:
        return self.endpoint_for("camera")

    def as_dict(self) -> dict[str, object]:
        return {
            "location": self.location,
            "model_name": self.model_name,
            "friendly_name": self.friendly_name,
            "api_version": self.api_version,
            "services": self.services,
            "camera_endpoint": self.camera_endpoint,
        }


def parse_device_description(xml_text: str, location: str = "") -> DeviceDescription:
    """Extract service URLs and identity from descriptor XML."""
    # Some firmware emits a stray BOM or leading whitespace before the declaration.
    cleaned = xml_text.lstrip("\ufeff \t\r\n")
    root = ET.fromstring(cleaned)

    services: dict[str, str] = {}
    for service in root.iter():
        if _local_name(service.tag) != "X_ScalarWebAPI_Service":
            continue
        service_type = _text_of(service, "X_ScalarWebAPI_ServiceType")
        action_url = _text_of(service, "X_ScalarWebAPI_ActionList_URL")
        if service_type and action_url:
            services[service_type] = action_url

    return DeviceDescription(
        location=location,
        model_name=_text_of(root, "modelName"),
        friendly_name=_text_of(root, "friendlyName"),
        api_version=_text_of(root, "X_ScalarWebAPI_Version"),
        services=services,
    )


def fetch_device_description(location: str, *, timeout: float = DESCRIPTOR_TIMEOUT) -> DeviceDescription:
    response = requests.get(location, timeout=timeout)
    response.raise_for_status()
    return parse_device_description(response.text, location=location)
